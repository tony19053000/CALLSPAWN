"""ApprovalService (CS-034): explicit authorization records.

An :class:`Approval` is the only thing that can authorize a call intent. It
is created ``PENDING`` with an expiry, changes state solely through
:meth:`ApprovalService.decide` (called by the typed decision endpoint) or
:meth:`expire_stale`, and is never inferred from conversation. No text in a
mission, a goal, a chat message or a research page reaches this module.

Mission transitions performed here are conditional on the persisted status:

* ``request`` moves ``CALL_PLAN_READY → CALL_AUTHORIZATION_PENDING`` the
  first time an approval is requested for the mission;
* an ``APPROVED`` decision moves ``CALL_AUTHORIZATION_PENDING →
  CALL_AUTHORIZED``;
* a ``REJECTED`` decision (or expiry) that leaves no other usable approval
  moves ``CALL_AUTHORIZATION_PENDING → REPLAN_DECISION_RUNNING``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from callswarm.config.settings import Settings
from callswarm.events import ActivityEventEmitter
from callswarm.models import (
    ActivityEvent,
    ActivityEventType,
    Approval,
    ApprovalStatus,
    ApprovalSubjectType,
    CallAuthorizationState,
    CallIntent,
    Mission,
    MissionStatus,
    utcnow,
)
from callswarm.orchestrator.state_machine import MissionStateMachine
from callswarm.persistence import (
    ApprovalRepository,
    CallIntentRepository,
    Database,
    MissionRepository,
)

logger = logging.getLogger(__name__)

Decision = Literal["APPROVED", "REJECTED"]

DECISION_TO_STATUS: dict[str, ApprovalStatus] = {
    "APPROVED": ApprovalStatus.APPROVED,
    "REJECTED": ApprovalStatus.REJECTED,
}
STATUS_TO_INTENT_STATE: dict[ApprovalStatus, CallAuthorizationState] = {
    ApprovalStatus.PENDING: CallAuthorizationState.PENDING,
    ApprovalStatus.APPROVED: CallAuthorizationState.APPROVED,
    ApprovalStatus.REJECTED: CallAuthorizationState.REJECTED,
    ApprovalStatus.EXPIRED: CallAuthorizationState.EXPIRED,
}


class ApprovalNotFound(KeyError):
    pass


class ApprovalNotPending(Exception):
    def __init__(self, approval: Approval) -> None:
        self.approval = approval
        super().__init__(f"approval {approval.id} is {approval.status.value}, not PENDING")


class ApprovalService:
    def __init__(
        self,
        database: Database,
        emitter: ActivityEventEmitter,
        settings: Settings,
        state_machine: MissionStateMachine,
    ) -> None:
        self._database = database
        self._emitter = emitter
        self._settings = settings
        self._machine = state_machine

    # --- queries ----------------------------------------------------------------
    async def list_for_mission(self, mission_id: str) -> list[Approval]:
        async with self._database.session() as session:
            approvals = await ApprovalRepository(session).list_by_mission(mission_id)
        return sorted(approvals, key=lambda a: a.requested_at)

    async def get(self, approval_id: str) -> Approval:
        async with self._database.session() as session:
            approval = await ApprovalRepository(session).get(approval_id)
        if approval is None:
            raise ApprovalNotFound(approval_id)
        return approval

    async def latest_for_intent(self, intent_id: str) -> Approval | None:
        """The most recently requested approval for an intent, if any."""
        async with self._database.session() as session:
            approvals = await ApprovalRepository(session).list_by_subject(intent_id)
        approvals = [a for a in approvals if a.subject_type is ApprovalSubjectType.CALL_INTENT]
        if not approvals:
            return None
        return max(approvals, key=lambda a: a.requested_at)

    # --- lifecycle --------------------------------------------------------------
    async def request(self, intent: CallIntent, *, now: datetime | None = None) -> Approval:
        """Create a ``PENDING`` approval for ``intent`` with an expiry."""
        moment = now or utcnow()
        approval = Approval(
            mission_id=intent.mission_id,
            subject_type=ApprovalSubjectType.CALL_INTENT,
            subject_id=intent.id,
            status=ApprovalStatus.PENDING,
            requested_at=moment,
            expires_at=moment + timedelta(minutes=self._settings.approval_ttl_minutes),
        )
        async with self._database.session() as session:
            stored = await ApprovalRepository(session).add(approval)
            await self._set_intent_state(session, intent.id, CallAuthorizationState.PENDING)
        await self._emitter.emit(
            ActivityEvent(
                mission_id=intent.mission_id,
                event_type=ActivityEventType.APPROVAL_EVENT,
                summary=f"Approval requested for a call: {intent.purpose}",
                payload={
                    "approval_id": stored.id,
                    "call_intent_id": intent.id,
                    "status": stored.status.value,
                    "expires_at": stored.expires_at.isoformat() if stored.expires_at else None,
                },
            )
        )
        mission = await self._mission(intent.mission_id)
        if mission.status is MissionStatus.CALL_PLAN_READY:
            await self._machine.propose_transition(
                mission, MissionStatus.CALL_AUTHORIZATION_PENDING, "approval requested"
            )
        return stored

    async def decide(
        self,
        approval_id: str,
        decision: Decision,
        decided_by: str,
        *,
        now: datetime | None = None,
    ) -> Approval:
        """Apply an explicit decision to a ``PENDING`` approval. The only way
        an approval becomes ``APPROVED``."""
        status = DECISION_TO_STATUS[decision]
        moment = now or utcnow()
        async with self._database.session() as session:
            repo = ApprovalRepository(session)
            approval = await repo.get(approval_id)
            if approval is None:
                raise ApprovalNotFound(approval_id)
            if approval.status is not ApprovalStatus.PENDING:
                raise ApprovalNotPending(approval)
            if approval.expires_at is not None and approval.expires_at <= moment:
                expired = await repo.update(
                    approval.model_copy(update={"status": ApprovalStatus.EXPIRED})
                )
                await self._set_intent_state(
                    session, approval.subject_id, CallAuthorizationState.EXPIRED
                )
                raise ApprovalNotPending(expired)
            updated = await repo.update(
                approval.model_copy(
                    update={"status": status, "decided_at": moment, "decided_by": decided_by}
                )
            )
            await self._set_intent_state(
                session, updated.subject_id, STATUS_TO_INTENT_STATE[status]
            )
        await self._emitter.emit(
            ActivityEvent(
                mission_id=updated.mission_id,
                event_type=ActivityEventType.APPROVAL_EVENT,
                summary=f"Call approval {updated.status.value.lower()} by {decided_by}.",
                payload={
                    "approval_id": updated.id,
                    "call_intent_id": updated.subject_id,
                    "status": updated.status.value,
                },
            )
        )
        await self._advance_mission(updated.mission_id)
        return updated

    async def expire_stale(self, *, now: datetime | None = None) -> list[Approval]:
        """Mark every ``PENDING`` approval past its expiry ``EXPIRED``."""
        moment = now or utcnow()
        expired: list[Approval] = []
        async with self._database.session() as session:
            repo = ApprovalRepository(session)
            for mission in await MissionRepository(session).list_all():
                for approval in await repo.list_by_mission(mission.id):
                    if (
                        approval.status is ApprovalStatus.PENDING
                        and approval.expires_at is not None
                        and approval.expires_at <= moment
                    ):
                        expired.append(
                            await repo.update(
                                approval.model_copy(update={"status": ApprovalStatus.EXPIRED})
                            )
                        )
                        await self._set_intent_state(
                            session, approval.subject_id, CallAuthorizationState.EXPIRED
                        )
        for approval in expired:
            await self._emitter.emit(
                ActivityEvent(
                    mission_id=approval.mission_id,
                    event_type=ActivityEventType.APPROVAL_EVENT,
                    summary="A call approval expired before a decision was made.",
                    payload={"approval_id": approval.id, "call_intent_id": approval.subject_id},
                )
            )
        for mission_id in {a.mission_id for a in expired}:
            await self._advance_mission(mission_id)
        return expired

    # --- helpers ------------------------------------------------------------------
    @staticmethod
    async def _set_intent_state(
        session: AsyncSession, intent_id: str, state: CallAuthorizationState
    ) -> None:
        intents = CallIntentRepository(session)
        intent = await intents.get(intent_id)
        if intent is None:
            return
        await intents.update(
            intent.model_copy(update={"authorization_state": state, "updated_at": utcnow()})
        )

    async def _mission(self, mission_id: str) -> Mission:
        async with self._database.session() as session:
            mission = await MissionRepository(session).get(mission_id)
        if mission is None:
            raise KeyError(f"mission {mission_id!r} not found")
        return mission

    async def _advance_mission(self, mission_id: str) -> None:
        mission = await self._mission(mission_id)
        if mission.status is not MissionStatus.CALL_AUTHORIZATION_PENDING:
            return
        approvals = await self.list_for_mission(mission_id)
        if any(a.status is ApprovalStatus.APPROVED for a in approvals):
            await self._machine.propose_transition(
                mission, MissionStatus.CALL_AUTHORIZED, "a call was approved"
            )
        elif not any(a.status is ApprovalStatus.PENDING for a in approvals):
            await self._machine.propose_transition(
                mission, MissionStatus.REPLAN_DECISION_RUNNING, "no call approved"
            )
