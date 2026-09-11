"""The CallExecutionProvider protocol, its typed errors and the gated base class.

The surface mirrors ``04_CALL_E_INTEGRATION.md`` exactly:

.. code-block:: text

    plan_call(intent)                          -> CallPlan
    authorize(plan, approval)                  -> AuthorizedPlan
    execute(plan)                              -> CallRun
    get_status(calle_call_id)                  -> CallRun
    get_events(calle_call_id, cursor, limit)   -> EventPage
    get_result(calle_call_id)                  -> CallResult
    reconcile(intent)                          -> CallRun
    cancel_local(intent)                       -> CallIntent

Two contract facts shape this module:

* **Reconciliation is replay, not retry.** CALL-E exposes no list, search or
  metadata lookup. After an ambiguous create the only supported recovery is to
  re-send ``POST /v1/calls`` with a byte-identical body under the identical
  deterministic ``Idempotency-Key`` (:func:`idempotency_key`); CALL-E then
  returns the original call task instead of dialing again.
* **There is no remote cancel.** ``cancel_local`` cancels an unexecuted plan
  or an unfired scheduled job only. An in-flight call cannot be recalled.

``execute`` on every provider runs the :class:`CallGateProtocol` *inside* the
provider before anything else happens. The gate is a constructor dependency of
:class:`GatedCallProvider`, so a caller that bypasses ``approvals/`` still
cannot dial: the base class reloads the intent, approval and mission from the
database by id and evaluates every gate against those authoritative rows, not
against whatever objects the caller holds.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Protocol, final, runtime_checkable

from pydantic import Field

from callswarm.config.settings import Settings
from callswarm.models import (
    Approval,
    ApprovalStatus,
    ApprovalSubjectType,
    CallAuthorizationState,
    CallIntent,
    CallRecipient,
    CallRun,
    CallStatus,
    CompletionConfidence,
    DomainModel,
    Mission,
    RecipientResult,
    ScheduledJobStatus,
    utcnow,
)
from callswarm.persistence import (
    ApprovalRepository,
    CallIntentRepository,
    Database,
    MissionRepository,
    ScheduledJobRepository,
)

IDEMPOTENCY_KEY_MAX_LENGTH = 255
CALL_INTENT_JOB_KEY = "call_intent_id"
NON_TERMINAL_STATUSES: frozenset[CallStatus] = frozenset(
    {CallStatus.QUEUED, CallStatus.IN_PROGRESS}
)
TERMINAL_STATUSES: frozenset[CallStatus] = frozenset(
    {CallStatus.COMPLETED, CallStatus.FAILED, CallStatus.CANCELED}
)


# --- errors ----------------------------------------------------------------------------


class CallProviderError(Exception):
    """Base class for every call-layer error."""


class CallProviderNotAvailable(CallProviderError):
    """The configured provider cannot be constructed. Never falls back to fake."""


class CallNotAuthorized(CallProviderError):
    """No usable ``APPROVED`` approval exists for the intent, or policy forbids calls."""


class LiveCallsDisabled(CallProviderError):
    """A live provider was asked to dial while ``CALLE_LIVE_CALLS_ENABLED`` is false."""


class RecipientNotAllowed(CallProviderError):
    """The recipient is not on ``CALL_ALLOWED_RECIPIENTS`` (empty list = allow none)."""


class CallBudgetExceeded(CallProviderError):
    """The mission has no call budget left."""


class QuietHours(CallProviderError):
    """The recipient's local time is inside the quiet-hours window, or unknown."""


class RecipientSuppressed(CallProviderError):
    """The recipient is on the do-not-contact list."""


class IntentAlreadyExecuted(CallProviderError):
    """``cancel_local`` was asked to cancel an intent that already produced a run."""


GATE_ERRORS: tuple[type[CallProviderError], ...] = (
    CallNotAuthorized,
    LiveCallsDisabled,
    RecipientNotAllowed,
    CallBudgetExceeded,
    QuietHours,
    RecipientSuppressed,
)


# --- plan and result models ----------------------------------------------------------


def idempotency_key(intent: CallIntent) -> str:
    """Deterministic ``Idempotency-Key`` for one intent.

    Derived from the intent id and a digest of the request-defining fields, so
    an identical replay carries the identical key and a *changed* request never
    silently reuses a key. Always well under CALL-E's 255-character limit.
    """
    material = "|".join(
        [
            intent.id,
            intent.call_goal,
            ",".join(sorted(r.phone_e164 for r in intent.recipients)),
        ]
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    key = f"callswarm-{intent.id}-{digest}"
    assert len(key) <= IDEMPOTENCY_KEY_MAX_LENGTH
    return key


class CallPlan(DomainModel):
    """A validated, executable description of one call. Not yet authorized."""

    mission_id: str
    call_intent_id: str
    task: str = Field(min_length=1)
    recipients: list[CallRecipient] = Field(min_length=1)
    result_schema: dict[str, Any] | None = None
    recipient_result_schema: dict[str, Any] | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=IDEMPOTENCY_KEY_MAX_LENGTH)
    estimated_cost_units: int = Field(default=1, ge=1, description="One unit per recipient")
    created_at: datetime = Field(default_factory=utcnow)


class AuthorizedPlan(DomainModel):
    """A plan paired with the id of the approval that was checked for it.

    Only the *id* is carried: ``execute`` reloads the approval row and checks
    it again, so a forged in-memory approval buys nothing.
    """

    plan: CallPlan
    approval_id: str = Field(min_length=1)
    authorized_at: datetime = Field(default_factory=utcnow)


class CallEvent(DomainModel):
    """One developer event from the provider's event stream."""

    id: str
    calle_call_id: str
    event_type: str
    created_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class EventPage(DomainModel):
    events: list[CallEvent] = Field(default_factory=list)
    next_cursor: str | None = None


class CallResult(DomainModel):
    """The terminal outcome of a call as the provider reports it.

    ``structured_result`` is ``None`` when no schema-valid result exists; that
    is an explicit outcome (gaps stay ``UNKNOWN``), never a failure to hide.
    """

    calle_call_id: str
    status: CallStatus
    structured_result: dict[str, Any] | None = None
    summary: str = ""
    evidence: list[str] = Field(default_factory=list)
    confidence: CompletionConfidence | None = None
    task_completed: bool | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    recipient_results: list[RecipientResult] = Field(default_factory=list)
    is_simulated: bool


# --- gate protocol -------------------------------------------------------------------


@runtime_checkable
class CallGateProtocol(Protocol):
    """What a provider needs from the gate. Implemented by ``calls/gates.py``."""

    async def check(
        self,
        intent: CallIntent,
        approval: Approval | None,
        settings: Settings,
        mission: Mission,
        now: datetime | None = None,
        *,
        simulated: bool,
    ) -> None: ...


# --- provider protocol -----------------------------------------------------------------


@runtime_checkable
class CallExecutionProvider(Protocol):
    name: str
    is_simulated: bool

    async def plan_call(self, intent: CallIntent) -> CallPlan: ...

    async def authorize(self, plan: CallPlan, approval: Approval) -> AuthorizedPlan: ...

    async def execute(self, plan: AuthorizedPlan) -> CallRun: ...

    async def get_status(self, calle_call_id: str) -> CallRun: ...

    async def get_events(
        self, calle_call_id: str, cursor: str | None = None, limit: int = 50
    ) -> EventPage: ...

    async def get_result(self, calle_call_id: str) -> CallResult: ...

    async def reconcile(self, intent: CallIntent) -> CallRun: ...

    async def cancel_local(self, intent: CallIntent) -> CallIntent: ...


# --- gated base class --------------------------------------------------------------------


class GatedCallProvider(ABC):
    """Base for every provider: ``execute`` is final and runs the gate first.

    Subclasses implement :meth:`_execute_authorized`, which is only ever
    reached after the gate has passed against freshly loaded rows.
    """

    name: str = "abstract"
    is_simulated: bool = True

    _FINAL_METHODS: tuple[str, ...] = ("execute", "_load_for_gate")

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        overridden = [name for name in cls._FINAL_METHODS if name in cls.__dict__]
        if overridden:
            raise TypeError(
                f"{cls.__name__} may not override {', '.join(overridden)}: the gate runs there"
            )

    def __init__(self, gate: CallGateProtocol, settings: Settings, database: Database) -> None:
        self._gate = gate
        self._settings = settings
        self._database = database

    # --- shared behaviour -------------------------------------------------------
    async def authorize(self, plan: CallPlan, approval: Approval) -> AuthorizedPlan:
        """Pair a plan with an approval that is APPROVED, unexpired and for this intent.

        This is a convenience pre-check; ``execute`` reloads and re-checks.
        """
        _require_matching_approval(plan, approval, utcnow())
        return AuthorizedPlan(plan=plan, approval_id=approval.id)

    @final
    async def execute(self, plan: AuthorizedPlan) -> CallRun:
        intent, approval, mission = await self._load_for_gate(plan)
        await self._gate.check(
            intent, approval, self._settings, mission, simulated=self.is_simulated
        )
        return await self._execute_authorized(plan, intent)

    @final
    async def _load_for_gate(
        self, plan: AuthorizedPlan
    ) -> tuple[CallIntent, Approval | None, Mission]:
        async with self._database.session() as session:
            intent = await CallIntentRepository(session).get(plan.plan.call_intent_id)
            approval = await ApprovalRepository(session).get(plan.approval_id)
            mission = await MissionRepository(session).get(plan.plan.mission_id)
        if intent is None:
            raise CallNotAuthorized(f"call intent {plan.plan.call_intent_id!r} does not exist")
        if mission is None:
            raise CallNotAuthorized(f"mission {plan.plan.mission_id!r} does not exist")
        if intent.mission_id != mission.id:
            raise CallNotAuthorized("call intent does not belong to the plan's mission")
        return intent, approval, mission

    @abstractmethod
    async def _execute_authorized(self, plan: AuthorizedPlan, intent: CallIntent) -> CallRun:
        """Perform the call. Reached only after the gate passed."""

    @abstractmethod
    async def plan_call(self, intent: CallIntent) -> CallPlan: ...

    @abstractmethod
    async def get_status(self, calle_call_id: str) -> CallRun: ...

    @abstractmethod
    async def get_events(
        self, calle_call_id: str, cursor: str | None = None, limit: int = 50
    ) -> EventPage: ...

    @abstractmethod
    async def get_result(self, calle_call_id: str) -> CallResult: ...

    @abstractmethod
    async def reconcile(self, intent: CallIntent) -> CallRun: ...

    @abstractmethod
    async def cancel_local(self, intent: CallIntent) -> CallIntent: ...


def _require_matching_approval(plan: CallPlan, approval: Approval, now: datetime) -> None:
    if approval.subject_type is not ApprovalSubjectType.CALL_INTENT:
        raise CallNotAuthorized("approval is not for a call intent")
    if approval.subject_id != plan.call_intent_id or approval.mission_id != plan.mission_id:
        raise CallNotAuthorized("approval is for a different call intent")
    if approval.status is not ApprovalStatus.APPROVED:
        raise CallNotAuthorized(f"approval is {approval.status.value}, not APPROVED")
    if approval.expires_at is not None and approval.expires_at <= now:
        raise CallNotAuthorized("approval is EXPIRED")


async def cancel_intent_locally(database: Database, intent: CallIntent) -> CallIntent:
    """Shared ``cancel_local`` body: block an unexecuted intent and cancel any
    unfired scheduled job that points at it. Callers check for an executed run
    first; there is no remote cancel and an executed intent cannot be recalled."""
    async with database.session() as session:
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
