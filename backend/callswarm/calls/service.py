"""Provider selection and call execution (CS-031 / CS-034 / CS-032 / CS-036).

``select_call_provider`` honours ``CALL_PROVIDER``: ``fake`` builds the
:class:`FakeCallProvider`; ``calle`` builds :class:`CalleProvider`, which
raises :class:`CallProviderNotAvailable` without ``CALLE_API_KEY``. It never
falls back — a fake call presented as real is worse than a hard startup error.

``CallService.execute_call(intent_id)`` is the one path from an authorized
intent to evidence:

    load intent, latest approval, mission
    → gate pre-check (raises cleanly; mission stays CALL_AUTHORIZED)
    → provider.plan_call → provider.authorize
    → mission CALL_AUTHORIZED → CALL_EXECUTION_RUNNING
    → provider.execute (the gate runs again inside the provider)
        ambiguous create → provider.reconcile (identical replay, never a
        fresh POST); an unrecoverable error → mission BLOCKED
    → persist CallRun, count it against the budget
    → terminal run: mission → CALL_RESULT_RECEIVED, result → EvidenceClaims
    → non-terminal run (webhook mode): mission stays CALL_EXECUTION_RUNNING
      and ``finalize_run`` completes it later from a re-read of the
      authoritative state (webhook receiver or poller)

Claims from a simulated run are ``source_type=SIMULATED``; from a real run
``PHONE``. Either way the status is ``PHONE_SUPPORTED``: a phone claim records
what was *said*, never verified truth. A ``structured_result`` of ``None``
yields no attribute claims (gaps stay ``UNKNOWN``); the summary and
``evidence[]`` are stored as low-confidence claims labelled as such. Per
recipient results of a fan-out call become claims for that recipient's
candidate.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from pydantic import Field

from callswarm.approvals import ApprovalService
from callswarm.calls.calle import AmbiguousCreate, CalleProvider
from callswarm.calls.fake import FakeCallProvider
from callswarm.calls.gates import CallGate
from callswarm.calls.provider import (
    NON_TERMINAL_STATUSES,
    CallExecutionProvider,
    CallProviderError,
    CallProviderNotAvailable,
)
from callswarm.calls.schema import validate_result_against_schema
from callswarm.config.settings import Settings
from callswarm.events import ActivityEventEmitter
from callswarm.models import (
    ActivityEvent,
    ActivityEventType,
    CallBudget,
    CallIntent,
    CallRun,
    CallStatus,
    DomainModel,
    EvidenceClaim,
    EvidenceStatus,
    Mission,
    MissionStatus,
    SourceType,
    WebhookEventType,
    utcnow,
)
from callswarm.orchestrator.state_machine import MissionStateMachine
from callswarm.persistence import (
    CallIntentRepository,
    CallRunRepository,
    CandidateEntityRepository,
    Database,
    EvidenceClaimRepository,
    MissionRepository,
)
from callswarm.sanitize import mask_phones

logger = logging.getLogger(__name__)

UNKNOWN_VALUE = "unknown"
SUMMARY_PREDICATE = "call_summary"
EVIDENCE_PREDICATE = "call_evidence"
LOW_CONFIDENCE_LABEL = "low-confidence"


def select_call_provider(
    settings: Settings, database: Database, emitter: ActivityEventEmitter
) -> CallExecutionProvider:
    """Build the configured provider or raise. Never silently substitutes fake."""
    gate = CallGate(database, emitter)
    if settings.call_provider == "fake":
        return FakeCallProvider(gate, settings, database)
    if settings.call_provider == "calle":
        # Raises CallProviderNotAvailable without CALLE_API_KEY. Live dialing
        # still needs CALLE_LIVE_CALLS_ENABLED=true and an APPROVED approval.
        return CalleProvider(gate, settings, database)
    raise CallProviderNotAvailable(f"unknown CALL_PROVIDER {settings.call_provider!r}")


class CallExecutionResult(DomainModel):
    run: CallRun
    claims: list[EvidenceClaim] = Field(default_factory=list)
    result_validation_failed: bool = False
    pending: bool = Field(
        default=False,
        description="The run is queued or in progress; the terminal result arrives later",
    )


def _flatten(value: Any) -> Any:
    """Claim values are JSON scalars or JSON text for structures."""
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _structured_claims(
    run: CallRun,
    structured: dict[str, Any],
    subject: str,
    entity_id: str | None,
    source_type: SourceType,
    reference: str,
) -> list[EvidenceClaim]:
    claims: list[EvidenceClaim] = []
    for key, value in structured.items():
        if value is None or (isinstance(value, str) and value.strip().lower() == UNKNOWN_VALUE):
            continue
        claims.append(
            EvidenceClaim(
                mission_id=run.mission_id,
                subject=subject,
                predicate=key,
                value=_flatten(value),
                source_type=source_type,
                source_reference=reference,
                evidence_status=EvidenceStatus.PHONE_SUPPORTED,
                entity_id=entity_id,
            )
        )
    return claims


def _recipient_entity(intent: CallIntent, recipient_ref: str) -> str | None:
    for index, recipient in enumerate(intent.recipients):
        if recipient_ref in (recipient.entity_id, f"recipient-{index}"):
            return recipient.entity_id
    return None


def claims_from_run(
    run: CallRun,
    intent: CallIntent,
    subject: str,
    *,
    result_valid: bool,
    recipient_subjects: Mapping[str, str] | None = None,
) -> list[EvidenceClaim]:
    """Convert a terminal run into claims. Pure; used by the service and tests.

    Task-level ``structured_result`` fields become claims about ``subject``
    (the first recipient's candidate). Per-recipient ``structured_result``
    fields (fan-out) become claims about that recipient's candidate, whose
    subject comes from ``recipient_subjects`` keyed by ``recipient_ref``.
    """
    source_type = SourceType.SIMULATED if run.is_simulated else SourceType.PHONE
    reference = f"call_run:{run.id}"
    entity_id = intent.recipients[0].entity_id if intent.recipients else None
    claims: list[EvidenceClaim] = []
    if run.structured_result is not None and result_valid:
        claims.extend(
            _structured_claims(
                run, run.structured_result, subject, entity_id, source_type, reference
            )
        )
    subjects = recipient_subjects or {}
    for recipient in run.recipient_results:
        if recipient.structured_result is None:
            continue
        if intent.recipient_result_schema and validate_result_against_schema(
            recipient.structured_result, intent.recipient_result_schema
        ):
            continue
        if not intent.recipient_result_schema and recipient.structured_result == (
            run.structured_result
        ):
            # The fake provider echoes the task-level result on recipient 0.
            continue
        claims.extend(
            _structured_claims(
                run,
                recipient.structured_result,
                subjects.get(recipient.recipient_ref, subject),
                _recipient_entity(intent, recipient.recipient_ref),
                source_type,
                f"{reference};recipient:{recipient.recipient_ref}",
            )
        )
    if run.summary.strip():
        claims.append(
            EvidenceClaim(
                mission_id=run.mission_id,
                subject=subject,
                predicate=SUMMARY_PREDICATE,
                value=mask_phones(run.summary),
                source_type=source_type,
                source_reference=f"{reference};{LOW_CONFIDENCE_LABEL}",
                evidence_status=EvidenceStatus.UNKNOWN,
                entity_id=entity_id,
            )
        )
    for item in run.evidence:
        if not item.strip():
            continue
        claims.append(
            EvidenceClaim(
                mission_id=run.mission_id,
                subject=subject,
                predicate=EVIDENCE_PREDICATE,
                value=mask_phones(item),
                source_type=source_type,
                source_reference=f"{reference};{LOW_CONFIDENCE_LABEL}",
                evidence_status=EvidenceStatus.UNKNOWN,
                entity_id=entity_id,
            )
        )
    return claims


class CallService:
    def __init__(
        self,
        provider: CallExecutionProvider,
        gate: CallGate,
        approvals: ApprovalService,
        database: Database,
        emitter: ActivityEventEmitter,
        settings: Settings,
        state_machine: MissionStateMachine,
    ) -> None:
        self.provider = provider
        self._gate = gate
        self._approvals = approvals
        self._database = database
        self._emitter = emitter
        self._settings = settings
        self._machine = state_machine

    async def _emit(self, mission_id: str, summary: str, **payload: object) -> None:
        await self._emitter.emit(
            ActivityEvent(
                mission_id=mission_id,
                event_type=ActivityEventType.CALL_EVENT,
                summary=summary,
                payload=dict(payload),
            )
        )

    async def _load(self, intent_id: str) -> tuple[CallIntent, Mission]:
        async with self._database.session() as session:
            intent = await CallIntentRepository(session).get(intent_id)
            if intent is None:
                raise KeyError(f"call intent {intent_id!r} not found")
            mission = await MissionRepository(session).get(intent.mission_id)
        if mission is None:
            raise KeyError(f"mission {intent.mission_id!r} not found")
        return intent, mission

    async def _subjects_for(self, intent: CallIntent) -> tuple[str, dict[str, str]]:
        """The task-level subject plus a ``recipient_ref → subject`` map."""
        by_ref: dict[str, str] = {}
        async with self._database.session() as session:
            candidates = CandidateEntityRepository(session)
            for index, recipient in enumerate(intent.recipients):
                ref = recipient.entity_id or f"recipient-{index}"
                candidate = (
                    None
                    if recipient.entity_id is None
                    else await candidates.get(recipient.entity_id)
                )
                by_ref[ref] = (
                    candidate.display_name
                    if candidate is not None
                    else f"call_intent:{intent.id}#{index}"
                )
        first_ref = (intent.recipients[0].entity_id or "recipient-0") if intent.recipients else ""
        subject = by_ref.get(first_ref, f"call_intent:{intent.id}")
        if subject.startswith("call_intent:"):
            subject = f"call_intent:{intent.id}"
        return subject, by_ref

    async def _existing(self, intent: CallIntent) -> CallExecutionResult | None:
        """An intent executes once. A repeat call is an idempotent no-op that
        returns the stored run and its claims: no dial, no budget increment,
        no duplicate claims."""
        async with self._database.session() as session:
            runs = await CallRunRepository(session).list_by_intent(intent.id)
            if not runs:
                return None
            run = runs[0]
            claims = await self._claims_for_run(session, run)
        if run.status in NON_TERMINAL_STATUSES:
            return CallExecutionResult(run=run, pending=True)
        validation_failed = bool(
            run.structured_result is not None
            and intent.result_schema
            and validate_result_against_schema(run.structured_result, intent.result_schema)
        )
        return CallExecutionResult(
            run=run, claims=claims, result_validation_failed=validation_failed
        )

    @staticmethod
    async def _claims_for_run(session: Any, run: CallRun) -> list[EvidenceClaim]:
        return [
            c
            for c in await EvidenceClaimRepository(session).list_by_mission(run.mission_id)
            if c.source_reference.split(";", 1)[0] == f"call_run:{run.id}"
        ]

    async def execute_call(
        self, intent_id: str, *, advance_mission: bool = True
    ) -> CallExecutionResult:
        """Execute one authorized intent.

        The mission enters ``CALL_EXECUTION_RUNNING`` (or is already there
        when a multi-call pattern is mid-round) and, with ``advance_mission``,
        moves to ``CALL_RESULT_RECEIVED`` once a terminal run is recorded. A
        pattern that places several calls in one round passes
        ``advance_mission=False`` and calls :meth:`mark_results_received`
        once at the end.
        """
        intent, mission = await self._load(intent_id)
        existing = await self._existing(intent)
        if existing is not None:
            logger.info("intent %s already executed as run %s", intent.id, existing.run.id)
            return existing
        approval = await self._approvals.latest_for_intent(intent.id)
        # Pre-check so a refusal leaves the mission where it was.
        await self._gate.check(
            intent, approval, self._settings, mission, simulated=self.provider.is_simulated
        )
        assert approval is not None  # the gate raised otherwise
        plan = await self.provider.plan_call(intent)
        authorized = await self.provider.authorize(plan, approval)

        if mission.status is not MissionStatus.CALL_EXECUTION_RUNNING:
            mission = await self._machine.propose_transition(
                mission, MissionStatus.CALL_EXECUTION_RUNNING, f"executing call intent {intent.id}"
            )
        await self._emit(
            mission.id,
            f"{'Simulated call' if self.provider.is_simulated else 'Call'} started: "
            f"{intent.purpose}",
            call_intent_id=intent.id,
            provider=self.provider.name,
            simulated=self.provider.is_simulated,
            status=CallStatus.QUEUED.value,
        )
        try:
            run = await self.provider.execute(authorized)
        except AmbiguousCreate as exc:
            # Transient: CALL-E may or may not hold the task. Reconcile by an
            # identical replay under the same key; never a fresh create.
            await self._emit(
                mission.id,
                "The call request did not get a definitive answer; reconciling by identical "
                "replay under the same idempotency key (never a fresh dial).",
                call_intent_id=intent.id,
                reason=exc.reason,
            )
            try:
                run = await self.provider.reconcile(intent)
            except CallProviderError as inner:
                await self._block(mission, inner, "call reconciliation failed")
                raise
            await self._emit(
                mission.id,
                "Reconciled: CALL-E returned the original call task for the same key.",
                call_intent_id=intent.id,
                calle_call_id=run.calle_call_id,
                status=run.status.value,
            )
        except CallProviderError as exc:
            # The in-provider gate refused (or the provider failed) after the
            # mission entered execution; the only lawful exits are forward or
            # abort, and no result exists, so the mission blocks with the reason.
            await self._block(mission, exc, "call execution refused")
            raise

        stored = await self._persist_run(run, intent, mission)
        if stored.status in NON_TERMINAL_STATUSES:
            await self._emit(
                mission.id,
                f"Call {stored.status.value}: {intent.purpose}. Waiting for the terminal result.",
                call_intent_id=intent.id,
                call_run_id=stored.id,
                status=stored.status.value,
                simulated=stored.is_simulated,
            )
            return CallExecutionResult(run=stored, pending=True)
        claims, validation_failed = await self._finalize(stored, intent, mission)
        if advance_mission:
            await self.mark_results_received(
                mission.id, f"call run {stored.id} {stored.status.value}"
            )
        return CallExecutionResult(
            run=stored, claims=claims, result_validation_failed=validation_failed
        )

    async def mark_results_received(self, mission_id: str, trigger: str) -> Mission:
        """``CALL_EXECUTION_RUNNING → CALL_RESULT_RECEIVED`` if the mission is
        there; otherwise leave it untouched."""
        async with self._database.session() as session:
            mission = await MissionRepository(session).get(mission_id)
        if mission is None:
            raise KeyError(f"mission {mission_id!r} not found")
        if mission.status is MissionStatus.CALL_EXECUTION_RUNNING:
            return await self._machine.propose_transition(
                mission, MissionStatus.CALL_RESULT_RECEIVED, trigger
            )
        return mission

    async def finalize_run(
        self, calle_call_id: str, *, event_type: WebhookEventType | None = None
    ) -> CallExecutionResult:
        """Complete a pending run from a fresh, authoritative ``get_status``.

        Used by the webhook receiver (CS-036) and any poller. The caller's
        payload is a notification only: nothing from it is written. If the
        re-read is still non-terminal nothing changes. ``event_type``
        ``call.result_validation_failed`` records the run without a
        structured result even if the re-read carries one, so the summary and
        evidence become low-confidence claims and gaps stay UNKNOWN.
        """
        async with self._database.session() as session:
            stored = await CallRunRepository(session).get_by_calle_call_id(calle_call_id)
            if stored is None:
                raise KeyError(f"no call run for CALL-E call {calle_call_id!r}")
            existing = await self._claims_for_run(session, stored)
            intent = await CallIntentRepository(session).get(stored.call_intent_id)
            mission = await MissionRepository(session).get(stored.mission_id)
        if intent is None or mission is None:
            raise KeyError(f"call run {stored.id!r} has no intent or mission")
        if stored.status not in NON_TERMINAL_STATUSES and existing:
            return CallExecutionResult(run=stored, claims=existing)

        fresh = await self.provider.get_status(calle_call_id)
        if fresh.status in NON_TERMINAL_STATUSES:
            return CallExecutionResult(run=stored, pending=True)
        force_unstructured = event_type is WebhookEventType.CALL_RESULT_VALIDATION_FAILED
        merged = stored.model_copy(
            update={
                "status": fresh.status,
                "started_at": stored.started_at or fresh.started_at,
                "completed_at": fresh.completed_at or utcnow(),
                "structured_result": None if force_unstructured else fresh.structured_result,
                "summary": fresh.summary,
                "task_completed": fresh.task_completed,
                "recipient_results": [
                    r.model_copy(update={"call_run_id": stored.id}) for r in fresh.recipient_results
                ],
                "transcript_reference": fresh.transcript_reference,
                "evidence": list(fresh.evidence),
                "confidence": fresh.confidence,
                "failure_code": fresh.failure_code,
                "failure_message": fresh.failure_message,
            }
        )
        # Atomic promotion: one conditional UPDATE guarded on the run still
        # being non-terminal. Two concurrent deliveries (project-level and
        # per-request webhooks carry different event ids) both reach this
        # point; exactly one wins the row and writes claims and events, the
        # other returns what the winner stored.
        async with self._database.session() as session:
            promoted = await CallRunRepository(session).promote_to_terminal(
                merged, [s.value for s in NON_TERMINAL_STATUSES]
            )
        if promoted is None:
            return await self._already_finalized(calle_call_id)
        claims, validation_failed = await self._finalize(promoted, intent, mission)
        await self.mark_results_received(
            mission.id, f"call run {promoted.id} {promoted.status.value}"
        )
        return CallExecutionResult(
            run=promoted, claims=claims, result_validation_failed=validation_failed
        )

    async def _already_finalized(self, calle_call_id: str) -> CallExecutionResult:
        """Another finalizer won the promotion; return its stored outcome."""
        async with self._database.session() as session:
            stored = await CallRunRepository(session).get_by_calle_call_id(calle_call_id)
            assert stored is not None
            claims = await self._claims_for_run(session, stored)
        return CallExecutionResult(
            run=stored, claims=claims, pending=stored.status in NON_TERMINAL_STATUSES
        )

    async def _block(self, mission: Mission, exc: CallProviderError, trigger: str) -> None:
        blocked = mission.model_copy(update={"blocker": str(exc), "updated_at": utcnow()})
        async with self._database.session() as session:
            await MissionRepository(session).update(blocked)
        await self._machine.propose_transition(
            blocked, MissionStatus.BLOCKED, f"{trigger}: {type(exc).__name__}"
        )

    async def _persist_run(self, run: CallRun, intent: CallIntent, mission: Mission) -> CallRun:
        """Store the run and count it against the budget. A dial happened
        whether or not the result is in yet."""
        async with self._database.session() as session:
            stored = await CallRunRepository(session).add(run)
            missions = MissionRepository(session)
            fresh = await missions.get(mission.id)
            if fresh is not None:
                budget = CallBudget(
                    max_calls=fresh.call_budget.max_calls,
                    calls_used=fresh.call_budget.calls_used + 1,
                )
                await missions.update(
                    fresh.model_copy(update={"call_budget": budget, "updated_at": utcnow()})
                )
        return stored

    async def _finalize(
        self, stored: CallRun, intent: CallIntent, mission: Mission
    ) -> tuple[list[EvidenceClaim], bool]:
        """Turn a persisted terminal run into claims and events."""
        validation_errors: list[str] = []
        if stored.structured_result is not None and intent.result_schema:
            validation_errors = validate_result_against_schema(
                stored.structured_result, intent.result_schema
            )
        result_valid = not validation_errors
        subject, recipient_subjects = await self._subjects_for(intent)
        claims = claims_from_run(
            stored,
            intent,
            subject,
            result_valid=result_valid,
            recipient_subjects=recipient_subjects,
        )
        async with self._database.session() as session:
            claim_repo = EvidenceClaimRepository(session)
            claims = [await claim_repo.add(c) for c in claims]
        for status in (CallStatus.IN_PROGRESS, stored.status):
            await self._emit(
                mission.id,
                f"{'Simulated call' if stored.is_simulated else 'Call'} {status.value}: "
                f"{intent.purpose}",
                call_intent_id=intent.id,
                call_run_id=stored.id,
                status=status.value,
                simulated=stored.is_simulated,
            )
        source = SourceType.SIMULATED if stored.is_simulated else SourceType.PHONE
        if stored.structured_result is None:
            await self._emit(
                mission.id,
                "No schema-valid structured result; the related gaps stay UNKNOWN. Summary and "
                "evidence were stored as low-confidence claims.",
                call_intent_id=intent.id,
                call_run_id=stored.id,
                result_validation_failed=bool(validation_errors),
            )
        elif validation_errors:
            await self._emit(
                mission.id,
                "The returned structured result did not match the call's schema; it was not "
                "turned into attribute claims. Summary stored as low-confidence.",
                call_intent_id=intent.id,
                call_run_id=stored.id,
                validation_error_count=len(validation_errors),
            )
        await self._emit(
            mission.id,
            f"Recorded {len(claims)} claim(s) from the call, source {source.value}, status "
            f"{EvidenceStatus.PHONE_SUPPORTED.value} for structured answers.",
            call_intent_id=intent.id,
            call_run_id=stored.id,
            source_type=source.value,
            claim_count=len(claims),
        )
        return claims, bool(validation_errors)
