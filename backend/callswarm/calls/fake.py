"""FakeCallProvider: the default provider. Simulated, offline, loudly labelled.

* ``is_simulated`` is a class constant ``True`` and every :class:`CallRun` it
  produces carries ``is_simulated=True``; every claim derived from one is
  ``source_type=SIMULATED`` (see ``calls/service.py``).
* It performs no I/O of any kind. Runs live in memory for the lifetime of the
  provider instance; the service persists what it needs.
* It walks the real CALL-E status sequence ``queued → in_progress →
  completed|failed`` and records one event per step, so ``get_events`` and the
  UI behave as they will against the real provider.
* Results are scripted with :meth:`script` — per intent id or FIFO — as a
  full structured result, ``structured_result=None`` (no schema-valid result),
  a ``failed`` call, or a ``result_validation_failed`` equivalent (the call
  completed, the result did not validate, only summary/evidence survive).
  Without a script the result is ``None`` with an explicit "no scripted
  result" summary: the fake never invents an answer.
* ``execute`` is inherited from :class:`GatedCallProvider` and runs the gate
  first; ``reconcile`` replays by idempotency key; ``cancel_local`` refuses an
  intent that already ran.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from callswarm.calls.provider import (
    AuthorizedPlan,
    CallEvent,
    CallGateProtocol,
    CallPlan,
    CallProviderError,
    CallResult,
    EventPage,
    GatedCallProvider,
    IntentAlreadyExecuted,
    idempotency_key,
)
from callswarm.calls.schema import validate_result_against_schema, validate_result_schema
from callswarm.config.settings import Settings
from callswarm.models import (
    CallAuthorizationState,
    CallIntent,
    CallRun,
    CallStatus,
    CompletionConfidence,
    DomainModel,
    RecipientResult,
    RecipientStatus,
    ScheduledJobStatus,
    new_id,
    utcnow,
)
from callswarm.persistence import CallIntentRepository, Database, ScheduledJobRepository
from callswarm.sanitize import mask_phone

FakeOutcome = Literal["completed", "failed", "result_validation_failed"]

SIMULATED_SUMMARY_PREFIX = "[SIMULATED]"
NO_SCRIPT_SUMMARY = f"{SIMULATED_SUMMARY_PREFIX} No scripted result; nothing was established."
CALL_INTENT_JOB_KEY = "call_intent_id"


class FakeScript(DomainModel):
    """One scripted outcome for the fake provider."""

    outcome: FakeOutcome = "completed"
    structured_result: dict[str, Any] | None = None
    summary: str = ""
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    failure_code: str | None = None
    failure_message: str | None = None


class FakeCallProvider(GatedCallProvider):
    name = "fake"
    is_simulated = True

    def __init__(self, gate: CallGateProtocol, settings: Settings, database: Database) -> None:
        super().__init__(gate, settings, database)
        self._by_intent: dict[str, deque[FakeScript]] = {}
        self._fifo: deque[FakeScript] = deque()
        self._runs: dict[str, CallRun] = {}
        self._events: dict[str, list[CallEvent]] = {}
        self._by_key: dict[str, str] = {}
        self._results: dict[str, CallResult] = {}

    # --- scripting ----------------------------------------------------------------
    def script(self, script: FakeScript, *, intent_id: str | None = None) -> None:
        if intent_id is None:
            self._fifo.append(script)
        else:
            self._by_intent.setdefault(intent_id, deque()).append(script)

    def _next_script(self, intent_id: str) -> FakeScript:
        queue = self._by_intent.get(intent_id)
        if queue:
            return queue.popleft()
        if self._fifo:
            return self._fifo.popleft()
        return FakeScript(outcome="completed", structured_result=None, summary=NO_SCRIPT_SUMMARY)

    @property
    def executed_intent_ids(self) -> list[str]:
        return [run.call_intent_id for run in self._runs.values()]

    # --- protocol --------------------------------------------------------------------
    async def plan_call(self, intent: CallIntent) -> CallPlan:
        schema = intent.result_schema or None
        if schema is not None:
            validate_result_schema(schema)
        recipient_schema = intent.recipient_result_schema or None
        if recipient_schema is not None:
            validate_result_schema(recipient_schema)
        return CallPlan(
            mission_id=intent.mission_id,
            call_intent_id=intent.id,
            task=intent.call_goal,
            recipients=list(intent.recipients),
            result_schema=schema,
            recipient_result_schema=recipient_schema,
            metadata={"mission_id": intent.mission_id, "call_intent_id": intent.id},
            idempotency_key=idempotency_key(intent),
            estimated_cost_units=len(intent.recipients),
        )

    async def _execute_authorized(self, plan: AuthorizedPlan, intent: CallIntent) -> CallRun:
        key = plan.plan.idempotency_key
        if key in self._by_key:
            # Same key, same request: the original run is returned, nothing dials twice.
            return self._runs[self._by_key[key]]
        script = self._next_script(intent.id)
        calle_call_id = f"sim-{new_id()}"
        started = utcnow()
        events: list[CallEvent] = []
        for status in (CallStatus.QUEUED, CallStatus.IN_PROGRESS):
            events.append(self._event(calle_call_id, status, started))
        result = self._terminal_result(calle_call_id, script, plan.plan, intent)
        events.append(self._event(calle_call_id, result.status, utcnow()))
        run = CallRun(
            mission_id=intent.mission_id,
            call_intent_id=intent.id,
            calle_call_id=calle_call_id,
            status=result.status,
            recipient_masked=", ".join(mask_phone(r.phone_e164) for r in intent.recipients),
            started_at=started,
            completed_at=utcnow(),
            structured_result=result.structured_result,
            summary=result.summary,
            task_completed=result.task_completed,
            recipient_results=result.recipient_results,
            evidence=list(result.evidence),
            confidence=result.confidence,
            failure_code=result.failure_code,
            failure_message=result.failure_message,
            is_simulated=True,
        )
        self._runs[calle_call_id] = run
        self._events[calle_call_id] = events
        self._results[calle_call_id] = result
        self._by_key[key] = calle_call_id
        return run

    def _terminal_result(
        self, calle_call_id: str, script: FakeScript, plan: CallPlan, intent: CallIntent
    ) -> CallResult:
        summary = script.summary or NO_SCRIPT_SUMMARY
        if not summary.startswith(SIMULATED_SUMMARY_PREFIX):
            summary = f"{SIMULATED_SUMMARY_PREFIX} {summary}"
        if script.outcome == "failed":
            return CallResult(
                calle_call_id=calle_call_id,
                status=CallStatus.FAILED,
                structured_result=None,
                summary=summary,
                evidence=list(script.evidence),
                task_completed=False,
                failure_code=script.failure_code or "simulated_failure",
                failure_message=script.failure_message or "scripted failure",
                recipient_results=self._recipient_results(intent, RecipientStatus.FAILED, None),
                is_simulated=True,
            )
        structured = script.structured_result
        if script.outcome == "result_validation_failed":
            structured = None
        elif structured is not None and plan.result_schema is not None:
            # The real provider only returns schema-valid results; mirror that.
            if validate_result_against_schema(structured, plan.result_schema):
                structured = None
        elif structured is not None and plan.result_schema is None:
            structured = None
        return CallResult(
            calle_call_id=calle_call_id,
            status=CallStatus.COMPLETED,
            structured_result=structured,
            summary=summary,
            evidence=list(script.evidence),
            confidence=CompletionConfidence(score=script.confidence, label="simulated"),
            task_completed=structured is not None,
            recipient_results=self._recipient_results(
                intent, RecipientStatus.COMPLETED, structured
            ),
            is_simulated=True,
        )

    @staticmethod
    def _recipient_results(
        intent: CallIntent, status: RecipientStatus, structured: dict[str, Any] | None
    ) -> list[RecipientResult]:
        return [
            RecipientResult(
                recipient_ref=recipient.entity_id or f"recipient-{index}",
                phone_masked=mask_phone(recipient.phone_e164),
                status=status,
                structured_result=structured if index == 0 else None,
                summary="",
            )
            for index, recipient in enumerate(intent.recipients)
        ]

    @staticmethod
    def _event(calle_call_id: str, status: CallStatus, at: datetime) -> CallEvent:
        return CallEvent(
            id=f"evt-{new_id()}",
            calle_call_id=calle_call_id,
            event_type=f"call.{status.value}",
            created_at=at,
            data={"status": status.value, "simulated": True},
        )

    def _run(self, calle_call_id: str) -> CallRun:
        run = self._runs.get(calle_call_id)
        if run is None:
            raise CallProviderError(f"unknown simulated call {calle_call_id!r}")
        return run

    async def get_status(self, calle_call_id: str) -> CallRun:
        return self._run(calle_call_id)

    async def get_events(
        self, calle_call_id: str, cursor: str | None = None, limit: int = 50
    ) -> EventPage:
        self._run(calle_call_id)
        events = self._events[calle_call_id]
        start = int(cursor) if cursor else 0
        page = events[start : start + max(1, min(limit, 100))]
        next_cursor = str(start + len(page)) if start + len(page) < len(events) else None
        return EventPage(events=page, next_cursor=next_cursor)

    async def get_result(self, calle_call_id: str) -> CallResult:
        self._run(calle_call_id)
        return self._results[calle_call_id]

    async def reconcile(self, intent: CallIntent) -> CallRun:
        """Replay under the intent's idempotency key. Returns the original run
        if one exists; raises if nothing was ever created, because a replay of
        a create that never happened would be a fresh dial."""
        key = idempotency_key(intent)
        if key not in self._by_key:
            raise CallProviderError(
                f"no run exists under the idempotency key for intent {intent.id!r}; "
                "reconcile replays a create that was attempted, it never starts one"
            )
        return self._runs[self._by_key[key]]

    async def cancel_local(self, intent: CallIntent) -> CallIntent:
        """Cancel an intent that has not executed and any unfired job for it.
        There is no remote cancel; an executed intent cannot be recalled."""
        if intent.id in self.executed_intent_ids:
            raise IntentAlreadyExecuted(
                f"intent {intent.id!r} has already executed; CALL-E exposes no cancel"
            )
        async with self._database.session() as session:
            intents = CallIntentRepository(session)
            stored = await intents.get(intent.id)
            if stored is None:
                raise CallProviderError(f"intent {intent.id!r} not found")
            updated = await intents.update(
                stored.model_copy(
                    update={
                        "authorization_state": CallAuthorizationState.BLOCKED,
                        "rejection_reason": "canceled locally before execution",
                        "updated_at": utcnow(),
                    }
                )
            )
            jobs = ScheduledJobRepository(session)
            for job in await jobs.list_by_mission(intent.mission_id):
                if (
                    job.status is ScheduledJobStatus.PENDING
                    and job.payload.get(CALL_INTENT_JOB_KEY) == intent.id
                ):
                    await jobs.update(
                        job.model_copy(
                            update={
                                "status": ScheduledJobStatus.CANCELED,
                                "status_reason": "call intent canceled locally",
                                "updated_at": utcnow(),
                            }
                        )
                    )
        return updated
