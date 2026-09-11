"""Call patterns (CS-035): composable handlers selected per intent by
``CallIntent.call_pattern``.

Every handler that dials does so through :meth:`CallService.execute_call`, so
every pattern passes through the same gates (live switch, approval, policy,
allow-list, suppression, quiet hours, budget). No handler talks to the dialer
directly. A mission uses only the pattern each of its intents carries; nothing
forces a mission through every pattern.

Handlers
--------
``ONE_SHOT``            one recipient, one bounded goal.
``FAN_OUT``             one create with several ``recipients[]`` and a
                        ``recipient_result_schema``. One create rather than N
                        because that is what the CALL-E contract designed
                        ``recipients[]`` for: one idempotency key, one
                        approval that names every recipient, per-recipient
                        results mapped to ``RecipientResult``. The cost is that
                        the budget gate counts one run rather than N
                        recipients; ``CallPlan.estimated_cost_units`` keeps
                        the true count for the planner.
``CASCADE``             recipients in order; the next one is called only when
                        the previous run fails a code-evaluated
                        :class:`PassCondition`. Each step is its own single-
                        recipient child intent with an approval *derived* from
                        the parent's APPROVED approval — derivation is a code
                        check that the child's recipient is one the human
                        already approved, never a new grant.
``NEGOTIATION_ROUND``   the prior quote claims for the recipient's candidate
                        are injected into the task text as a delimited
                        untrusted block, and the result schema gains a
                        required ``counter_offer_status`` enum.
``CLARIFICATION``       conflicting claims for the candidate are injected the
                        same way; the schema gains ``clarification_status``.
``VERIFICATION``        the call runs, then each structured answer is compared
                        with the prior supported claim for the same candidate
                        and predicate: a mismatch marks *both* CONFLICTED (never
                        silently resolved); a match upgrades both to
                        MULTI_SOURCE_SUPPORTED.
``FOLLOW_UP``           persists a ``ScheduledJob`` due later. Does not dial.
``ESCALATION``          only when the policy allows; the next contact comes
                        from the candidate's ``escalation_contacts`` attribute
                        (exact E.164 only, never reformatted). A new number is
                        a new recipient, so a fresh approval is requested and
                        the pattern stops. Does not dial.
``HUMAN_GATE``          requests an approval and stops; proceeds as one-shot
                        only once that approval is APPROVED.

Progress is recorded on the intent's ``pattern_progress`` after every run.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import Field

from callswarm.approvals import ApprovalService
from callswarm.calls.provider import CallProviderError
from callswarm.calls.schema import validate_result_schema
from callswarm.calls.service import CallExecutionResult, CallService
from callswarm.config.settings import Settings
from callswarm.events import ActivityEventEmitter
from callswarm.llm.prompt import untrusted_block
from callswarm.models import (
    ActivityEvent,
    ActivityEventType,
    Approval,
    ApprovalStatus,
    ApprovalSubjectType,
    CallAuthorizationState,
    CallIntent,
    CallPattern,
    CallRecipient,
    CallRun,
    CallStatus,
    CandidateEntity,
    DomainModel,
    EvidenceClaim,
    EvidenceStatus,
    Mission,
    ScheduledJob,
    hash_phone,
    utcnow,
)
from callswarm.persistence import (
    ApprovalRepository,
    CallIntentRepository,
    CallRunRepository,
    CandidateEntityRepository,
    Database,
    EvidenceClaimRepository,
    MissionRepository,
    ScheduledJobRepository,
)

logger = logging.getLogger(__name__)

UNKNOWN_VALUE = "unknown"
COUNTER_OFFER_FIELD = "counter_offer_status"
COUNTER_OFFER_VALUES = ["improved", "unchanged", "refused", UNKNOWN_VALUE]
CLARIFICATION_FIELD = "clarification_status"
CLARIFICATION_VALUES = ["clarified", "still_unclear", UNKNOWN_VALUE]
ESCALATION_CONTACTS_ATTRIBUTE = "escalation_contacts"
FOLLOW_UP_JOB_TYPE = "call_follow_up"
NON_ATTRIBUTE_PREDICATES = frozenset({"call_summary", "call_evidence"})
SUPPORTED_STATUSES = frozenset(
    {
        EvidenceStatus.PHONE_SUPPORTED,
        EvidenceStatus.WEB_SUPPORTED,
        EvidenceStatus.MULTI_SOURCE_SUPPORTED,
    }
)
PatternStatus = Literal["completed", "pending", "stopped", "scheduled", "refused"]


class PatternRefused(CallProviderError):
    """The pattern cannot run for this intent under the current policy or data."""


# --- inputs and outputs ------------------------------------------------------------------


class PassCondition(DomainModel):
    """Code-evaluated success test for a cascade step.

    Passes when the run completed with a schema-valid ``structured_result``
    and, if ``field`` is set, that field holds one of ``accepted_values``
    (``unknown`` never passes).
    """

    field: str | None = None
    accepted_values: list[str] = Field(default_factory=list)

    def passes(self, result: CallExecutionResult) -> bool:
        run = result.run
        if run.status is not CallStatus.COMPLETED or run.structured_result is None:
            return False
        if result.result_validation_failed:
            return False
        if self.field is None:
            return True
        value = run.structured_result.get(self.field)
        if value is None or (isinstance(value, str) and value.lower() == UNKNOWN_VALUE):
            return False
        return str(value) in self.accepted_values


class PatternOptions(DomainModel):
    pass_condition: PassCondition = Field(default_factory=PassCondition)
    follow_up_due_at: datetime | None = None
    prior_claim_ids: list[str] = Field(
        default_factory=list,
        description="Claims to negotiate against / verify; empty means auto-select by candidate",
    )


class PatternStep(DomainModel):
    kind: str
    intent_id: str
    run_id: str | None = None
    status: str
    detail: str = ""


class PatternOutcome(DomainModel):
    pattern: CallPattern
    intent_id: str
    status: PatternStatus
    steps: list[PatternStep] = Field(default_factory=list)
    results: list[CallExecutionResult] = Field(default_factory=list)
    reason: str | None = None
    scheduled_job_id: str | None = None
    approval_id: str | None = None
    conflicted_claim_ids: list[str] = Field(default_factory=list)
    corroborated_claim_ids: list[str] = Field(default_factory=list)

    @property
    def dialed(self) -> bool:
        return bool(self.results)


class PatternHandler(Protocol):
    pattern: CallPattern

    async def run(
        self, runner: PatternRunner, intent: CallIntent, options: PatternOptions
    ) -> PatternOutcome: ...


# --- pure helpers ----------------------------------------------------------------------


def _same_value(a: Any, b: Any) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a).strip().lower() == str(b).strip().lower()


def claims_as_context(claims: list[EvidenceClaim]) -> str:
    lines = [
        f"{c.predicate}: {json.dumps(c.value, ensure_ascii=False)} "
        f"(source {c.source_type.value}, status {c.evidence_status.value})"
        for c in claims
    ]
    return "\n".join(lines)


def with_context_block(goal: str, label: str, claims: list[EvidenceClaim], instruction: str) -> str:
    """Append a delimited untrusted block of prior claims to the goal text.
    The claims came from a phone call or a web page; they are data."""
    block = untrusted_block(label, claims_as_context(claims))
    return f"{goal.rstrip()}\n\n{instruction}\n{block}"


def with_status_enum(
    schema: dict[str, Any], field: str, values: list[str], description: str
) -> dict[str, Any]:
    """Add a required string enum to a result schema and validate the result."""
    updated: dict[str, Any] = json.loads(json.dumps(schema)) if schema else {}
    updated.setdefault("type", "object")
    updated.setdefault("additionalProperties", False)
    properties = updated.setdefault("properties", {})
    properties[field] = {"type": "string", "enum": list(values), "description": description}
    required = list(updated.get("required", []))
    if field not in required:
        required.append(field)
    updated["required"] = required
    validate_result_schema(updated)
    return updated


def derive_step_approval(
    parent: Approval, child: CallIntent, parent_intent: CallIntent
) -> Approval:
    """An approval for a cascade step, derived by code from the parent's.

    Allowed only when the parent is APPROVED and unexpired and every child
    recipient is one of the parent's recipients: the human approved dialing
    those numbers; the step merely dials one of them.
    """
    if parent.status is not ApprovalStatus.APPROVED:
        raise PatternRefused(f"parent approval is {parent.status.value}, not APPROVED")
    if parent.expires_at is not None and parent.expires_at <= utcnow():
        raise PatternRefused("parent approval is EXPIRED")
    approved_numbers = {r.phone_e164 for r in parent_intent.recipients}
    if not {r.phone_e164 for r in child.recipients} <= approved_numbers:
        raise PatternRefused("cascade step names a recipient the parent approval does not")
    return Approval(
        mission_id=child.mission_id,
        subject_type=ApprovalSubjectType.CALL_INTENT,
        subject_id=child.id,
        status=ApprovalStatus.APPROVED,
        requested_at=parent.requested_at,
        decided_at=parent.decided_at,
        expires_at=parent.expires_at,
        decided_by=parent.decided_by,
        reason=f"derived from approval {parent.id} for cascade step",
    )


def escalation_contacts(candidate: CandidateEntity) -> list[CallRecipient]:
    """Exact E.164 contacts from the candidate's attributes. Anything that
    does not validate is skipped, never reformatted or guessed."""
    raw = candidate.attributes.get(ESCALATION_CONTACTS_ATTRIBUTE)
    if not isinstance(raw, list):
        return []
    contacts: list[CallRecipient] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            contacts.append(
                CallRecipient(
                    entity_id=candidate.id,
                    phone_e164=str(item.get("phone_e164", "")),
                    region=item.get("region") or candidate.contact.region,
                    locale=item.get("locale") or candidate.contact.locale,
                )
            )
        except ValueError:
            continue
    return contacts


def _usable(approval: Approval | None) -> bool:
    return (
        approval is not None
        and approval.status is ApprovalStatus.APPROVED
        and (approval.expires_at is None or approval.expires_at > utcnow())
    )


# --- handlers ------------------------------------------------------------------------------


class OneShotHandler:
    pattern = CallPattern.ONE_SHOT

    async def run(
        self, runner: PatternRunner, intent: CallIntent, options: PatternOptions
    ) -> PatternOutcome:
        result = await runner.service.execute_call(intent.id, advance_mission=False)
        return runner.single_result_outcome(self.pattern, intent, result)


class FanOutHandler:
    pattern = CallPattern.FAN_OUT

    async def run(
        self, runner: PatternRunner, intent: CallIntent, options: PatternOptions
    ) -> PatternOutcome:
        if len(intent.recipients) < 2:
            return runner.refused(self.pattern, intent, "fan-out needs at least two recipients")
        if not intent.recipient_result_schema:
            # Per-recipient outcomes use the task-level fields; the validator
            # enforces the reserved-name rule on the copy.
            schema = json.loads(json.dumps(intent.result_schema)) if intent.result_schema else {}
            if schema:
                validate_result_schema(schema)
                intent = await runner.update_intent(intent, recipient_result_schema=schema)
        result = await runner.service.execute_call(intent.id, advance_mission=False)
        outcome = runner.single_result_outcome(self.pattern, intent, result)
        outcome.steps.extend(
            PatternStep(
                kind="recipient",
                intent_id=intent.id,
                run_id=result.run.id,
                status=r.status.value,
                detail=r.recipient_ref,
            )
            for r in result.run.recipient_results
        )
        return outcome


class CascadeHandler:
    pattern = CallPattern.CASCADE

    async def run(
        self, runner: PatternRunner, intent: CallIntent, options: PatternOptions
    ) -> PatternOutcome:
        if len(intent.recipients) < 2:
            return runner.refused(self.pattern, intent, "cascade needs at least two recipients")
        parent_approval = await runner.approvals.latest_for_intent(intent.id)
        if not _usable(parent_approval):
            return runner.refused(
                self.pattern, intent, "cascade needs an APPROVED approval for the parent intent"
            )
        assert parent_approval is not None
        steps: list[PatternStep] = []
        results: list[CallExecutionResult] = []
        for index, recipient in enumerate(intent.recipients):
            child = intent.model_copy(
                update={
                    "id": f"{intent.id}-step{index + 1}",
                    "recipients": [recipient],
                    "purpose": f"{intent.purpose} (cascade step {index + 1} of "
                    f"{len(intent.recipients)})",
                    "call_pattern": CallPattern.ONE_SHOT,
                    "authorization_state": CallAuthorizationState.APPROVED,
                    "pattern_progress": {"parent_intent_id": intent.id, "step": index + 1},
                    "created_at": utcnow(),
                    "updated_at": utcnow(),
                }
            )
            try:
                approval = derive_step_approval(parent_approval, child, intent)
            except PatternRefused as exc:
                return runner.refused(self.pattern, intent, str(exc), steps=steps)
            await runner.persist_child(child, approval)
            result = await runner.service.execute_call(child.id, advance_mission=False)
            results.append(result)
            if result.pending:
                steps.append(
                    PatternStep(
                        kind="step", intent_id=child.id, run_id=result.run.id, status="pending"
                    )
                )
                return PatternOutcome(
                    pattern=self.pattern,
                    intent_id=intent.id,
                    status="pending",
                    steps=steps,
                    results=results,
                    reason="awaiting the terminal result before evaluating the condition",
                )
            passed = options.pass_condition.passes(result)
            steps.append(
                PatternStep(
                    kind="step",
                    intent_id=child.id,
                    run_id=result.run.id,
                    status="passed" if passed else "failed_condition",
                )
            )
            if passed:
                return PatternOutcome(
                    pattern=self.pattern,
                    intent_id=intent.id,
                    status="completed",
                    steps=steps,
                    results=results,
                    reason=f"step {index + 1} passed the condition",
                )
        return PatternOutcome(
            pattern=self.pattern,
            intent_id=intent.id,
            status="completed",
            steps=steps,
            results=results,
            reason="no recipient passed the condition",
        )


class NegotiationHandler:
    pattern = CallPattern.NEGOTIATION_ROUND

    async def run(
        self, runner: PatternRunner, intent: CallIntent, options: PatternOptions
    ) -> PatternOutcome:
        mission = await runner.mission(intent.mission_id)
        if not mission.authority_policy.negotiation_allowed:
            return runner.refused(self.pattern, intent, "negotiation is not allowed by the policy")
        prior = await runner.prior_claims(intent, options, statuses=SUPPORTED_STATUSES)
        if not prior:
            return runner.refused(self.pattern, intent, "no prior quote claim to negotiate against")
        goal = with_context_block(
            intent.call_goal,
            "prior quote from an earlier conversation",
            prior,
            "The earlier conversation with this recipient established the following. Treat it "
            "as context only; ask whether they can improve on it and record the outcome.",
        )
        schema = with_status_enum(
            intent.result_schema,
            COUNTER_OFFER_FIELD,
            COUNTER_OFFER_VALUES,
            "Whether the recipient improved on the earlier quote: improved, unchanged, refused "
            "to discuss, or unknown if the call did not establish it.",
        )
        if not await runner.has_run(intent.id):
            # A rerun (e.g. after a pending run) must not append a second
            # context block or rewrite the schema under an executed intent.
            intent = await runner.update_intent(intent, call_goal=goal, result_schema=schema)
        result = await runner.service.execute_call(intent.id, advance_mission=False)
        return runner.single_result_outcome(self.pattern, intent, result)


class ClarificationHandler:
    pattern = CallPattern.CLARIFICATION

    async def run(
        self, runner: PatternRunner, intent: CallIntent, options: PatternOptions
    ) -> PatternOutcome:
        conflicted = await runner.prior_claims(
            intent, options, statuses=frozenset({EvidenceStatus.CONFLICTED})
        )
        goal = intent.call_goal
        if conflicted:
            goal = with_context_block(
                goal,
                "conflicting information to clarify",
                conflicted,
                "Sources disagree on the following. Treat it as context only and establish "
                "which is correct.",
            )
        schema = with_status_enum(
            intent.result_schema,
            CLARIFICATION_FIELD,
            CLARIFICATION_VALUES,
            "Whether the open point was clarified on the call, is still unclear, or unknown.",
        )
        if not await runner.has_run(intent.id):
            # A rerun must not append a second context block or rewrite the
            # schema under an already-dialed intent.
            intent = await runner.update_intent(intent, call_goal=goal, result_schema=schema)
        result = await runner.service.execute_call(intent.id, advance_mission=False)
        return runner.single_result_outcome(self.pattern, intent, result)


class VerificationHandler:
    pattern = CallPattern.VERIFICATION

    async def run(
        self, runner: PatternRunner, intent: CallIntent, options: PatternOptions
    ) -> PatternOutcome:
        mission = await runner.mission(intent.mission_id)
        if not mission.authority_policy.confirmation_calls_allowed:
            return runner.refused(
                self.pattern, intent, "confirmation calls are not allowed by the policy"
            )
        prior = await runner.prior_claims(intent, options, statuses=SUPPORTED_STATUSES)
        result = await runner.service.execute_call(intent.id, advance_mission=False)
        outcome = runner.single_result_outcome(self.pattern, intent, result)
        if result.pending:
            return outcome
        conflicted, corroborated = await runner.compare_claims(result.claims, prior)
        outcome.conflicted_claim_ids = conflicted
        outcome.corroborated_claim_ids = corroborated
        outcome.reason = (
            f"{len(conflicted)} claim(s) marked CONFLICTED, {len(corroborated)} corroborated"
        )
        return outcome


class FollowUpHandler:
    pattern = CallPattern.FOLLOW_UP

    async def run(
        self, runner: PatternRunner, intent: CallIntent, options: PatternOptions
    ) -> PatternOutcome:
        mission = await runner.mission(intent.mission_id)
        if not mission.authority_policy.scheduled_follow_up_allowed:
            return runner.refused(
                self.pattern, intent, "scheduled follow-up is not allowed by the policy"
            )
        due = options.follow_up_due_at
        if due is None or due <= utcnow():
            return runner.refused(
                self.pattern, intent, "a follow-up needs a due time in the future"
            )
        job = ScheduledJob(
            mission_id=intent.mission_id,
            job_type=FOLLOW_UP_JOB_TYPE,
            due_at=due,
            payload={"call_intent_id": intent.id, "pattern": self.pattern.value},
        )
        async with runner.database.session() as session:
            job = await ScheduledJobRepository(session).add(job)
        await runner.emit(
            intent.mission_id,
            ActivityEventType.SCHEDULER_EVENT,
            f"Follow-up call scheduled for {due.isoformat()}: {intent.purpose}",
            call_intent_id=intent.id,
            scheduled_job_id=job.id,
        )
        return PatternOutcome(
            pattern=self.pattern,
            intent_id=intent.id,
            status="scheduled",
            scheduled_job_id=job.id,
            steps=[PatternStep(kind="schedule", intent_id=intent.id, status="scheduled")],
            reason="the call is scheduled; nothing was dialed now",
        )


class EscalationHandler:
    pattern = CallPattern.ESCALATION

    async def run(
        self, runner: PatternRunner, intent: CallIntent, options: PatternOptions
    ) -> PatternOutcome:
        mission = await runner.mission(intent.mission_id)
        if not mission.authority_policy.escalation_allowed:
            return runner.refused(self.pattern, intent, "escalation is not allowed by the policy")
        entity_id = intent.recipients[0].entity_id
        candidate = None if entity_id is None else await runner.candidate(entity_id)
        if candidate is None:
            return runner.refused(self.pattern, intent, "no candidate to escalate within")
        # Progress records hashes, never numbers: the intent row's recipients are
        # the only place a full number lives.
        escalated_hashes = set(intent.pattern_progress.get("escalated_phone_hashes", []))
        already = {hash_phone(r.phone_e164) for r in intent.recipients} | escalated_hashes
        contact = next(
            (c for c in escalation_contacts(candidate) if hash_phone(c.phone_e164) not in already),
            None,
        )
        if contact is None:
            return runner.refused(
                self.pattern, intent, "no unambiguous escalation contact is recorded"
            )
        child = intent.model_copy(
            update={
                "id": f"{intent.id}-escalation{len(escalated_hashes) + 1}",
                "recipients": [contact],
                "purpose": f"Escalation: {intent.purpose}",
                "call_pattern": CallPattern.ONE_SHOT,
                "authorization_state": CallAuthorizationState.NOT_REQUESTED,
                "pattern_progress": {"parent_intent_id": intent.id, "escalation": True},
                "created_at": utcnow(),
                "updated_at": utcnow(),
            }
        )
        await runner.persist_child(child, None)
        # A new number is a new recipient: it needs its own explicit approval.
        approval = await runner.approvals.request(child)
        await runner.update_intent(
            intent,
            pattern_progress={
                **intent.pattern_progress,
                "escalated_phone_hashes": sorted(
                    escalated_hashes | {hash_phone(contact.phone_e164)}
                ),
                "escalation_intent_ids": [
                    *intent.pattern_progress.get("escalation_intent_ids", []),
                    child.id,
                ],
            },
        )
        return PatternOutcome(
            pattern=self.pattern,
            intent_id=intent.id,
            status="stopped",
            approval_id=approval.id,
            steps=[
                PatternStep(
                    kind="escalation",
                    intent_id=child.id,
                    status="approval_requested",
                    detail="the escalation contact needs a new explicit approval",
                )
            ],
            reason="escalation contact requires a new explicit approval before any call",
        )


class HumanGateHandler:
    pattern = CallPattern.HUMAN_GATE

    async def run(
        self, runner: PatternRunner, intent: CallIntent, options: PatternOptions
    ) -> PatternOutcome:
        approval = await runner.approvals.latest_for_intent(intent.id)
        if _usable(approval):
            result = await runner.service.execute_call(intent.id, advance_mission=False)
            return runner.single_result_outcome(self.pattern, intent, result)
        if approval is None or approval.status is not ApprovalStatus.PENDING:
            approval = await runner.approvals.request(intent)
        return PatternOutcome(
            pattern=self.pattern,
            intent_id=intent.id,
            status="stopped",
            approval_id=approval.id,
            steps=[PatternStep(kind="human_gate", intent_id=intent.id, status="approval_pending")],
            reason="waiting for a human decision; nothing was dialed",
        )


HANDLERS: dict[CallPattern, PatternHandler] = {
    handler.pattern: handler
    for handler in (
        OneShotHandler(),
        FanOutHandler(),
        CascadeHandler(),
        NegotiationHandler(),
        ClarificationHandler(),
        VerificationHandler(),
        FollowUpHandler(),
        EscalationHandler(),
        HumanGateHandler(),
    )
}


# --- runner ------------------------------------------------------------------------------


class PatternRunner:
    """Dispatches an intent to its pattern handler and records progress."""

    def __init__(
        self,
        service: CallService,
        approvals: ApprovalService,
        database: Database,
        emitter: ActivityEventEmitter,
        settings: Settings,
    ) -> None:
        self.service = service
        self.approvals = approvals
        self.database = database
        self._emitter = emitter
        self._settings = settings

    async def run(
        self, intent: CallIntent, options: PatternOptions | None = None
    ) -> PatternOutcome:
        # Always work from the persisted row: progress from earlier rounds
        # lives there, and the caller's copy may be stale.
        intent = await self.update_intent(intent, updated_at=utcnow())
        handler = HANDLERS[intent.call_pattern]
        opts = options or PatternOptions()
        intent = await self.update_intent(
            intent,
            pattern_progress={
                **intent.pattern_progress,
                "pattern": intent.call_pattern.value,
                "status": "running",
                "started_at": utcnow().isoformat(),
            },
        )
        outcome = await handler.run(self, intent, opts)
        if outcome.dialed and outcome.status != "pending":
            # One transition per pattern round, however many calls it placed.
            await self.service.mark_results_received(
                intent.mission_id, f"pattern {outcome.pattern.value} {outcome.status}"
            )
        await self.record_progress(intent.id, outcome)
        await self.emit(
            intent.mission_id,
            ActivityEventType.CALL_EVENT,
            f"Call pattern {outcome.pattern.value} {outcome.status}: "
            f"{outcome.reason or intent.purpose}",
            call_intent_id=intent.id,
            pattern=outcome.pattern.value,
            status=outcome.status,
            step_count=len(outcome.steps),
            dialed=outcome.dialed,
            scheduled_job_id=outcome.scheduled_job_id,
            approval_id=outcome.approval_id,
            conflicted_claim_count=len(outcome.conflicted_claim_ids),
        )
        return outcome

    # --- helpers used by handlers ----------------------------------------------------
    def refused(
        self,
        pattern: CallPattern,
        intent: CallIntent,
        reason: str,
        *,
        steps: list[PatternStep] | None = None,
    ) -> PatternOutcome:
        logger.info("pattern %s refused for intent %s: %s", pattern.value, intent.id, reason)
        return PatternOutcome(
            pattern=pattern,
            intent_id=intent.id,
            status="refused",
            steps=steps or [],
            reason=reason,
        )

    @staticmethod
    def single_result_outcome(
        pattern: CallPattern, intent: CallIntent, result: CallExecutionResult
    ) -> PatternOutcome:
        return PatternOutcome(
            pattern=pattern,
            intent_id=intent.id,
            status="pending" if result.pending else "completed",
            steps=[
                PatternStep(
                    kind="call",
                    intent_id=intent.id,
                    run_id=result.run.id,
                    status=result.run.status.value,
                )
            ],
            results=[result],
        )

    async def emit(
        self, mission_id: str, event_type: ActivityEventType, summary: str, **payload: object
    ) -> None:
        await self._emitter.emit(
            ActivityEvent(
                mission_id=mission_id,
                event_type=event_type,
                summary=summary,
                payload=dict(payload),
            )
        )

    async def mission(self, mission_id: str) -> Mission:
        async with self.database.session() as session:
            mission = await MissionRepository(session).get(mission_id)
        if mission is None:
            raise KeyError(f"mission {mission_id!r} not found")
        return mission

    async def candidate(self, entity_id: str) -> CandidateEntity | None:
        async with self.database.session() as session:
            return await CandidateEntityRepository(session).get(entity_id)

    async def update_intent(self, intent: CallIntent, **changes: Any) -> CallIntent:
        async with self.database.session() as session:
            intents = CallIntentRepository(session)
            stored = await intents.get(intent.id)
            if stored is None:
                raise KeyError(f"call intent {intent.id!r} not found")
            return await intents.update(
                stored.model_copy(update={**changes, "updated_at": utcnow()})
            )

    async def record_progress(self, intent_id: str, outcome: PatternOutcome) -> None:
        async with self.database.session() as session:
            intents = CallIntentRepository(session)
            stored = await intents.get(intent_id)
            if stored is None:
                return
            progress = {
                **stored.pattern_progress,
                "pattern": outcome.pattern.value,
                "status": outcome.status,
                "reason": outcome.reason,
                "steps": [s.model_dump(mode="json") for s in outcome.steps],
                "run_ids": [r.run.id for r in outcome.results],
                "scheduled_job_id": outcome.scheduled_job_id,
                "approval_id": outcome.approval_id,
                "conflicted_claim_ids": outcome.conflicted_claim_ids,
                "updated_at": utcnow().isoformat(),
            }
            await intents.update(
                stored.model_copy(update={"pattern_progress": progress, "updated_at": utcnow()})
            )

    async def persist_child(self, child: CallIntent, approval: Approval | None) -> CallIntent:
        """Get-or-create. Child ids are deterministic (``{parent}-step{n}``) so
        a rerun after a ``pending`` step finds the existing child (and its
        approval) instead of failing the insert; ``execute_call`` then treats
        the already-dialed step as an idempotent no-op."""
        async with self.database.session() as session:
            intents = CallIntentRepository(session)
            existing = await intents.get(child.id)
            if existing is not None:
                return existing
            stored = await intents.add(child)
            if approval is not None:
                await ApprovalRepository(session).add(approval)
        return stored

    async def has_run(self, intent_id: str) -> bool:
        """True once a run exists for the intent: the goal text and schema
        must not be rewritten under an already-dialed intent."""
        async with self.database.session() as session:
            return bool(await CallRunRepository(session).list_by_intent(intent_id))

    async def prior_claims(
        self,
        intent: CallIntent,
        options: PatternOptions,
        *,
        statuses: frozenset[EvidenceStatus],
    ) -> list[EvidenceClaim]:
        """Explicitly named claims, else the candidate's attribute claims in
        the given statuses. Never the summary/evidence prose claims."""
        async with self.database.session() as session:
            repo = EvidenceClaimRepository(session)
            if options.prior_claim_ids:
                named = [await repo.get(cid) for cid in options.prior_claim_ids]
                return [c for c in named if c is not None and c.mission_id == intent.mission_id]
            claims = await repo.list_by_mission(intent.mission_id)
        entity_id = intent.recipients[0].entity_id if intent.recipients else None
        return [
            c
            for c in claims
            if c.entity_id is not None
            and c.entity_id == entity_id
            and c.evidence_status in statuses
            and c.predicate not in NON_ATTRIBUTE_PREDICATES
        ]

    async def compare_claims(
        self, fresh: list[EvidenceClaim], prior: list[EvidenceClaim]
    ) -> tuple[list[str], list[str]]:
        """Mismatch → both CONFLICTED with cross-references; match → both
        MULTI_SOURCE_SUPPORTED. Returns (conflicted ids, corroborated ids)."""
        conflicted: list[str] = []
        corroborated: list[str] = []
        by_predicate: dict[str, list[EvidenceClaim]] = {}
        for claim in prior:
            by_predicate.setdefault(claim.predicate, []).append(claim)
        async with self.database.session() as session:
            repo = EvidenceClaimRepository(session)
            for claim in fresh:
                if claim.evidence_status is not EvidenceStatus.PHONE_SUPPORTED:
                    continue
                for earlier in by_predicate.get(claim.predicate, []):
                    if earlier.id == claim.id:
                        continue
                    if _same_value(claim.value, earlier.value):
                        await repo.update(
                            claim.model_copy(
                                update={"evidence_status": EvidenceStatus.MULTI_SOURCE_SUPPORTED}
                            )
                        )
                        await repo.update(
                            earlier.model_copy(
                                update={"evidence_status": EvidenceStatus.MULTI_SOURCE_SUPPORTED}
                            )
                        )
                        corroborated.extend([claim.id, earlier.id])
                    else:
                        await repo.update(
                            claim.model_copy(
                                update={
                                    "evidence_status": EvidenceStatus.CONFLICTED,
                                    "conflicts": sorted({*claim.conflicts, earlier.id}),
                                }
                            )
                        )
                        await repo.update(
                            earlier.model_copy(
                                update={
                                    "evidence_status": EvidenceStatus.CONFLICTED,
                                    "conflicts": sorted({*earlier.conflicts, claim.id}),
                                }
                            )
                        )
                        conflicted.extend([claim.id, earlier.id])
        return sorted(set(conflicted)), sorted(set(corroborated))


def outcome_dialed_runs(outcome: PatternOutcome) -> list[CallRun]:
    return [r.run for r in outcome.results]
