"""Provider selection and call execution (CS-031 / CS-034).

``select_call_provider`` honours ``CALL_PROVIDER``: ``fake`` builds the
:class:`FakeCallProvider`; ``calle`` raises :class:`CallProviderNotAvailable`
until CS-032 lands. It never falls back — a fake call presented as real is
worse than a hard startup error.

``CallService.execute_call(intent_id)`` is the one path from an authorized
intent to evidence:

    load intent, latest approval, mission
    → gate pre-check (raises cleanly; mission stays CALL_AUTHORIZED)
    → provider.plan_call → provider.authorize
    → mission CALL_AUTHORIZED → CALL_EXECUTION_RUNNING
    → provider.execute (the gate runs again inside the provider)
    → persist CallRun, count it against the budget
    → mission → CALL_RESULT_RECEIVED
    → result → EvidenceClaims

Claims from a simulated run are ``source_type=SIMULATED``; from a real run
``PHONE``. Either way the status is ``PHONE_SUPPORTED``: a phone claim records
what was *said*, never verified truth. A ``structured_result`` of ``None``
yields no attribute claims (gaps stay ``UNKNOWN``); the summary and
``evidence[]`` are stored as low-confidence claims labelled as such.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import Field

from callswarm.approvals import ApprovalService
from callswarm.calls.fake import FakeCallProvider
from callswarm.calls.gates import CallGate
from callswarm.calls.provider import (
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
        raise CallProviderNotAvailable(
            "CALL_PROVIDER=calle is selected but the CALL-E provider (CS-032) is not implemented "
            "in this build. Refusing to start rather than substituting the fake provider: set "
            "CALL_PROVIDER=fake for simulated calls."
        )
    raise CallProviderNotAvailable(f"unknown CALL_PROVIDER {settings.call_provider!r}")


class CallExecutionResult(DomainModel):
    run: CallRun
    claims: list[EvidenceClaim] = Field(default_factory=list)
    result_validation_failed: bool = False


def _flatten(value: Any) -> Any:
    """Claim values are JSON scalars or JSON text for structures."""
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def claims_from_run(
    run: CallRun, intent: CallIntent, subject: str, *, result_valid: bool
) -> list[EvidenceClaim]:
    """Convert a terminal run into claims. Pure; used by the service and tests."""
    source_type = SourceType.SIMULATED if run.is_simulated else SourceType.PHONE
    reference = f"call_run:{run.id}"
    entity_id = intent.recipients[0].entity_id if intent.recipients else None
    claims: list[EvidenceClaim] = []
    if run.structured_result is not None and result_valid:
        for key, value in run.structured_result.items():
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

    async def _subject_for(self, intent: CallIntent) -> str:
        entity_id = intent.recipients[0].entity_id if intent.recipients else None
        if entity_id is not None:
            async with self._database.session() as session:
                candidate = await CandidateEntityRepository(session).get(entity_id)
            if candidate is not None:
                return candidate.display_name
        return f"call_intent:{intent.id}"

    async def _existing(self, intent: CallIntent) -> CallExecutionResult | None:
        """An intent executes once. A repeat call is an idempotent no-op that
        returns the stored run and its claims: no dial, no budget increment,
        no duplicate claims."""
        async with self._database.session() as session:
            runs = await CallRunRepository(session).list_by_intent(intent.id)
            if not runs:
                return None
            run = runs[0]
            claims = [
                c
                for c in await EvidenceClaimRepository(session).list_by_mission(run.mission_id)
                if c.source_reference.split(";", 1)[0] == f"call_run:{run.id}"
            ]
        validation_failed = bool(
            run.structured_result is not None
            and intent.result_schema
            and validate_result_against_schema(run.structured_result, intent.result_schema)
        )
        return CallExecutionResult(
            run=run, claims=claims, result_validation_failed=validation_failed
        )

    async def execute_call(self, intent_id: str) -> CallExecutionResult:
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
        except CallProviderError as exc:
            # The in-provider gate refused (or the provider failed) after the
            # mission entered execution; the only lawful exits are forward or
            # abort, and no result exists, so the mission blocks with the reason.
            blocked = mission.model_copy(update={"blocker": str(exc), "updated_at": utcnow()})
            async with self._database.session() as session:
                await MissionRepository(session).update(blocked)
            await self._machine.propose_transition(
                blocked, MissionStatus.BLOCKED, f"call execution refused: {type(exc).__name__}"
            )
            raise

        run, claims, validation_failed = await self._record(run, intent, mission)
        mission = await self._machine.propose_transition(
            mission, MissionStatus.CALL_RESULT_RECEIVED, f"call run {run.id} {run.status.value}"
        )
        return CallExecutionResult(
            run=run, claims=claims, result_validation_failed=validation_failed
        )

    async def _record(
        self, run: CallRun, intent: CallIntent, mission: Mission
    ) -> tuple[CallRun, list[EvidenceClaim], bool]:
        validation_errors: list[str] = []
        if run.structured_result is not None and intent.result_schema:
            validation_errors = validate_result_against_schema(
                run.structured_result, intent.result_schema
            )
        result_valid = not validation_errors
        subject = await self._subject_for(intent)
        claims = claims_from_run(run, intent, subject, result_valid=result_valid)
        async with self._database.session() as session:
            stored = await CallRunRepository(session).add(run)
            claim_repo = EvidenceClaimRepository(session)
            claims = [await claim_repo.add(c) for c in claims]
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
        return stored, claims, bool(validation_errors)
