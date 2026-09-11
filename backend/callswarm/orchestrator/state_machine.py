"""Deterministic mission state machine (CS-011).

The model proposes a next state; this module decides. ``TRANSITIONS`` is the
complete V1 table from ``02_ARCHITECTURE.md``: there is no other way to move a
mission, and a proposal outside the table raises :class:`IllegalTransition`
before anything is touched. Every applied transition is persisted as a
``MissionTransition`` row carrying its trigger and announced as a
``MISSION_STATUS_CHANGED`` activity event.

``FINAL_EXECUTION_*`` states do not exist in V1 and are therefore unreachable.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import MappingProxyType

from callswarm.events import ActivityEventEmitter
from callswarm.models import (
    ActivityEvent,
    ActivityEventType,
    Mission,
    MissionStatus,
    MissionTransition,
    utcnow,
)
from callswarm.persistence import Database, MissionRepository, MissionTransitionRepository

logger = logging.getLogger(__name__)

S = MissionStatus

TERMINAL_STATES: frozenset[MissionStatus] = frozenset({S.COMPLETE, S.BLOCKED, S.CANCELED})

# Any live mission can be blocked (a required authorization, credential or piece
# of evidence cannot be obtained) or canceled by the user.
_ABORTS: frozenset[MissionStatus] = frozenset({S.BLOCKED, S.CANCELED})

_TABLE: dict[MissionStatus, frozenset[MissionStatus]] = {
    S.MISSION_CREATED: frozenset({S.GOAL_UNDERSTANDING}),
    # The clarification round is optional: a fully specified goal skips it.
    S.GOAL_UNDERSTANDING: frozenset({S.CLARIFICATION_REQUIRED, S.MISSION_SPEC_READY}),
    S.CLARIFICATION_REQUIRED: frozenset({S.CLARIFICATION_COMPLETE}),
    S.CLARIFICATION_COMPLETE: frozenset({S.MISSION_SPEC_READY}),
    S.MISSION_SPEC_READY: frozenset({S.STRATEGY_DISCOVERY_RUNNING}),
    S.STRATEGY_DISCOVERY_RUNNING: frozenset({S.STRATEGY_SET_READY}),
    S.STRATEGY_SET_READY: frozenset({S.SWARM_DESIGN_RUNNING}),
    S.SWARM_DESIGN_RUNNING: frozenset({S.SWARM_READY}),
    S.SWARM_READY: frozenset({S.RESEARCH_RUNNING}),
    S.RESEARCH_RUNNING: frozenset({S.RESEARCH_REVIEW_RUNNING}),
    S.RESEARCH_REVIEW_RUNNING: frozenset({S.INFORMATION_GAPS_READY}),
    S.INFORMATION_GAPS_READY: frozenset({S.CALL_SELECTION_RUNNING}),
    S.CALL_SELECTION_RUNNING: frozenset({S.CALL_PLAN_READY}),
    # An empty call plan (nothing worth calling, or calls not permitted) goes
    # straight to the replan decision instead of through authorization.
    S.CALL_PLAN_READY: frozenset({S.CALL_AUTHORIZATION_PENDING, S.REPLAN_DECISION_RUNNING}),
    # A rejected or expired approval returns to the replan decision; it never
    # proceeds to execution.
    S.CALL_AUTHORIZATION_PENDING: frozenset({S.CALL_AUTHORIZED, S.REPLAN_DECISION_RUNNING}),
    S.CALL_AUTHORIZED: frozenset({S.CALL_EXECUTION_RUNNING}),
    S.CALL_EXECUTION_RUNNING: frozenset({S.CALL_RESULT_RECEIVED}),
    S.CALL_RESULT_RECEIVED: frozenset({S.EVIDENCE_UPDATE_RUNNING}),
    S.EVIDENCE_UPDATE_RUNNING: frozenset({S.REPLAN_DECISION_RUNNING}),
    # Replan outcomes per 03_SWARM_ORCHESTRATION.md "Replanning": another
    # research pass, a new specialist, another call round, a follow-up round,
    # or proceed to optimization.
    S.REPLAN_DECISION_RUNNING: frozenset(
        {
            S.NEGOTIATION_OR_FOLLOWUP_RUNNING,
            S.OPTIMIZATION_RUNNING,
            S.RESEARCH_RUNNING,
            S.SWARM_DESIGN_RUNNING,
            S.CALL_SELECTION_RUNNING,
        }
    ),
    # Loop-back: a follow-up round re-enters call selection.
    S.NEGOTIATION_OR_FOLLOWUP_RUNNING: frozenset({S.CALL_SELECTION_RUNNING}),
    S.OPTIMIZATION_RUNNING: frozenset({S.REVIEW_RUNNING}),
    S.REVIEW_RUNNING: frozenset({S.REVIEW_FAILED, S.REVIEW_PASSED}),
    # Loop-back: a critic FAIL re-enters the replan decision.
    S.REVIEW_FAILED: frozenset({S.REPLAN_DECISION_RUNNING}),
    S.REVIEW_PASSED: frozenset({S.PLAN_OPTIONS_READY}),
    # A user constraint change can arrive as soon as options are presented,
    # before the user has formally entered a decision (03 "User constraint
    # updates": mission state persists; derived artifacts go stale in place).
    S.PLAN_OPTIONS_READY: frozenset(
        {S.USER_DECISION_PENDING, S.MISSION_REVISION_RUNNING, S.COMPLETE}
    ),
    S.USER_DECISION_PENDING: frozenset({S.MISSION_REVISION_RUNNING, S.COMPLETE}),
    # Loop-back: a user constraint change re-enters the loop at the earliest
    # stage whose artifacts went stale (03 "User constraint updates").
    S.MISSION_REVISION_RUNNING: frozenset(
        {
            S.MISSION_SPEC_READY,
            S.STRATEGY_DISCOVERY_RUNNING,
            S.SWARM_DESIGN_RUNNING,
            S.RESEARCH_RUNNING,
            S.CALL_SELECTION_RUNNING,
            S.REPLAN_DECISION_RUNNING,
            S.OPTIMIZATION_RUNNING,
        }
    ),
    S.COMPLETE: frozenset(),
    S.BLOCKED: frozenset(),
    S.CANCELED: frozenset(),
}

TRANSITIONS: Mapping[MissionStatus, frozenset[MissionStatus]] = MappingProxyType(
    {
        state: (targets if state in TERMINAL_STATES else targets | _ABORTS)
        for state, targets in _TABLE.items()
    }
)

assert set(TRANSITIONS) == set(MissionStatus), "every MissionStatus must have a table entry"


class IllegalTransition(Exception):
    """A proposed transition is not in the table. Nothing was mutated."""

    def __init__(self, mission_id: str, current: MissionStatus, proposed: MissionStatus) -> None:
        self.mission_id = mission_id
        self.current = current
        self.proposed = proposed
        super().__init__(
            f"mission {mission_id}: transition {current.value} -> {proposed.value} is not allowed"
        )


def is_allowed(current: MissionStatus, proposed: MissionStatus) -> bool:
    return proposed in TRANSITIONS[current]


def allowed_next(current: MissionStatus) -> frozenset[MissionStatus]:
    return TRANSITIONS[current]


def is_terminal(status: MissionStatus) -> bool:
    return status in TERMINAL_STATES


class MissionStateMachine:
    """Applies table-validated transitions and records each one."""

    def __init__(self, database: Database, emitter: ActivityEventEmitter) -> None:
        self._database = database
        self._emitter = emitter

    async def propose_transition(
        self, mission: Mission, proposed: MissionStatus, trigger: str
    ) -> Mission:
        """Validate ``proposed`` against the table and apply it.

        Validation happens against the *persisted* status, so a stale in-memory
        mission cannot be used to skip a state. Only ``status`` and
        ``updated_at`` are written. On rejection the mission row is untouched,
        no transition row is written and no event is emitted.
        """
        if not trigger.strip():
            raise ValueError("a transition trigger is required")
        async with self._database.session() as session:
            repo = MissionRepository(session)
            stored = await repo.get(mission.id)
            if stored is None:
                raise KeyError(f"mission {mission.id!r} not found")
            current = stored.status
            if not is_allowed(current, proposed):
                logger.warning(
                    "illegal transition rejected: mission=%s %s -> %s (trigger=%s)",
                    mission.id,
                    current.value,
                    proposed.value,
                    trigger,
                )
                raise IllegalTransition(mission.id, current, proposed)
            # Only the status changes here. Callers persist their own field
            # updates (spec, budget, ...) before proposing a transition.
            updated = stored.model_copy(update={"status": proposed, "updated_at": utcnow()})
            await repo.update(updated)
            await MissionTransitionRepository(session).add(
                MissionTransition(
                    mission_id=mission.id,
                    from_status=current,
                    to_status=proposed,
                    trigger=trigger,
                )
            )
        await self._emitter.emit(
            ActivityEvent(
                mission_id=mission.id,
                event_type=ActivityEventType.MISSION_STATUS_CHANGED,
                summary=f"Mission moved from {current.value} to {proposed.value}.",
                payload={
                    "from_status": current.value,
                    "to_status": proposed.value,
                    "trigger": trigger,
                },
            )
        )
        return updated

    async def history(self, mission_id: str) -> list[MissionTransition]:
        async with self._database.session() as session:
            return await MissionTransitionRepository(session).list_by_mission(mission_id)
