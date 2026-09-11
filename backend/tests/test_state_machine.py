"""CS-011: table-driven mission state machine."""

from __future__ import annotations

import pytest

from callswarm.events import ActivityEventEmitter
from callswarm.models import ActivityEventType, Mission, MissionStatus
from callswarm.orchestrator.state_machine import (
    TERMINAL_STATES,
    TRANSITIONS,
    IllegalTransition,
    MissionStateMachine,
    allowed_next,
    is_allowed,
    is_terminal,
)
from callswarm.persistence import (
    ActivityEventRepository,
    Database,
    MissionRepository,
    MissionTransitionRepository,
)
from tests.conftest import set_mission_status

S = MissionStatus

HAPPY_PATH: list[MissionStatus] = [
    S.GOAL_UNDERSTANDING,
    S.CLARIFICATION_REQUIRED,
    S.CLARIFICATION_COMPLETE,
    S.MISSION_SPEC_READY,
    S.STRATEGY_DISCOVERY_RUNNING,
    S.STRATEGY_SET_READY,
    S.SWARM_DESIGN_RUNNING,
    S.SWARM_READY,
    S.RESEARCH_RUNNING,
    S.RESEARCH_REVIEW_RUNNING,
    S.INFORMATION_GAPS_READY,
    S.CALL_SELECTION_RUNNING,
    S.CALL_PLAN_READY,
    S.CALL_AUTHORIZATION_PENDING,
    S.CALL_AUTHORIZED,
    S.CALL_EXECUTION_RUNNING,
    S.CALL_RESULT_RECEIVED,
    S.EVIDENCE_UPDATE_RUNNING,
    S.REPLAN_DECISION_RUNNING,
    S.OPTIMIZATION_RUNNING,
    S.REVIEW_RUNNING,
    S.REVIEW_PASSED,
    S.PLAN_OPTIONS_READY,
    S.USER_DECISION_PENDING,
    S.COMPLETE,
]


def test_table_covers_every_state_and_has_no_execution_states() -> None:
    assert set(TRANSITIONS) == set(MissionStatus)
    assert not any("FINAL_EXECUTION" in s.value for s in MissionStatus)
    for terminal in TERMINAL_STATES:
        assert allowed_next(terminal) == frozenset()
        assert is_terminal(terminal)
    for state in MissionStatus:
        if state not in TERMINAL_STATES:
            assert {S.BLOCKED, S.CANCELED} <= allowed_next(state)
            assert not is_terminal(state)


def test_happy_path_pairs_are_in_the_table() -> None:
    current = S.MISSION_CREATED
    for nxt in HAPPY_PATH:
        assert is_allowed(current, nxt), f"{current} -> {nxt}"
        current = nxt


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (S.REVIEW_RUNNING, S.REVIEW_FAILED),
        (S.REVIEW_FAILED, S.REPLAN_DECISION_RUNNING),
        (S.REPLAN_DECISION_RUNNING, S.NEGOTIATION_OR_FOLLOWUP_RUNNING),
        (S.NEGOTIATION_OR_FOLLOWUP_RUNNING, S.CALL_SELECTION_RUNNING),
        (S.USER_DECISION_PENDING, S.MISSION_REVISION_RUNNING),
        (S.MISSION_REVISION_RUNNING, S.RESEARCH_RUNNING),
        (S.MISSION_REVISION_RUNNING, S.STRATEGY_DISCOVERY_RUNNING),
        (S.MISSION_REVISION_RUNNING, S.OPTIMIZATION_RUNNING),
        (S.GOAL_UNDERSTANDING, S.MISSION_SPEC_READY),  # no clarification needed
        (S.CALL_PLAN_READY, S.REPLAN_DECISION_RUNNING),  # empty call plan
        (S.CALL_AUTHORIZATION_PENDING, S.REPLAN_DECISION_RUNNING),  # rejected approval
        (S.PLAN_OPTIONS_READY, S.COMPLETE),
    ],
)
def test_loop_backs_and_optional_paths(frm: MissionStatus, to: MissionStatus) -> None:
    assert is_allowed(frm, to)


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (S.MISSION_CREATED, S.CALL_AUTHORIZED),
        (S.CALL_PLAN_READY, S.CALL_AUTHORIZED),  # skipping authorization
        (S.CALL_AUTHORIZATION_PENDING, S.CALL_EXECUTION_RUNNING),  # skipping CALL_AUTHORIZED
        (S.CALL_SELECTION_RUNNING, S.CALL_EXECUTION_RUNNING),
        (S.SWARM_READY, S.COMPLETE),
        (S.COMPLETE, S.MISSION_CREATED),
        (S.CANCELED, S.GOAL_UNDERSTANDING),
        (S.GOAL_UNDERSTANDING, S.GOAL_UNDERSTANDING),
    ],
)
def test_illegal_pairs(frm: MissionStatus, to: MissionStatus) -> None:
    assert not is_allowed(frm, to)


async def test_full_happy_path_persists_every_transition_with_trigger(
    database: Database, state_machine: MissionStateMachine, mission: Mission
) -> None:
    current = mission
    for index, nxt in enumerate(HAPPY_PATH):
        current = await state_machine.propose_transition(current, nxt, trigger=f"step-{index}")
        assert current.status is nxt
    async with database.session() as s:
        stored = await MissionRepository(s).get(mission.id)
        rows = await MissionTransitionRepository(s).list_by_mission(mission.id)
        events = await ActivityEventRepository(s).list_by_mission(mission.id)
    assert stored is not None and stored.status is S.COMPLETE
    assert [r.to_status for r in rows] == HAPPY_PATH
    assert [r.from_status for r in rows] == [S.MISSION_CREATED, *HAPPY_PATH[:-1]]
    assert [r.trigger for r in rows] == [f"step-{i}" for i in range(len(HAPPY_PATH))]
    status_events = [e for e in events if e.event_type is ActivityEventType.MISSION_STATUS_CHANGED]
    assert len(status_events) == len(HAPPY_PATH)
    assert status_events[-1].payload["trigger"] == f"step-{len(HAPPY_PATH) - 1}"


async def test_every_loop_back_applies(
    database: Database, state_machine: MissionStateMachine, mission: Mission
) -> None:
    m = await set_mission_status(database, mission, S.REVIEW_RUNNING)
    m = await state_machine.propose_transition(m, S.REVIEW_FAILED, trigger="critic.fail")
    m = await state_machine.propose_transition(m, S.REPLAN_DECISION_RUNNING, trigger="replan")
    m = await state_machine.propose_transition(
        m, S.NEGOTIATION_OR_FOLLOWUP_RUNNING, trigger="followup"
    )
    m = await state_machine.propose_transition(m, S.CALL_SELECTION_RUNNING, trigger="loop")
    m = await set_mission_status(database, m, S.USER_DECISION_PENDING)
    m = await state_machine.propose_transition(m, S.MISSION_REVISION_RUNNING, trigger="user")
    m = await state_machine.propose_transition(m, S.RESEARCH_RUNNING, trigger="revise")
    assert m.status is S.RESEARCH_RUNNING
    history = await state_machine.history(mission.id)
    assert [t.trigger for t in history] == [
        "critic.fail",
        "replan",
        "followup",
        "loop",
        "user",
        "revise",
    ]


async def test_illegal_proposal_raises_and_does_not_mutate(
    database: Database,
    state_machine: MissionStateMachine,
    mission: Mission,
    emitter: ActivityEventEmitter,
) -> None:
    with pytest.raises(IllegalTransition) as info:
        await state_machine.propose_transition(mission, S.CALL_AUTHORIZED, trigger="model says so")
    assert info.value.current is S.MISSION_CREATED
    assert info.value.proposed is S.CALL_AUTHORIZED
    async with database.session() as s:
        stored = await MissionRepository(s).get(mission.id)
        rows = await MissionTransitionRepository(s).list_by_mission(mission.id)
        events = await ActivityEventRepository(s).list_by_mission(mission.id)
    assert stored is not None and stored.status is S.MISSION_CREATED
    assert rows == []
    assert events == []


async def test_stale_in_memory_status_cannot_skip_a_state(
    database: Database, state_machine: MissionStateMachine, mission: Mission
) -> None:
    forged = mission.model_copy(update={"status": S.CALL_AUTHORIZATION_PENDING})
    with pytest.raises(IllegalTransition):
        await state_machine.propose_transition(forged, S.CALL_AUTHORIZED, trigger="forged")
    async with database.session() as s:
        stored = await MissionRepository(s).get(mission.id)
    assert stored is not None and stored.status is S.MISSION_CREATED


async def test_terminal_states_have_no_exit(
    database: Database, state_machine: MissionStateMachine, mission: Mission
) -> None:
    m = await state_machine.propose_transition(mission, S.CANCELED, trigger="user.cancel")
    with pytest.raises(IllegalTransition):
        await state_machine.propose_transition(m, S.GOAL_UNDERSTANDING, trigger="retry")
    with pytest.raises(ValueError, match="trigger"):
        await state_machine.propose_transition(mission, S.GOAL_UNDERSTANDING, trigger="  ")
    with pytest.raises(KeyError):
        await state_machine.propose_transition(
            Mission(user_goal="ghost"), S.GOAL_UNDERSTANDING, trigger="x"
        )


async def test_transition_writes_only_status(
    database: Database, state_machine: MissionStateMachine, mission: Mission
) -> None:
    """Non-status fields on the caller's object are not written by the machine."""
    tampered = mission.model_copy(update={"user_goal": "changed in memory"})
    await state_machine.propose_transition(tampered, S.GOAL_UNDERSTANDING, trigger="t")
    async with database.session() as s:
        stored = await MissionRepository(s).get(mission.id)
    assert stored is not None
    assert stored.user_goal == "test goal"
    assert stored.status is S.GOAL_UNDERSTANDING
    assert stored.updated_at >= mission.updated_at
