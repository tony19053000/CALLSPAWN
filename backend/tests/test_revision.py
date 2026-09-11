"""CS-042: constraint updates change the spec and stale only dependent artifacts."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from callswarm.events import ActivityEventEmitter
from callswarm.evidence import EvidenceEngine
from callswarm.models import (
    ActivityEventType,
    AgentRun,
    AgentSpec,
    AgentState,
    AuthorityPolicy,
    CallBudget,
    CallRun,
    ConstraintChange,
    ConstraintUpdate,
    EvidenceClaim,
    HardConstraint,
    Mission,
    MissionSpec,
    MissionStatus,
    PlanComponent,
    PlanOption,
    SoftPreference,
    SourceType,
    StrategyCandidate,
    StrategyStatus,
)
from callswarm.models.mission import ConstraintOperator
from callswarm.orchestrator.revision import (
    RevisionNotAllowedError,
    RevisionService,
    apply_change_to_spec,
    build_dependency_graph,
    key_matches,
)
from callswarm.orchestrator.state_machine import MissionStateMachine, is_allowed
from callswarm.persistence import (
    ActivityEventRepository,
    AgentRunRepository,
    AgentSpecRepository,
    CallIntentRepository,
    CallRunRepository,
    Database,
    EvidenceClaimRepository,
    MissionRepository,
    MissionTransitionRepository,
    PlanOptionRepository,
    StrategyCandidateRepository,
)
from tests.conftest import make_intent

S = MissionStatus
BUDGET_KEY = "max_total_cost"


@pytest.fixture
async def service(
    database: Database, emitter: ActivityEventEmitter, state_machine: MissionStateMachine
) -> RevisionService:
    return RevisionService(database, emitter, state_machine)


@pytest.fixture
async def world(database: Database, emitter: ActivityEventEmitter) -> dict[str, Any]:
    """The 03 example, domain-neutral: a total-cost constraint of 400000, two
    components (component_a, component_b), one agent per component plus one
    budget-dependent agent, two strategies, two plan options, evidence from
    research and from a simulated call."""
    async with database.session() as s:
        mission = await MissionRepository(s).add(
            Mission(
                user_goal="test goal",
                status=S.USER_DECISION_PENDING,
                authority_policy=AuthorityPolicy(calls_allowed=True, max_call_count=3),
                call_budget=CallBudget(max_calls=3, calls_used=1),
            )
        )
        spec = MissionSpec(
            mission_id=mission.id,
            summary="s",
            hard_constraints=[
                HardConstraint(key=BUDGET_KEY, operator=ConstraintOperator.LE, value=400000),
                HardConstraint(
                    key="event_date", operator=ConstraintOperator.EQ, value="2026-12-01"
                ),
            ],
            soft_preferences=[SoftPreference(key="component_b quality", weight=0.5)],
        )
        mission = await MissionRepository(s).update(
            mission.model_copy(
                update={"spec": spec, "hard_constraints": list(spec.hard_constraints)}
            )
        )
        strategies = StrategyCandidateRepository(s)
        budget_strategy = await strategies.add(
            StrategyCandidate(
                mission_id=mission.id,
                title="Cost-led",
                status=StrategyStatus.ACTIVE,
                required_information=["max_total_cost split across components"],
            )
        )
        date_strategy = await strategies.add(
            StrategyCandidate(
                mission_id=mission.id,
                title="Date-led",
                status=StrategyStatus.ACTIVE,
                required_information=["event_date availability"],
            )
        )
        specs = AgentSpecRepository(s)
        allocator = await specs.add(
            AgentSpec(
                mission_id=mission.id,
                name="Allocator",
                role="r",
                objective="split the max_total_cost",
                why_needed="w",
                owns="max_total_cost allocation across components",
                required_inputs=["max_total_cost"],
                strategy_id=budget_strategy.id,
                state=AgentState.COMPLETE,
            )
        )
        component_a_agent = await specs.add(
            AgentSpec(
                mission_id=mission.id,
                name="Component A scout",
                role="r",
                objective="find component_a options within the allocation",
                why_needed="w",
                owns="component_a selection",
                required_inputs=["max_total_cost allocation"],
                state=AgentState.COMPLETE,
            )
        )
        component_b_agent = await specs.add(
            AgentSpec(
                mission_id=mission.id,
                name="Component B scout",
                role="r",
                objective="find component_b options",
                why_needed="w",
                owns="component_b selection",
                required_inputs=["max_total_cost allocation"],
                state=AgentState.COMPLETE,
            )
        )
        date_agent = await specs.add(
            AgentSpec(
                mission_id=mission.id,
                name="Date checker",
                role="r",
                objective="confirm event_date availability",
                why_needed="w",
                owns="event_date availability",
                required_inputs=["event_date"],
                strategy_id=date_strategy.id,
                state=AgentState.COMPLETE,
            )
        )
        runs = AgentRunRepository(s)
        run_ids: dict[str, str] = {}
        for agent in (allocator, component_a_agent, component_b_agent, date_agent):
            run = await runs.add(
                AgentRun(
                    mission_id=mission.id,
                    agent_id=agent.id,
                    status=AgentState.COMPLETE,
                    output_artifact={"result": agent.name},
                )
            )
            run_ids[agent.id] = run.id
        plans = PlanOptionRepository(s)
        budget_plan = await plans.add(
            PlanOption(
                mission_id=mission.id,
                name="plan within cost",
                components=[
                    PlanComponent(name="component_a", cost=250000),
                    PlanComponent(name="component_b", cost=120000),
                ],
                total_cost=370000,
                constraint_keys=[BUDGET_KEY],
                hard_constraints_passed=True,
            )
        )
        date_plan = await plans.add(
            PlanOption(
                mission_id=mission.id,
                name="plan keyed on date only",
                components=[PlanComponent(name="component_c", cost=1)],
                constraint_keys=["event_date"],
                hard_constraints_passed=True,
            )
        )
        intent = await CallIntentRepository(s).add(make_intent(mission.id))
        call_run = await CallRunRepository(s).add(
            CallRun(
                mission_id=mission.id,
                call_intent_id=intent.id,
                is_simulated=True,
                structured_result={"quoted_cost": 120000},
            )
        )
    engine = EvidenceEngine(database, emitter)
    await engine.ingest(
        [
            EvidenceClaim(
                mission_id=mission.id,
                subject="Candidate B1",
                predicate="quoted_cost",
                value=120000,
                source_type=SourceType.SIMULATED,
                source_reference=f"call_run:{call_run.id}",
            ),
            EvidenceClaim(
                mission_id=mission.id,
                subject="Candidate A1",
                predicate="listed_cost",
                value=250000,
                source_type=SourceType.FIXTURE,
                source_reference="artifact:1",
            ),
        ],
        source="fixture",
    )
    return {
        "mission": mission,
        "budget_strategy": budget_strategy,
        "date_strategy": date_strategy,
        "allocator": allocator,
        "component_a_agent": component_a_agent,
        "component_b_agent": component_b_agent,
        "date_agent": date_agent,
        "run_ids": run_ids,
        "budget_plan": budget_plan,
        "date_plan": date_plan,
        "call_run": call_run,
    }


async def snapshot(database: Database, mission_id: str) -> dict[str, Any]:
    async with database.session() as s:
        claims = await EvidenceClaimRepository(s).list_by_mission(mission_id)
        runs = await CallRunRepository(s).list_by_mission(mission_id)
        agents = {a.id: a for a in await AgentSpecRepository(s).list_by_mission(mission_id)}
        agent_runs = {r.id: r for r in await AgentRunRepository(s).list_by_mission(mission_id)}
        plans = {p.id: p for p in await PlanOptionRepository(s).list_by_mission(mission_id)}
        strategies = {
            x.id: x for x in await StrategyCandidateRepository(s).list_by_mission(mission_id)
        }
        mission = await MissionRepository(s).get(mission_id)
    return {
        "claims": {c.id: c.model_dump(mode="json") for c in claims},
        "call_runs": {r.id: r.model_dump(mode="json") for r in runs},
        "agents": agents,
        "agent_runs": agent_runs,
        "plans": plans,
        "strategies": strategies,
        "mission": mission,
    }


# --- pure pieces --------------------------------------------------------------------


def test_key_matches_is_token_based() -> None:
    assert key_matches("max_total_cost", "Max total cost allocation")
    assert key_matches("component_a", "component_a selection")
    assert not key_matches("component_a", "component_ab selection")
    assert not key_matches("cost", "costume options")
    assert not key_matches("", "anything")


def test_apply_change_to_spec_is_keyed_by_key_and_operator() -> None:
    spec = MissionSpec(
        mission_id="m",
        hard_constraints=[HardConstraint(key="k", operator=ConstraintOperator.LE, value=10)],
        soft_preferences=[SoftPreference(key="p", weight=0.5)],
        locked_components=["component_x"],
    )
    change = ConstraintChange(
        updates=[
            ConstraintUpdate(key="K", operator=ConstraintOperator.LE, value=8),
            ConstraintUpdate(key="k", operator=ConstraintOperator.GE, value=1),
        ],
        locks=["component_y", "Component_X"],
        unlocks=[],
        preference_changes={"p": 0.9, "q": 0.1},
    )
    new_spec, changed = apply_change_to_spec(spec, change)
    assert changed == ["K", "k", "p", "q"]
    by_slot = {(c.key, c.operator): c.value for c in new_spec.hard_constraints}
    assert by_slot == {("k", ConstraintOperator.LE): 8, ("k", ConstraintOperator.GE): 1}
    assert all(c.locked for c in new_spec.hard_constraints)
    assert {p.key: p.weight for p in new_spec.soft_preferences} == {"p": 0.9, "q": 0.1}
    assert new_spec.locked_components == ["component_x", "component_y"]
    # Same value again → nothing changed; unlock removes the lock.
    again, changed = apply_change_to_spec(
        new_spec,
        ConstraintChange(
            updates=[ConstraintUpdate(key="k", operator=ConstraintOperator.LE, value=8)],
            unlocks=["component_x"],
        ),
    )
    assert changed == [] and again.locked_components == ["component_y"]


def test_dependency_graph_defaults_unknown_plan_to_every_hard_constraint() -> None:
    spec = MissionSpec(
        mission_id="m",
        hard_constraints=[
            HardConstraint(key="a", operator=ConstraintOperator.LE, value=1),
            HardConstraint(key="b", operator=ConstraintOperator.LE, value=1),
        ],
    )
    unknown = PlanOption(mission_id="m", name="u")
    declared = PlanOption(mission_id="m", name="d", constraint_keys=["b"])
    agent = AgentSpec(
        mission_id="m", name="N", role="r", objective="o", why_needed="w", owns="component_z"
    )
    graph = build_dependency_graph(
        spec, [], [agent], [unknown, declared], component_keys=["component_z"]
    )
    assert graph.plan_options[unknown.id] == ["a", "b"]
    assert graph.plan_options[declared.id] == ["b"]
    assert graph.component_owners == {"component_z": [agent.id]}
    strategies, agents, plans = graph.dependents_of(["a"])
    assert plans == {unknown.id} and agents == set() and strategies == set()


def test_revision_transitions_are_in_the_table() -> None:
    assert is_allowed(S.USER_DECISION_PENDING, S.MISSION_REVISION_RUNNING)
    assert is_allowed(S.PLAN_OPTIONS_READY, S.MISSION_REVISION_RUNNING)
    assert is_allowed(S.MISSION_REVISION_RUNNING, S.REPLAN_DECISION_RUNNING)


# --- the 03 example ---------------------------------------------------------------------


async def test_budget_change_with_locked_component_preserves_evidence_and_stales_selectively(
    service: RevisionService, database: Database, world: dict[str, Any]
) -> None:
    before = await snapshot(database, world["mission"].id)
    assert len(before["claims"]) == 2 and len(before["call_runs"]) == 1

    result = await service.apply(
        world["mission"],
        ConstraintChange(
            updates=[
                ConstraintUpdate(key=BUDGET_KEY, operator=ConstraintOperator.LE, value=320000)
            ],
            locks=["component_b"],
        ),
    )
    after = await snapshot(database, world["mission"].id)

    # Evidence and call runs: byte-identical, nothing re-asked.
    assert after["claims"] == before["claims"]
    assert after["call_runs"] == before["call_runs"]
    assert result.preserved_claims == 2 and result.preserved_call_runs == 1
    assert after["mission"].call_budget.calls_used == 1

    # Spec updated deterministically and mirrored on the mission.
    spec = after["mission"].spec
    assert spec is not None
    budget = next(c for c in spec.hard_constraints if c.key == BUDGET_KEY)
    assert budget.value == 320000 and budget.operator is ConstraintOperator.LE and budget.locked
    assert next(c for c in spec.hard_constraints if c.key == "event_date").value == "2026-12-01"
    assert spec.locked_components == ["component_b"]
    assert next(c for c in after["mission"].hard_constraints if c.key == BUDGET_KEY).value == 320000
    assert result.changed_keys == [BUDGET_KEY]

    # Only budget-dependent derived artifacts are stale.
    assert set(result.stale_plan_option_ids) == {world["budget_plan"].id}
    assert after["plans"][world["budget_plan"].id].stale is True
    assert BUDGET_KEY in (after["plans"][world["budget_plan"].id].stale_reason or "")
    assert after["plans"][world["date_plan"].id].stale is False
    assert set(result.stale_strategy_ids) == {world["budget_strategy"].id}
    assert after["strategies"][world["date_strategy"].id].stale is False
    assert after["strategies"][world["budget_strategy"].id].status is StrategyStatus.ACTIVE

    # The locked component's plan entry is marked locked.
    locked_components = {
        c.name: c.locked for c in after["plans"][world["budget_plan"].id].components
    }
    assert locked_components == {"component_a": False, "component_b": True}

    # Agents: budget-dependent ones re-queued; the locked component's owner and
    # the date agent untouched.
    assert set(result.requeued_agent_ids) == {
        world["allocator"].id,
        world["component_a_agent"].id,
    }
    assert after["agents"][world["allocator"].id].state is AgentState.READY
    assert after["agents"][world["component_a_agent"].id].state is AgentState.READY
    assert after["agents"][world["component_b_agent"].id].state is AgentState.COMPLETE
    assert after["agents"][world["component_b_agent"].id].state_reason is None
    assert after["agents"][world["date_agent"].id].state is AgentState.COMPLETE
    assert set(result.untouched_agent_ids) == {
        world["component_b_agent"].id,
        world["date_agent"].id,
    }
    stale_runs = {rid for rid, r in after["agent_runs"].items() if r.stale}
    assert stale_runs == {
        world["run_ids"][world["allocator"].id],
        world["run_ids"][world["component_a_agent"].id],
    }
    assert set(result.stale_run_ids) == stale_runs
    # Stale outputs are kept, not deleted.
    assert all(r.output_artifact is not None for r in after["agent_runs"].values())

    # Transitions and the counted event.
    assert result.status is S.REPLAN_DECISION_RUNNING
    assert after["mission"].status is S.REPLAN_DECISION_RUNNING
    async with database.session() as s:
        history = await MissionTransitionRepository(s).list_by_mission(world["mission"].id)
        events = await ActivityEventRepository(s).list_by_mission(world["mission"].id)
    assert [(t.from_status, t.to_status) for t in history] == [
        (S.USER_DECISION_PENDING, S.MISSION_REVISION_RUNNING),
        (S.MISSION_REVISION_RUNNING, S.REPLAN_DECISION_RUNNING),
    ]
    revised = [e for e in events if e.summary.startswith("constraints revised")]
    assert len(revised) == 1
    assert revised[0].summary == (
        "constraints revised: 4 derived artifacts marked stale, 2 agents re-queued, "
        "evidence preserved (2 claims)"
    )
    assert revised[0].event_type is ActivityEventType.SYSTEM
    assert revised[0].payload["locked_components"] == ["component_b"]
    requeue_events = [e for e in events if e.event_type is ActivityEventType.AGENT_STATUS_CHANGED]
    assert {e.agent_id for e in requeue_events} == set(result.requeued_agent_ids)


async def test_unrelated_change_touches_nothing_but_the_spec(
    service: RevisionService, database: Database, world: dict[str, Any]
) -> None:
    result = await service.apply(
        world["mission"],
        ConstraintChange(
            updates=[
                ConstraintUpdate(key="attendee_count", operator=ConstraintOperator.GE, value=3)
            ]
        ),
    )
    after = await snapshot(database, world["mission"].id)
    assert result.changed_keys == ["attendee_count"]
    assert result.stale_artifact_count == 0 and result.requeued_agent_ids == []
    assert all(not p.stale for p in after["plans"].values())
    assert all(a.state is AgentState.COMPLETE for a in after["agents"].values())
    assert after["mission"].spec is not None
    assert any(c.key == "attendee_count" for c in after["mission"].spec.hard_constraints)
    assert after["mission"].status is S.REPLAN_DECISION_RUNNING


async def test_lock_alone_changes_no_key_and_stales_nothing(
    service: RevisionService, database: Database, world: dict[str, Any]
) -> None:
    result = await service.apply(world["mission"], ConstraintChange(locks=["component_a"]))
    after = await snapshot(database, world["mission"].id)
    assert result.changed_keys == [] and result.stale_artifact_count == 0
    assert after["mission"].spec is not None
    assert after["mission"].spec.locked_components == ["component_a"]
    assert {c.name: c.locked for c in after["plans"][world["budget_plan"].id].components} == {
        "component_a": True,
        "component_b": False,
    }


async def test_stopped_agents_are_not_requeued(
    service: RevisionService, database: Database, world: dict[str, Any]
) -> None:
    async with database.session() as s:
        await AgentSpecRepository(s).update(
            world["allocator"].model_copy(
                update={"state": AgentState.STOPPED, "state_reason": "stopped by Orchestrator: x"}
            )
        )
    result = await service.apply(
        world["mission"],
        ConstraintChange(
            updates=[ConstraintUpdate(key=BUDGET_KEY, operator=ConstraintOperator.LE, value=1)]
        ),
    )
    after = await snapshot(database, world["mission"].id)
    assert world["allocator"].id not in result.requeued_agent_ids
    assert after["agents"][world["allocator"].id].state is AgentState.STOPPED
    # Its stale output is still marked so nobody reuses it.
    assert after["agent_runs"][world["run_ids"][world["allocator"].id]].stale is True


async def test_revision_refused_outside_decision_states(
    service: RevisionService, database: Database, world: dict[str, Any]
) -> None:
    async with database.session() as s:
        await MissionRepository(s).update(
            world["mission"].model_copy(update={"status": S.RESEARCH_RUNNING})
        )
    with pytest.raises(RevisionNotAllowedError):
        await service.apply(
            world["mission"],
            ConstraintChange(
                updates=[ConstraintUpdate(key=BUDGET_KEY, operator=ConstraintOperator.LE, value=1)]
            ),
        )
    after = await snapshot(database, world["mission"].id)
    assert after["mission"].status is S.RESEARCH_RUNNING
    assert after["mission"].spec is not None
    assert (
        next(c for c in after["mission"].spec.hard_constraints if c.key == BUDGET_KEY).value
        == 400000
    )

    async with database.session() as s:
        await MissionRepository(s).update(
            world["mission"].model_copy(update={"status": S.PLAN_OPTIONS_READY})
        )
    result = await service.apply(world["mission"], ConstraintChange(locks=["component_a"]))
    assert result.status is S.REPLAN_DECISION_RUNNING


# --- API ------------------------------------------------------------------------------


async def test_constraints_endpoint(
    client: AsyncClient, database: Database, world: dict[str, Any]
) -> None:
    url = f"/api/missions/{world['mission'].id}/constraints"
    body = {
        "updates": [{"key": BUDGET_KEY, "operator": "LE", "value": 320000}],
        "locks": ["component_b"],
        "unlocks": [],
        "preference_changes": {},
    }
    response = await client.post(url, json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["changed_keys"] == [BUDGET_KEY]
    assert data["locked_components"] == ["component_b"]
    assert data["status"] == "REPLAN_DECISION_RUNNING"
    assert data["preserved_claims"] == 2 and data["preserved_call_runs"] == 1
    assert set(data["requeued_agent_ids"]) == {
        world["allocator"].id,
        world["component_a_agent"].id,
    }
    # Not revisable any more (now REPLAN_DECISION_RUNNING).
    assert (await client.post(url, json=body)).status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"updates": [], "locks": [], "unlocks": [], "preference_changes": {}},
        {"updates": [{"key": "k", "operator": "LESS", "value": 1}]},
        {"updates": [{"key": "", "operator": "LE", "value": 1}]},
        {"updates": [{"key": "k", "operator": "LE", "value": 1, "note": "free text"}]},
        {"locks": "component_b"},
        {"preference_changes": {"k": "high"}},
        {"text": "bring it under 320000 but keep component_b"},
    ],
)
async def test_constraints_endpoint_rejects_malformed_bodies(
    client: AsyncClient, world: dict[str, Any], body: dict[str, Any]
) -> None:
    response = await client.post(f"/api/missions/{world['mission'].id}/constraints", json=body)
    assert response.status_code == 422, response.text


async def test_constraints_endpoint_unknown_mission(client: AsyncClient) -> None:
    response = await client.post(
        "/api/missions/missing/constraints", json={"locks": ["component_a"]}
    )
    assert response.status_code == 404
