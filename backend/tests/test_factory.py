"""CS-013: dynamic agent factory — code-side validation of a generated swarm."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

import callswarm.agents as agents_pkg
import callswarm.orchestrator as orchestrator_pkg
import callswarm.strategies as strategies_pkg
from callswarm.agents.factory import (
    ALLOWED_TOOLS,
    RESERVED_NAMES,
    AgentFactory,
    AgentSpecProposal,
    agent_cap,
    complexity_score,
    is_reserved_name,
    truncate_to_cap,
    validate_swarm,
)
from callswarm.config.settings import DEFAULT_PROHIBITED_AGENT_PURPOSES, Settings
from callswarm.events import ActivityEventEmitter
from callswarm.llm import FakeLLMProvider
from callswarm.models import (
    ActivityEventType,
    ConstraintOperator,
    HardConstraint,
    Mission,
    MissionSpec,
    MissionStatus,
    RiskLevel,
    SoftPreference,
    StrategyCandidate,
)
from callswarm.orchestrator.graph import GraphError, build_graph
from callswarm.orchestrator.state_machine import MissionStateMachine
from callswarm.persistence import (
    ActivityEventRepository,
    AgentSpecRepository,
    Database,
    MissionRepository,
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "finding": {"type": "string", "description": "what was found"},
        "status": {"type": "string", "enum": ["done", "unknown"]},
    },
    "required": ["finding", "status"],
    "additionalProperties": False,
}

PURPOSES = list(DEFAULT_PROHIBITED_AGENT_PURPOSES)


def prop(
    name: str,
    owns: str,
    *,
    deps: list[str] | None = None,
    tools: list[str] | None = None,
    why: str = "the mission cannot be evaluated without it",
    objective: str | None = None,
    schema: dict[str, Any] | None = None,
) -> AgentSpecProposal:
    return AgentSpecProposal(
        name=name,
        role=f"{name} role",
        objective=objective or f"produce the {owns}",
        why_needed=why,
        owns=owns,
        dependencies=deps or [],
        allowed_tools=tools if tools is not None else ["evidence.read"],
        expected_output_schema=schema or SCHEMA,
        does_not_control=["final choice"],
        stop_conditions=["output delivered"],
    )


def validate(proposals: list[AgentSpecProposal], strategies: list[StrategyCandidate] | None = None):  # type: ignore[no-untyped-def]
    return validate_swarm(
        proposals,
        mission_id="m1",
        strategies=strategies or [],
        prohibited_purposes=PURPOSES,
        overlap_threshold=0.6,
    )


# --- per-spec gates -------------------------------------------------------------


def test_tool_allow_list_rejects_unknown_tools() -> None:
    result = validate(
        [
            prop("Alpha", "alpha analysis", tools=["evidence.read", "shell.exec"]),
            prop("Beta", "beta comparison", tools=sorted(ALLOWED_TOOLS)),
            prop("Gamma", "gamma listing", tools=[]),
        ]
    )
    assert [s.name for s in result.accepted] == ["Beta", "Gamma"]
    assert result.rejections[0].name == "Alpha"
    assert "shell.exec" in result.rejections[0].reason
    assert {
        "research.search",
        "research.fetch_public_page",
        "evidence.read",
        "evidence.write_claim",
        "calls.request_intent",
        "orchestrator.request_agent",
    } == ALLOWED_TOOLS


@pytest.mark.parametrize(
    "name",
    [
        "Orchestrator",
        "the Main Orchestrator",
        "Strategy Architect",
        "Call Strategy agent",
        "Evidence Engine",
        "optimizer",
        "CRITIC",
    ],
)
def test_reserved_framework_names_are_rejected(name: str) -> None:
    assert is_reserved_name(name)
    result = validate([prop(name, "something"), prop("Fine", "other thing")])
    assert [s.name for s in result.accepted] == ["Fine"]
    assert result.rejections[0].reason == "reserved framework component name"
    assert len(RESERVED_NAMES) == 7


def test_reserved_check_is_exact_not_substring() -> None:
    assert not is_reserved_name("Budget Optimizer")
    assert not is_reserved_name("Timeline Critic Reviewer")


@pytest.mark.parametrize(
    "objective",
    [
        "Provide a medical diagnosis from the symptoms described",
        "Give legal advice on the contract",
        "Execute financial trading on the account",
        "Collect the one-time password from the recipient",
        "Impersonation of a bank representative",
        "Debt collection from listed contacts",
        "Political persuasion of the contacted households",
    ],
)
def test_prohibited_purposes_are_rejected(objective: str) -> None:
    result = validate(
        [prop("Risky", "a risky thing", objective=objective), prop("Ok", "safe work")]
    )
    assert [s.name for s in result.accepted] == ["Ok"]
    assert result.rejections[0].reason.startswith("prohibited purpose:")


def test_exclusions_in_does_not_control_do_not_trigger_the_boundary() -> None:
    spec = prop("Matcher", "category matching")
    spec = spec.model_copy(update={"does_not_control": ["legal advice of any kind"]})
    assert validate([spec]).accepted[0].name == "Matcher"


def test_invalid_output_schema_is_rejected() -> None:
    bad = {"type": "object", "properties": {"x": {"$ref": "#/foo"}}, "additionalProperties": False}
    open_schema = {"type": "object", "properties": {}, "additionalProperties": True}
    result = validate(
        [prop("A", "a", schema=bad), prop("B", "b", schema=open_schema), prop("C", "c")]
    )
    assert [s.name for s in result.accepted] == ["C"]
    assert all("invalid output schema" in r.reason for r in result.rejections)


def test_duplicate_names_are_rejected() -> None:
    result = validate([prop("Same", "one thing"), prop("same ", "another thing")])
    assert len(result.accepted) == 1 and result.rejections[0].reason.startswith("duplicate")


# --- overlap -----------------------------------------------------------------------


def test_overlapping_ownership_is_merged_unless_justified() -> None:
    first = prop("Cost Analyst", "total cost comparison across candidates", tools=["evidence.read"])
    dup = prop(
        "Price Analyst",
        "cost comparison across candidates",
        tools=["evidence.write_claim"],
        deps=["Cost Analyst"],
    )
    justified = prop(
        "Delivery Cost Analyst",
        "cost comparison across candidates for delivery only",
        why="Cost Analyst covers list prices; this one owns delivery surcharges separately",
    )
    downstream = prop("Reporter", "final report", deps=["Price Analyst"])
    result = validate([first, dup, justified, downstream])
    names = [s.name for s in result.accepted]
    assert names == ["Cost Analyst", "Delivery Cost Analyst", "Reporter"]
    assert result.merges[0].dropped == "Price Analyst" and result.merges[0].into == "Cost Analyst"
    merged = result.accepted[0]
    assert merged.allowed_tools == ["evidence.read", "evidence.write_claim"]
    assert "cost comparison across candidates" in merged.owns
    reporter = result.accepted[2]
    assert reporter.dependencies == [merged.id]  # rewired through the merge


# --- graph --------------------------------------------------------------------------


def test_unresolved_dependency_rejects_spec_and_its_dependents() -> None:
    result = validate(
        [
            prop("A", "a work"),
            prop("B", "b work", deps=["Ghost"]),
            prop("C", "c work", deps=["B"]),
        ]
    )
    assert [s.name for s in result.accepted] == ["A"]
    reasons = {r.name: r.reason for r in result.rejections}
    assert "Ghost" in reasons["B"] and "unresolved" in reasons["C"]


def test_dependency_cycle_is_rejected() -> None:
    result = validate(
        [
            prop("A", "a work", deps=["C"]),
            prop("B", "b work", deps=["A"]),
            prop("C", "c work", deps=["B"]),
            prop("D", "d work"),
            prop("E", "e work", deps=["E"]),  # self-reference is dropped, not fatal
        ]
    )
    assert sorted(s.name for s in result.accepted) == ["D", "E"]
    assert {r.name for r in result.rejections} == {"A", "B", "C"}
    assert all("cycle" in r.reason for r in result.rejections)
    accepted_ids = {s.name: s.id for s in result.accepted}
    assert result.accepted[[s.name for s in result.accepted].index("E")].dependencies == []
    with pytest.raises(GraphError):
        build_graph(
            [
                *result.accepted,
                result.accepted[0].model_copy(update={"id": "z", "dependencies": ["nope"]}),
            ]
        )
    assert accepted_ids


def test_graph_ready_and_downstream() -> None:
    result = validate([prop("A", "a"), prop("B", "b", deps=["A"]), prop("C", "c", deps=["B"])])
    a, b, c = (s.id for s in result.accepted)
    graph = build_graph(result.accepted)
    assert graph.ready([]) == [a]
    assert graph.ready([a]) == [b]
    assert graph.downstream(a) == {b, c}
    assert graph.leaves() == [c]
    assert graph.topological_order() == [a, b, c]


# --- complexity cap ------------------------------------------------------------------


def test_complexity_score_and_cap_bands() -> None:
    spec = MissionSpec(
        mission_id="m",
        objectives=["find candidates", "compare cost"],
        hard_constraints=[
            HardConstraint(key="budget", operator=ConstraintOperator.LE, value=1),
            HardConstraint(key="deadline", operator=ConstraintOperator.LE, value="x"),
        ],
        soft_preferences=[SoftPreference(key="compare cost")],  # same category as an objective
        priority_weights={"speed": 1.0},
    )
    strategies = [StrategyCandidate(mission_id="m", title="t")]
    assert complexity_score(spec, strategies) == 2 + 3 + 1
    assert [agent_cap(n) for n in (0, 3, 4, 6, 7, 9, 10, 50)] == [3, 3, 5, 5, 7, 7, 9, 9]


def test_truncate_drops_leaves_last_first() -> None:
    result = validate(
        [prop("A", "a"), prop("B", "b", deps=["A"]), prop("C", "c", deps=["A"]), prop("D", "d")]
    )
    kept, dropped = truncate_to_cap(result.accepted, 2)
    assert [s.name for s in dropped] == ["D", "C"]
    assert [s.name for s in kept] == ["A", "B"]


@pytest.fixture
def factory(
    database: Database,
    emitter: ActivityEventEmitter,
    fake_llm: FakeLLMProvider,
    settings: Settings,
    state_machine: MissionStateMachine,
) -> AgentFactory:
    return AgentFactory(database, emitter, fake_llm, settings, state_machine)


async def test_design_swarm_enforces_cap_after_one_reduction_request(
    factory: AgentFactory, fake_llm: FakeLLMProvider, database: Database, mission: Mission
) -> None:
    spec = MissionSpec(
        mission_id=mission.id,
        summary="small",
        hard_constraints=[
            HardConstraint(key="budget", operator=ConstraintOperator.LE, value=100),
            HardConstraint(key="count", operator=ConstraintOperator.EQ, value=2),
        ],
    )
    strategies = [StrategyCandidate(mission_id=mission.id, title="Only way")]
    assert agent_cap(complexity_score(spec, strategies)) == 3
    async with database.session() as s:
        await MissionRepository(s).update(
            mission.model_copy(update={"spec": spec, "status": MissionStatus.STRATEGY_SET_READY})
        )
    topics = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "india"]
    eight = [
        prop(f"Agent {i}", f"{topic} assessment").model_dump(mode="json")
        | {"strategy_title": "Only way", "risk_level": "LOW"}
        for i, topic in enumerate(topics)
    ]
    fake_llm.enqueue({"agents": eight})  # first proposal: 8
    fake_llm.enqueue({"agents": eight})  # asked once to reduce; still 8 -> truncated by code
    specs = await factory.design_swarm(spec, strategies)
    assert len(specs) == 3
    assert fake_llm.remaining == 0
    assert fake_llm.calls[0].inputs["max_agents"] == "3"
    assert "current_team" in fake_llm.calls[1].inputs
    assert all(s.strategy_id == strategies[0].id for s in specs)
    async with database.session() as s:
        stored = await AgentSpecRepository(s).list_by_mission(mission.id)
        m = await MissionRepository(s).get(mission.id)
        events = await ActivityEventRepository(s).list_by_mission(mission.id)
    assert len(stored) == 3 and m is not None and m.status is MissionStatus.SWARM_READY
    created = [e for e in events if e.event_type is ActivityEventType.AGENT_CREATED]
    assert {e.agent_id for e in created} == {s.id for s in specs}
    assert sum("dropped to respect the cap" in e.summary for e in events) == 5


async def test_design_swarm_reports_rejections_as_events(
    factory: AgentFactory, fake_llm: FakeLLMProvider, database: Database, mission: Mission
) -> None:
    spec = MissionSpec(mission_id=mission.id, summary="s", objectives=["obtain"])
    async with database.session() as s:
        await MissionRepository(s).update(
            mission.model_copy(update={"spec": spec, "status": MissionStatus.STRATEGY_SET_READY})
        )
    fake_llm.enqueue(
        {
            "agents": [
                prop("Critic", "review").model_dump(mode="json"),
                prop("Hacker", "x", tools=["shell"]).model_dump(mode="json"),
                prop("Worker", "the actual work", tools=["evidence.write_claim"]).model_dump(
                    mode="json"
                )
                | {"risk_level": RiskLevel.MEDIUM.value},
            ]
        }
    )
    specs = await factory.design_swarm(spec, [])
    assert [s.name for s in specs] == ["Worker"]
    assert specs[0].risk_level is RiskLevel.MEDIUM and specs[0].owns == "the actual work"
    async with database.session() as s:
        events = await ActivityEventRepository(s).list_by_mission(mission.id)
    rejected = [e.payload for e in events if "rejected" in e.summary]
    assert [r["name"] for r in rejected] == ["Critic", "Hacker"]


# --- static: framework code names no domain -----------------------------------------------

DOMAIN_PATTERN = re.compile(
    r"venue|caterer|\bgpu\b|\bcpu\b|motherboard|hotel|law firm|lawyer|photographer|wedding|"
    r"anniversary|prospect|\blead\b|café|cafe",
    re.IGNORECASE,
)


@pytest.mark.parametrize(
    "package", [agents_pkg, strategies_pkg, orchestrator_pkg], ids=lambda p: p.__name__
)
def test_framework_source_contains_no_domain_nouns(package: Any) -> None:
    package_dir = Path(package.__file__).parent
    offenders: list[str] = []
    for path in sorted(package_dir.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if DOMAIN_PATTERN.search(line):
                offenders.append(f"{path.relative_to(package_dir.parent)}:{lineno}: {line.strip()}")
    assert offenders == []
