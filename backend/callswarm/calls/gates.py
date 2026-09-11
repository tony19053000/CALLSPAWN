"""CallGate: every check that stands between an intent and a dial (CS-034).

Evaluated in this order, and the first failure raises:

1. **Live switch** — a non-simulated provider requires
   ``CALLE_LIVE_CALLS_ENABLED=true`` and ``CALL_PROVIDER=calle``. The fake
   provider skips this one check so the whole flow can be rehearsed offline;
   it still needs everything below, including an ``APPROVED`` approval.
2. **Approval** — must exist, be for this intent, be ``APPROVED`` and not
   expired. ``PENDING``, ``REJECTED`` and ``EXPIRED`` each raise
   :class:`CallNotAuthorized` naming the state.
3. **Policy** — ``authority_policy.calls_allowed``.
4. **Allow-list** — live only. ``CALL_ALLOWED_RECIPIENTS`` is a hard
   allow-list; an empty list permits no recipient at all.
5. **Suppression** — the persisted do-not-contact list, by phone hash, for
   every recipient, simulated or not.
6. **Quiet hours** — in the recipient's region. A missing or unknown region
   is refused when live; the fake provider skips a region it cannot resolve.
7. **Budget** — executed ``CallRun`` rows for the mission versus the
   effective cap ``min(mission budget, policy max, CALL_MAX_PER_MISSION)``.

Every refusal emits one concise ``CALL_EVENT`` naming the gate. Payloads carry
counts and ids only; the emitter masks anything phone-shaped regardless.
"""

from __future__ import annotations

from datetime import datetime, time

from callswarm.calls.provider import (
    CallBudgetExceeded,
    CallNotAuthorized,
    CallProviderError,
    LiveCallsDisabled,
    QuietHours,
    RecipientNotAllowed,
    RecipientSuppressed,
)
from callswarm.calls.regions import zones_for_region
from callswarm.config.settings import Settings
from callswarm.events import ActivityEventEmitter
from callswarm.models import (
    ActivityEvent,
    ActivityEventType,
    Approval,
    ApprovalStatus,
    ApprovalSubjectType,
    CallIntent,
    Mission,
    hash_phone,
    utcnow,
)
from callswarm.persistence import CallRunRepository, Database, SuppressionEntryRepository

GATE_LIVE_SWITCH = "live_switch"
GATE_APPROVAL = "approval"
GATE_POLICY = "policy"
GATE_ALLOW_LIST = "allow_list"
GATE_SUPPRESSION = "suppression"
GATE_QUIET_HOURS = "quiet_hours"
GATE_BUDGET = "budget"


def parse_clock(value: str) -> time:
    """``HH:MM`` → :class:`datetime.time`; raises ``ValueError`` otherwise."""
    hours, minutes = value.strip().split(":", 1)
    return time(hour=int(hours), minute=int(minutes))


def in_quiet_window(local: time, start: time, end: time) -> bool:
    """True when ``local`` falls inside ``[start, end)``; the window may cross midnight.
    Equal bounds mean no window."""
    if start == end:
        return False
    if start < end:
        return start <= local < end
    return local >= start or local < end


def effective_call_cap(mission: Mission, settings: Settings) -> int:
    return min(
        mission.call_budget.max_calls,
        mission.authority_policy.max_call_count,
        settings.call_max_per_mission,
    )


class CallGate:
    """The one implementation of :class:`callswarm.calls.provider.CallGateProtocol`."""

    def __init__(self, database: Database, emitter: ActivityEventEmitter) -> None:
        self._database = database
        self._emitter = emitter

    async def check(
        self,
        intent: CallIntent,
        approval: Approval | None,
        settings: Settings,
        mission: Mission,
        now: datetime | None = None,
        *,
        simulated: bool,
    ) -> None:
        """Return normally only when every gate passes; otherwise emit and raise."""
        moment = now or utcnow()
        try:
            self._check_live_switch(settings, simulated)
            self._check_approval(intent, approval, moment)
            self._check_policy(mission)
            self._check_allow_list(intent, settings, simulated)
            await self._check_suppression(intent)
            self._check_quiet_hours(intent, settings, moment, simulated)
            await self._check_budget(mission, settings)
        except CallProviderError as exc:
            await self._refuse(intent, exc)
            raise

    # --- individual gates ----------------------------------------------------------
    @staticmethod
    def _check_live_switch(settings: Settings, simulated: bool) -> None:
        if simulated:
            return
        if not settings.calle_live_calls_enabled:
            raise LiveCallsDisabled(
                f"[{GATE_LIVE_SWITCH}] CALLE_LIVE_CALLS_ENABLED is false; live calls are off"
            )
        if settings.call_provider != "calle":
            raise LiveCallsDisabled(
                f"[{GATE_LIVE_SWITCH}] CALL_PROVIDER is {settings.call_provider!r}, not 'calle'"
            )

    @staticmethod
    def _check_approval(intent: CallIntent, approval: Approval | None, now: datetime) -> None:
        if approval is None:
            raise CallNotAuthorized(f"[{GATE_APPROVAL}] no approval record exists for this intent")
        if (
            approval.subject_type is not ApprovalSubjectType.CALL_INTENT
            or approval.subject_id != intent.id
            or approval.mission_id != intent.mission_id
        ):
            raise CallNotAuthorized(f"[{GATE_APPROVAL}] approval is for a different subject")
        if approval.status is not ApprovalStatus.APPROVED:
            raise CallNotAuthorized(
                f"[{GATE_APPROVAL}] approval is {approval.status.value}, not APPROVED"
            )
        if approval.expires_at is not None and approval.expires_at <= now:
            raise CallNotAuthorized(f"[{GATE_APPROVAL}] approval is EXPIRED")

    @staticmethod
    def _check_policy(mission: Mission) -> None:
        if not mission.authority_policy.calls_allowed:
            raise CallNotAuthorized(
                f"[{GATE_POLICY}] mission authority policy does not allow calls"
            )

    @staticmethod
    def _check_allow_list(intent: CallIntent, settings: Settings, simulated: bool) -> None:
        if simulated:
            return
        allowed = {number.strip() for number in settings.call_allowed_recipients if number.strip()}
        if not allowed:
            raise RecipientNotAllowed(
                f"[{GATE_ALLOW_LIST}] CALL_ALLOWED_RECIPIENTS is empty; no live recipient is "
                "permitted"
            )
        for index, recipient in enumerate(intent.recipients):
            if recipient.phone_e164 not in allowed:
                raise RecipientNotAllowed(
                    f"[{GATE_ALLOW_LIST}] recipient #{index} is not on the allow-list"
                )

    async def _check_suppression(self, intent: CallIntent) -> None:
        async with self._database.session() as session:
            repo = SuppressionEntryRepository(session)
            for index, recipient in enumerate(intent.recipients):
                if await repo.is_suppressed(hash_phone(recipient.phone_e164)):
                    raise RecipientSuppressed(
                        f"[{GATE_SUPPRESSION}] recipient #{index} is on the do-not-contact list"
                    )

    @staticmethod
    def _check_quiet_hours(
        intent: CallIntent, settings: Settings, now: datetime, simulated: bool
    ) -> None:
        start = parse_clock(settings.call_quiet_hours_start)
        end = parse_clock(settings.call_quiet_hours_end)
        for index, recipient in enumerate(intent.recipients):
            zones = zones_for_region(recipient.region)
            if not zones:
                if simulated:
                    continue
                raise QuietHours(
                    f"[{GATE_QUIET_HOURS}] recipient #{index} has no resolvable region; local "
                    "time cannot be established"
                )
            for zone in zones:
                local = now.astimezone(zone).time().replace(second=0, microsecond=0)
                if in_quiet_window(local, start, end):
                    raise QuietHours(
                        f"[{GATE_QUIET_HOURS}] recipient #{index} is inside quiet hours "
                        f"({settings.call_quiet_hours_start}-{settings.call_quiet_hours_end} "
                        f"in {zone.key})"
                    )

    async def _check_budget(self, mission: Mission, settings: Settings) -> None:
        cap = effective_call_cap(mission, settings)
        async with self._database.session() as session:
            executed = len(await CallRunRepository(session).list_by_mission(mission.id))
        if executed >= cap:
            raise CallBudgetExceeded(
                f"[{GATE_BUDGET}] {executed} call(s) executed against a cap of {cap}"
            )

    # --- refusal event --------------------------------------------------------------
    async def _refuse(self, intent: CallIntent, exc: CallProviderError) -> None:
        message = str(exc)
        gate = message[1 : message.index("]")] if message.startswith("[") else "unknown"
        await self._emitter.emit(
            ActivityEvent(
                mission_id=intent.mission_id,
                event_type=ActivityEventType.CALL_EVENT,
                summary=f"Call refused by the {gate} gate: {message.split('] ', 1)[-1]}",
                payload={
                    "call_intent_id": intent.id,
                    "gate": gate,
                    "error": type(exc).__name__,
                    "recipient_count": len(intent.recipients),
                },
            )
        )
