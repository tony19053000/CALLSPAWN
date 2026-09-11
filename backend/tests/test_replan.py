"""CS-041: the replanning engine — the model proposes, code validates and applies."""

from __future__ import annotations

from typing import Any

import pytest

from callswarm.agents.factory import AgentFactory
from callswarm.agents.runner import AgentRunner
from callswarm.agents.tools import default_tools
from callswarm.config.settings import Settings
from callswarm.events import ActivityEventEmitter
from callswarm.evidence import EvidenceEngine
from callswarm.llm import FakeLLMProvider
from callswarm.models import (
    ActivityEvent,
    AgentRun,
    AgentSpec,
    AgentState,
    AuthorityPolicy,
    CallBudget,
    EvidenceClaim,
    HardConstraint,
    InformationGap,
    Mission,
    MissionSpec,
    MissionStatus,
    PlanOption,
    ReplanAction,
    ReplanDecision,
    ReplanTrigger,
    SourceType,
    StrategyCandidate,
    StrategyStatus,
)
from callswarm.models.mission import ConstraintOperator
from callswarm.orchestrator.replan import (
    IN_PLACE_ACTIONS,
    TARGET_STATE,
    ReplanEngine,
    ReplanNotReadyError,
)
from callswarm.orchestrator.state_machine import MissionStateMachine, is_allowed
from callswarm.persistence import (
    ActivityEventRepository,
    AgentRunRepository,
    AgentSpecRepository,
    Database,
    InformationGapRepository,
    MissionRepository,
    PlanOptionRepository,
    ReplanDecisionRepository,
    StrategyCandidateRepository,
)
from callswarm.sanitize import Sanitizer
from callswarm.strategies.architect import StrategyArchitect

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "finding": {"type": "string"},
        "status": {"type": "string", "enum": ["done", "unknown"]},
    },
    "required": ["finding", "status"],
    "additionalProperties": False,
}


def specialist(name: str, owns: str, tools: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "role": f"{name} role",
        "objective": f"produce the {owns}",
        "why_needed": "the mission cannot be evaluated without it",
        "owns": owns,
        "required_inputs": [],
        "dependencies": [],
        "allowed_tools": tools if tools is not None else ["evidence.read"],
        "expected_output_schema": SCHEMA,
        "does_not_control": ["final choice"],
        "stop_conditions": ["output delivered"],
        "risk_level": "LOW",
    }


def proposal(
    action: str, rationale: str = "Because the evidence changed.", **kw: Any
) -> dict[str, Any]:
    return {"action": action, "rationale": rationale, **kw}


@pytest.fixture
async def engine(
    database: Database,
    emitter: ActivityEventEmitter,
    fake_llm: FakeLLMProvider,
    settings: Settings,
    state_machine: MissionStateMachine,
) -> ReplanEngine:
    factory = AgentFactory(database, emitter, fake_llm, settings, state_machine)
    architect = StrategyArchitect(database, emitter, fake_llm, settings, state_machine)
    runner = AgentRunner(database, emitter, fake_llm, settings, default_tools(), Sanitizer())
    return ReplanEngine(
        database, emitter, fake_llm, settings, state_machine, factory, architect, runner
    )


@pytest.fixture
async def world(database: Database) -> dict[str, Any]:
    """A mission at REPLAN_DECISION_RUNNING with two strategies (one pruned),
    two agents, one open gap, a plan option and some claims."""
    async with database.session() as s:
        mission = await MissionRepository(s).add(
            Mission(
                user_goal="test goal",
                status=MissionStatus.REPLAN_DECISION_RUNNING,
                authority_policy=AuthorityPolicy(calls_allowed=True, max_call_count=2),
                call_budget=CallBudget(max_calls=2, calls_used=0),
            )
        )
        spec = MissionSpec(
            mission_id=mission.id,
            summary="s",
            objectives=["objective one"],
            hard_constraints=[
                HardConstraint(key="max_total_cost", operator=ConstraintOperator.LE, value=100)
            ],
        )
        mission = await MissionRepository(s).update(mission.model_copy(update={"spec": spec}))
        strategies = StrategyCandidateRepository(s)
        active = await strategies.add(
            StrategyCandidate(
                mission_id=mission.id,
                title="Separate components",
                status=StrategyStatus.ACTIVE,
                required_information=["component_a options", "component_b options"],
            )
        )
        pruned = await strategies.add(
            StrategyCandidate(
                mission_id=mission.id,
                title="Combined package",
                status=StrategyStatus.PRUNED,
                status_reason="no package offers found",
                required_information=["package_discount"],
            )
        )
        specs = AgentSpecRepository(s)
        agent_a = await specs.add(
            AgentSpec(
                mission_id=mission.id,
                name="Alpha",
                role="r",
                objective="component_a options",
                why_needed="w",
                owns="component_a options",
                strategy_id=active.id,
                allowed_tools=["evidence.read"],
                expected_output_schema=SCHEMA,
                state=AgentState.COMPLETE,
            )
        )
        agent_b = await specs.add(
            AgentSpec(
                mission_id=mission.id,
                name="Beta",
                role="r",
                objective="component_b options",
                why_needed="w",
                owns="component_b options",
                strategy_id=active.id,
                allowed_tools=["evidence.read"],
                expected_output_schema=SCHEMA,
                state=AgentState.COMPLETE,
            )
        )
        runs = AgentRunRepository(s)
        run_a = await runs.add(
            AgentRun(
                mission_id=mission.id,
                agent_id=agent_a.id,
                status=AgentState.COMPLETE,
                output_artifact={"finding": "x", "status": "done"},
            )
        )
        gap = await InformationGapRepository(s).add(
            InformationGap(mission_id=mission.id, question="what is component_a's cost?")
        )
        plan = await PlanOptionRepository(s).add(
            PlanOption(mission_id=mission.id, name="plan 1", total_cost=90)
        )
    return {
        "mission": mission,
        "active": active,
        "pruned": pruned,
        "agent_a": agent_a,
        "agent_b": agent_b,
        "run_a": run_a,
        "gap": gap,
        "plan": plan,
    }


async def decisions(database: Database, mission_id: str) -> list[ReplanDecision]:
    async with database.session() as s:
        return await ReplanDecisionRepository(s).list_by_mission(mission_id)


async def status(database: Database, mission_id: str) -> MissionStatus:
    async with database.session() as s:
        mission = await MissionRepository(s).get(mission_id)
    assert mission is not None
    return mission.status


async def events(database: Database, mission_id: str) -> list[ActivityEvent]:
    async with database.session() as s:
        return await ActivityEventRepository(s).list_by_mission(mission_id)


# --- guards ---------------------------------------------------------------------------


def test_every_transitioning_action_is_in_the_state_table() -> None:
    for action, target in TARGET_STATE.items():
        assert action not in IN_PLACE_ACTIONS
        assert is_allowed(MissionStatus.REPLAN_DECISION_RUNNING, target), action
    assert set(TARGET_STATE) | IN_PLACE_ACTIONS == set(ReplanAction)


async def test_decide_requires_replan_state(
    engine: ReplanEngine, database: Database, mission: Mission
) -> None:
    with pytest.raises(ReplanNotReadyError):
        await engine.decide(mission, ReplanTrigger.NEW_EVIDENCE)


# --- each action ----------------------------------------------------------------------


async def test_proceed_moves_to_optimization_and_is_persisted(
    engine: ReplanEngine, fake_llm: FakeLLMProvider, database: Database, world: dict[str, Any]
) -> None:
    fake_llm.enqueue(proposal("PROCEED_TO_OPTIMIZATION", "Enough evidence."))
    decision = await engine.decide(world["mission"], ReplanTrigger.NEW_EVIDENCE, refs=["c1"])
    assert decision.action is ReplanAction.PROCEED_TO_OPTIMIZATION
    assert decision.trigger is ReplanTrigger.NEW_EVIDENCE and decision.trigger_refs == ["c1"]
    assert decision.applied_at is not None and "OPTIMIZATION_RUNNING" in decision.outcome
    assert decision.rewrite_reason is None
    assert await status(database, world["mission"].id) is MissionStatus.OPTIMIZATION_RUNNING
    stored = await decisions(database, world["mission"].id)
    assert [d.id for d in stored] == [decision.id]
    async with database.session() as s:
        plan = await PlanOptionRepository(s).get(world["plan"].id)
    assert plan is not None and plan.stale is False  # proceeding keeps the plans


async def test_rerun_agent_requeues_and_marks_its_run_stale(
    engine: ReplanEngine, fake_llm: FakeLLMProvider, database: Database, world: dict[str, Any]
) -> None:
    fake_llm.enqueue(proposal("RERUN_AGENT", agent_id=world["agent_a"].id))
    decision = await engine.decide(world["mission"], ReplanTrigger.NEW_EVIDENCE)
    assert decision.action is ReplanAction.RERUN_AGENT
    assert decision.target_id == world["agent_a"].id
    assert await status(database, world["mission"].id) is MissionStatus.RESEARCH_RUNNING
    async with database.session() as s:
        agent = await AgentSpecRepository(s).get(world["agent_a"].id)
        other = await AgentSpecRepository(s).get(world["agent_b"].id)
        run = await AgentRunRepository(s).get(world["run_a"].id)
        plan = await PlanOptionRepository(s).get(world["plan"].id)
    assert agent is not None and agent.state is AgentState.READY
    assert other is not None and other.state is AgentState.COMPLETE
    assert run is not None and run.stale and run.stale_reason
    assert plan is not None and plan.stale and "RERUN_AGENT" in (plan.stale_reason or "")


async def test_stop_agent_persists_reason_on_spec_and_run_then_asks_again(
    engine: ReplanEngine, fake_llm: FakeLLMProvider, database: Database, world: dict[str, Any]
) -> None:
    fake_llm.enqueue(
        proposal("STOP_AGENT", agent_id=world["agent_a"].id, reason="its strategy is impossible"),
        proposal("PROCEED_TO_OPTIMIZATION"),
    )
    final = await engine.decide(world["mission"], ReplanTrigger.STRATEGY_IMPOSSIBLE)
    assert final.action is ReplanAction.PROCEED_TO_OPTIMIZATION
    stored = await decisions(database, world["mission"].id)
    assert [d.action for d in stored] == [
        ReplanAction.STOP_AGENT,
        ReplanAction.PROCEED_TO_OPTIMIZATION,
    ]
    assert stored[0].details == {"reason": "its strategy is impossible"}
    async with database.session() as s:
        agent = await AgentSpecRepository(s).get(world["agent_a"].id)
        run = await AgentRunRepository(s).get(world["run_a"].id)
    assert agent is not None and agent.state is AgentState.STOPPED
    assert agent.state_reason == "stopped by Orchestrator: its strategy is impossible"
    assert run is not None and run.stop_reason == "its strategy is impossible"
    summaries = [e.summary for e in await events(database, world["mission"].id)]
    assert any("Alpha is STOPPED" in s and "impossible" in s for s in summaries)


async def test_stop_agent_with_live_run_goes_through_the_runner(
    engine: ReplanEngine, fake_llm: FakeLLMProvider, database: Database, world: dict[str, Any]
) -> None:
    async with database.session() as s:
        live = await AgentRunRepository(s).add(
            AgentRun(
                mission_id=world["mission"].id,
                agent_id=world["agent_b"].id,
                status=AgentState.WAITING_FOR_CALL,
            )
        )
    fake_llm.enqueue(
        proposal("STOP_AGENT", agent_id=world["agent_b"].id, reason="redundant"),
        proposal("PROCEED_TO_OPTIMIZATION"),
    )
    await engine.decide(world["mission"], ReplanTrigger.CALL_RESULT)
    async with database.session() as s:
        run = await AgentRunRepository(s).get(live.id)
        agent = await AgentSpecRepository(s).get(world["agent_b"].id)
    assert run is not None and run.status is AgentState.STOPPED
    assert run.stop_reason == "redundant"
    assert agent is not None and agent.state is AgentState.STOPPED


async def test_prune_strategy_stops_its_live_agents_with_the_reason(
    engine: ReplanEngine, fake_llm: FakeLLMProvider, database: Database, world: dict[str, Any]
) -> None:
    async with database.session() as s:
        specs = AgentSpecRepository(s)
        waiting = await specs.update(
            world["agent_b"].model_copy(update={"state": AgentState.WAITING_FOR_CALL})
        )
        await AgentRunRepository(s).add(
            AgentRun(
                mission_id=world["mission"].id,
                agent_id=waiting.id,
                status=AgentState.WAITING_FOR_CALL,
            )
        )
    fake_llm.enqueue(
        proposal("PRUNE_STRATEGY", strategy_id=world["active"].id, reason="no candidate fits"),
        proposal("PROCEED_TO_OPTIMIZATION"),
    )
    await engine.decide(world["mission"], ReplanTrigger.STRATEGY_IMPOSSIBLE)
    async with database.session() as s:
        strategy = await StrategyCandidateRepository(s).get(world["active"].id)
        b = await AgentSpecRepository(s).get(waiting.id)
        a = await AgentSpecRepository(s).get(world["agent_a"].id)
    assert strategy is not None and strategy.status is StrategyStatus.PRUNED
    assert strategy.status_reason == "no candidate fits"
    assert b is not None and b.state is AgentState.STOPPED
    assert "strategy pruned: no candidate fits" in (b.state_reason or "")
    assert a is not None and a.state is AgentState.COMPLETE  # finished work is kept


async def test_research_pass_and_call_round_transitions(
    engine: ReplanEngine, fake_llm: FakeLLMProvider, database: Database, world: dict[str, Any]
) -> None:
    fake_llm.enqueue(proposal("RESEARCH_PASS", queries=["open question one", " "]))
    decision = await engine.decide(world["mission"], ReplanTrigger.NEW_EVIDENCE)
    assert decision.action is ReplanAction.RESEARCH_PASS
    assert decision.details == {"queries": ["open question one"]}
    assert await status(database, world["mission"].id) is MissionStatus.RESEARCH_RUNNING

    async with database.session() as s:
        await MissionRepository(s).update(
            world["mission"].model_copy(update={"status": MissionStatus.REPLAN_DECISION_RUNNING})
        )
    fake_llm.enqueue(proposal("CALL_ROUND", gap_ids=[world["gap"].id]))
    decision = await engine.decide(world["mission"], ReplanTrigger.CONFLICT)
    assert decision.action is ReplanAction.CALL_ROUND
    assert decision.details == {"gap_ids": [world["gap"].id]}
    assert await status(database, world["mission"].id) is MissionStatus.CALL_SELECTION_RUNNING


# --- validation -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        proposal("RERUN_AGENT", agent_id="not-an-agent"),
        proposal("STOP_AGENT", agent_id="not-an-agent", reason="x"),
        proposal("PRUNE_STRATEGY", strategy_id="not-a-strategy", reason="x"),
        proposal("REVIVE_STRATEGY", strategy_id="not-a-strategy", evidence_ref="c"),
        proposal("CALL_ROUND", gap_ids=["not-a-gap"]),
        proposal("RESEARCH_PASS", queries=[]),
        proposal("CREATE_SPECIALIST"),
        {"action": "BOOK_IT", "rationale": "not in the enum"},
    ],
    ids=lambda p: f"{p['action']}",
)
async def test_invalid_proposals_are_rejected_then_retried_once(
    engine: ReplanEngine,
    fake_llm: FakeLLMProvider,
    database: Database,
    world: dict[str, Any],
    bad: dict[str, Any],
) -> None:
    fake_llm.enqueue(bad, proposal("PROCEED_TO_OPTIMIZATION", "Corrected."))
    decision = await engine.decide(world["mission"], ReplanTrigger.CRITIC_FAIL)
    assert decision.action is ReplanAction.PROCEED_TO_OPTIMIZATION
    assert decision.rewrite_reason is None  # the retry was accepted as-is
    assert fake_llm.remaining == 0
    assert "validation errors" in fake_llm.calls[1].inputs
    assert any("rejected" in e.summary for e in await events(database, world["mission"].id))


async def test_invalid_twice_forces_proceed_with_recorded_reason(
    engine: ReplanEngine, fake_llm: FakeLLMProvider, database: Database, world: dict[str, Any]
) -> None:
    fake_llm.enqueue(
        proposal("RERUN_AGENT", agent_id="ghost"), proposal("RERUN_AGENT", agent_id="ghost")
    )
    decision = await engine.decide(world["mission"], ReplanTrigger.NEW_EVIDENCE)
    assert decision.action is ReplanAction.PROCEED_TO_OPTIMIZATION
    assert decision.proposed_action is ReplanAction.RERUN_AGENT
    assert decision.rewrite_reason and "proposal invalid after retry" in decision.rewrite_reason
    assert await status(database, world["mission"].id) is MissionStatus.OPTIMIZATION_RUNNING


async def test_ids_from_another_mission_are_rejected(
    engine: ReplanEngine, fake_llm: FakeLLMProvider, database: Database, world: dict[str, Any]
) -> None:
    async with database.session() as s:
        other = await MissionRepository(s).add(Mission(user_goal="other"))
        foreign = await AgentSpecRepository(s).add(
            AgentSpec(
                mission_id=other.id, name="F", role="r", objective="o", why_needed="w", owns="o"
            )
        )
    fake_llm.enqueue(
        proposal("RERUN_AGENT", agent_id=foreign.id), proposal("PROCEED_TO_OPTIMIZATION")
    )
    decision = await engine.decide(world["mission"], ReplanTrigger.NEW_EVIDENCE)
    assert decision.action is ReplanAction.PROCEED_TO_OPTIMIZATION
    async with database.session() as s:
        stored = await AgentSpecRepository(s).get(foreign.id)
    assert stored is not None and stored.state is AgentState.CREATED


async def test_call_round_over_budget_is_rewritten_to_proceed_with_reason(
    engine: ReplanEngine, fake_llm: FakeLLMProvider, database: Database, world: dict[str, Any]
) -> None:
    async with database.session() as s:
        await MissionRepository(s).update(
            world["mission"].model_copy(
                update={"call_budget": CallBudget(max_calls=2, calls_used=2)}
            )
        )
    fake_llm.enqueue(proposal("CALL_ROUND", gap_ids=[world["gap"].id]))
    decision = await engine.decide(world["mission"], ReplanTrigger.CONFLICT)
    assert decision.action is ReplanAction.PROCEED_TO_OPTIMIZATION
    assert decision.proposed_action is ReplanAction.CALL_ROUND
    assert decision.rewrite_reason and "budget exhausted (2/2)" in decision.rewrite_reason
    assert decision.details == {"requested_gap_ids": [world["gap"].id]}
    assert await status(database, world["mission"].id) is MissionStatus.OPTIMIZATION_RUNNING


async def test_call_round_without_call_permission_is_rewritten(
    engine: ReplanEngine, fake_llm: FakeLLMProvider, database: Database, world: dict[str, Any]
) -> None:
    async with database.session() as s:
        await MissionRepository(s).update(
            world["mission"].model_copy(
                update={"authority_policy": AuthorityPolicy(calls_allowed=False)}
            )
        )
    fake_llm.enqueue(proposal("CALL_ROUND", gap_ids=[world["gap"].id]))
    decision = await engine.decide(world["mission"], ReplanTrigger.CONFLICT)
    assert decision.action is ReplanAction.PROCEED_TO_OPTIMIZATION
    assert (
        decision.rewrite_reason and "not allowed by the authority policy" in decision.rewrite_reason
    )


# --- CREATE_SPECIALIST through the factory ---------------------------------------------


async def test_create_specialist_goes_through_factory_validation(
    engine: ReplanEngine, fake_llm: FakeLLMProvider, database: Database, world: dict[str, Any]
) -> None:
    # A tool outside the allow-list is rejected by the factory; the decision
    # stays on record as rejected and the engine asks again.
    fake_llm.enqueue(
        proposal(
            "CREATE_SPECIALIST",
            specialist=specialist("Rogue", "something new", tools=["shell.exec"]),
        ),
        proposal("PROCEED_TO_OPTIMIZATION"),
    )
    await engine.decide(world["mission"], ReplanTrigger.CALL_RESULT)
    stored = await decisions(database, world["mission"].id)
    assert stored[0].action is ReplanAction.CREATE_SPECIALIST
    assert "rejected" in stored[0].outcome and "tools not permitted" in stored[0].outcome
    assert stored[1].action is ReplanAction.PROCEED_TO_OPTIMIZATION
    async with database.session() as s:
        names = {a.name for a in await AgentSpecRepository(s).list_by_mission(world["mission"].id)}
    assert names == {"Alpha", "Beta"}


async def test_create_specialist_overlap_with_existing_agent_is_rejected(
    engine: ReplanEngine, fake_llm: FakeLLMProvider, database: Database, world: dict[str, Any]
) -> None:
    fake_llm.enqueue(
        proposal("CREATE_SPECIALIST", specialist=specialist("A2", "component_a options")),
        proposal("PROCEED_TO_OPTIMIZATION"),
    )
    await engine.decide(world["mission"], ReplanTrigger.CALL_RESULT)
    stored = await decisions(database, world["mission"].id)
    assert "overlaps existing specialist 'Alpha'" in stored[0].outcome


async def test_create_specialist_respects_cap_counting_live_agents(
    engine: ReplanEngine,
    fake_llm: FakeLLMProvider,
    database: Database,
    world: dict[str, Any],
) -> None:
    # complexity: 1 hard constraint + 1 objective + 1 surviving strategy = 3 -> cap 3.
    async with database.session() as s:
        await AgentSpecRepository(s).add(
            AgentSpec(
                mission_id=world["mission"].id,
                name="C",
                role="r",
                objective="delivery timing",
                why_needed="w",
                owns="delivery timing",
                expected_output_schema=SCHEMA,
                state=AgentState.COMPLETE,
            )
        )
    fake_llm.enqueue(
        proposal("CREATE_SPECIALIST", specialist=specialist("D", "warranty terms")),
        proposal("PROCEED_TO_OPTIMIZATION"),
    )
    await engine.decide(world["mission"], ReplanTrigger.NEW_EVIDENCE)
    stored = await decisions(database, world["mission"].id)
    assert "agent cap 3 reached: 3 live specialist(s)" in stored[0].outcome

    # A stopped agent no longer counts against the cap.
    async with database.session() as s:
        await AgentSpecRepository(s).update(
            world["agent_b"].model_copy(update={"state": AgentState.STOPPED})
        )
        await MissionRepository(s).update(
            world["mission"].model_copy(update={"status": MissionStatus.REPLAN_DECISION_RUNNING})
        )
    fake_llm.enqueue(proposal("CREATE_SPECIALIST", specialist=specialist("D", "warranty terms")))
    decision = await engine.decide(world["mission"], ReplanTrigger.NEW_EVIDENCE)
    assert decision.action is ReplanAction.CREATE_SPECIALIST
    assert decision.target_id is not None
    async with database.session() as s:
        created = await AgentSpecRepository(s).get(decision.target_id)
    assert created is not None and created.name == "D"


# --- the bundle scenario ---------------------------------------------------------------


async def test_bundle_scenario_revives_strategy_and_creates_specialist_through_factory(
    engine: ReplanEngine,
    fake_llm: FakeLLMProvider,
    database: Database,
    emitter: ActivityEventEmitter,
    world: dict[str, Any],
) -> None:
    """A call result carries a predicate (package_discount) no current strategy
    required. The model revives the pruned strategy on that claim and creates
    the specialist it needs; code creates it through the factory and marks the
    existing plan stale."""
    evidence = EvidenceEngine(database, emitter)
    ingested = await evidence.ingest(
        [
            EvidenceClaim(
                mission_id=world["mission"].id,
                subject="Candidate X",
                predicate="package_discount",
                value=15,
                source_type=SourceType.SIMULATED,
                source_reference="call_run:r1",
            )
        ],
        source="call_run:r1",
    )
    claim_id = ingested.claims[0].id
    fake_llm.enqueue(
        proposal(
            "REVIVE_STRATEGY",
            "The call stated a package discount, which the combined strategy needs.",
            strategy_id=world["pruned"].id,
            evidence_ref=claim_id,
        ),
        proposal(
            "CREATE_SPECIALIST",
            "No current agent owns package pricing; a specialist is needed.",
            strategy_id=world["pruned"].id,
            specialist=specialist("Package pricing", "package_discount evaluation"),
        ),
    )
    final = await engine.decide(world["mission"], ReplanTrigger.CALL_RESULT, refs=[claim_id])
    assert final.action is ReplanAction.CREATE_SPECIALIST
    assert fake_llm.remaining == 0
    stored = await decisions(database, world["mission"].id)
    assert [d.action for d in stored] == [
        ReplanAction.REVIVE_STRATEGY,
        ReplanAction.CREATE_SPECIALIST,
    ]
    assert all(d.trigger is ReplanTrigger.CALL_RESULT for d in stored)
    assert all(d.trigger_refs == [claim_id] for d in stored)
    assert all(d.applied_at is not None for d in stored)
    async with database.session() as s:
        strategy = await StrategyCandidateRepository(s).get(world["pruned"].id)
        agents = await AgentSpecRepository(s).list_by_mission(world["mission"].id)
        plan = await PlanOptionRepository(s).get(world["plan"].id)
    assert strategy is not None and strategy.status is StrategyStatus.ACTIVE
    assert strategy.revival_evidence_ref == claim_id
    created = next(a for a in agents if a.name == "Package pricing")
    assert created.id == stored[1].target_id
    assert created.strategy_id == world["pruned"].id
    assert created.allowed_tools == ["evidence.read"]  # validated grant
    assert created.state is AgentState.CREATED
    assert plan is not None and plan.stale is True and plan.stale_reason
    assert await status(database, world["mission"].id) is MissionStatus.SWARM_READY
    summaries = [e.summary for e in await events(database, world["mission"].id)]
    assert any("Specialist created: Package pricing" in s for s in summaries)
    assert any("Strategy revived" in s for s in summaries)
    # The model's rationale is the activity line; no hidden reasoning field exists.
    assert any("package discount" in s for s in summaries)


# --- loop guard -----------------------------------------------------------------------


async def test_loop_guard_trips_on_consecutive_identical_actions(
    engine: ReplanEngine, fake_llm: FakeLLMProvider, database: Database, world: dict[str, Any]
) -> None:
    async with database.session() as s:
        for _ in range(3):
            await ReplanDecisionRepository(s).add(
                ReplanDecision(
                    mission_id=world["mission"].id,
                    trigger=ReplanTrigger.NEW_EVIDENCE,
                    action=ReplanAction.RERUN_AGENT,
                    target_id=world["agent_a"].id,
                    outcome="done",
                )
            )
    fake_llm.enqueue(proposal("RERUN_AGENT", agent_id=world["agent_a"].id))
    decision = await engine.decide(world["mission"], ReplanTrigger.NEW_EVIDENCE)
    assert decision.action is ReplanAction.PROCEED_TO_OPTIMIZATION
    assert decision.proposed_action is ReplanAction.RERUN_AGENT
    assert decision.rewrite_reason and "loop guard" in decision.rewrite_reason
    assert "4 times in a row" in decision.rewrite_reason
    assert await status(database, world["mission"].id) is MissionStatus.OPTIMIZATION_RUNNING


async def test_loop_guard_trips_on_per_mission_maximum_without_asking_the_model(
    engine: ReplanEngine, fake_llm: FakeLLMProvider, database: Database, world: dict[str, Any]
) -> None:
    async with database.session() as s:
        for i in range(12):
            await ReplanDecisionRepository(s).add(
                ReplanDecision(
                    mission_id=world["mission"].id,
                    trigger=ReplanTrigger.NEW_EVIDENCE,
                    action=ReplanAction.STOP_AGENT if i % 2 else ReplanAction.RESEARCH_PASS,
                    outcome="done",
                )
            )
    decision = await engine.decide(world["mission"], ReplanTrigger.CRITIC_FAIL)
    assert decision.action is ReplanAction.PROCEED_TO_OPTIMIZATION
    assert (
        decision.rewrite_reason
        and "12 replan decisions already recorded" in decision.rewrite_reason
    )
    assert fake_llm.calls == []


async def test_in_place_rounds_are_bounded(
    engine: ReplanEngine,
    fake_llm: FakeLLMProvider,
    database: Database,
    emitter: ActivityEventEmitter,
    world: dict[str, Any],
) -> None:
    # Alternate prune/revive forever: the per-call round limit ends it.
    a, p = world["active"].id, world["pruned"].id
    async with database.session() as s:
        await StrategyCandidateRepository(s).add(
            StrategyCandidate(
                mission_id=world["mission"].id, title="third", status=StrategyStatus.ACTIVE
            )
        )
    async with database.session() as s:
        strategies = await StrategyCandidateRepository(s).list_by_mission(world["mission"].id)
    third = next(x.id for x in strategies if x.title == "third")
    evidence = EvidenceEngine(database, emitter)
    ref = (
        (
            await evidence.ingest(
                [
                    EvidenceClaim(
                        mission_id=world["mission"].id,
                        subject="s",
                        predicate="p",
                        value=1,
                        source_type=SourceType.WEB,
                        source_reference="artifact:1",
                    )
                ],
                source="artifact:1",
            )
        )
        .claims[0]
        .id
    )
    fake_llm.enqueue(
        proposal("PRUNE_STRATEGY", strategy_id=a, reason="r"),
        proposal("REVIVE_STRATEGY", strategy_id=p, evidence_ref=ref),
        proposal("PRUNE_STRATEGY", strategy_id=third, reason="r"),
        proposal("REVIVE_STRATEGY", strategy_id=a, evidence_ref=ref),
        proposal("PRUNE_STRATEGY", strategy_id=p, reason="r"),
        proposal("REVIVE_STRATEGY", strategy_id=third, evidence_ref=ref),
    )
    decision = await engine.decide(world["mission"], ReplanTrigger.NEW_EVIDENCE)
    assert decision.action is ReplanAction.PROCEED_TO_OPTIMIZATION
    assert (
        decision.rewrite_reason
        and "in-place actions in one decision round" in decision.rewrite_reason
    )
    assert fake_llm.remaining == 1
    assert len(await decisions(database, world["mission"].id)) == 6
