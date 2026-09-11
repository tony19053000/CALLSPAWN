"""CS-012: strategy generation, diversity enforcement, prune and revive."""

from __future__ import annotations

from typing import Any

import pytest

from callswarm.config.settings import Settings
from callswarm.events import ActivityEventEmitter
from callswarm.llm import FakeLLMProvider
from callswarm.models import (
    ActivityEventType,
    Mission,
    MissionSpec,
    MissionStatus,
    StrategyStatus,
)
from callswarm.orchestrator.state_machine import IllegalTransition, MissionStateMachine
from callswarm.persistence import (
    ActivityEventRepository,
    Database,
    MissionRepository,
    StrategyCandidateRepository,
)
from callswarm.strategies import (
    StrategyArchitect,
    StrategyGenerationFailed,
    StrategyProposal,
    validate_set,
)
from callswarm.strategies.architect import replacements_needed
from callswarm.strategies.diversity import jaccard, normalize_label, tokens


def proposal(title: str, axis: str, *assumptions: str) -> dict[str, Any]:
    return {
        "title": title,
        "description": f"{title} approach",
        "objective_axis": axis,
        "assumptions": list(assumptions),
        "benefits": ["b"],
        "drawbacks": ["d"],
        "required_information": ["r"],
        "expected_dependencies": ["e"],
    }


DISTINCT = [
    proposal(
        "Single supplier for everything", "lowest total cost", "one supplier can cover all items"
    ),
    proposal("Best-in-class per item", "highest quality", "quality varies a lot per item"),
    proposal("Fewest moving parts", "least complexity", "user values simplicity over savings"),
]


@pytest.fixture
def architect(
    database: Database,
    emitter: ActivityEventEmitter,
    fake_llm: FakeLLMProvider,
    settings: Settings,
    state_machine: MissionStateMachine,
) -> StrategyArchitect:
    return StrategyArchitect(database, emitter, fake_llm, settings, state_machine)


@pytest.fixture
async def ready_mission(database: Database, mission: Mission) -> Mission:
    spec = MissionSpec(mission_id=mission.id, summary="s", objectives=["obtain items"])
    async with database.session() as s:
        return await MissionRepository(s).update(
            mission.model_copy(update={"spec": spec, "status": MissionStatus.MISSION_SPEC_READY})
        )


# --- pure diversity checks ---------------------------------------------------------


def test_tokens_and_overlap_are_lexical_and_deterministic() -> None:
    assert tokens("The suppliers are cheaper") == tokens("supplier cheap")
    assert "the" not in tokens("the supplier")
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert jaccard({"a"}, {"b"}) == 0.0
    assert normalize_label("Cost-First") == normalize_label("cost first ")


def test_validate_set_rejects_shared_axis_and_heavy_overlap() -> None:
    proposals = [StrategyProposal(**p) for p in DISTINCT] + [
        StrategyProposal(**proposal("Bundle everything", "Lowest Total Cost", "bundles exist")),
        StrategyProposal(
            **proposal(
                "Single supplier for everything, negotiated",
                "fastest completion",
                "one supplier can cover all items",
            )
        ),
    ]
    accepted, rejected = validate_set(proposals, threshold=0.6)
    assert [p.title for p in accepted] == [p["title"] for p in DISTINCT]
    assert [p.title for p, _ in rejected] == [
        "Bundle everything",
        "Single supplier for everything, negotiated",
    ]
    assert "shares objective axis" in rejected[0][1]
    assert "overlaps" in rejected[1][1]


def test_replacement_count_rules() -> None:
    assert replacements_needed(accepted_count=3, rejected_count=1) == 1
    assert replacements_needed(accepted_count=2, rejected_count=0) == 1
    assert replacements_needed(accepted_count=5, rejected_count=2) == 0
    assert replacements_needed(accepted_count=4, rejected_count=3) == 1


# --- generation ------------------------------------------------------------------


async def test_generate_rejects_duplicates_and_requests_one_replacement(
    architect: StrategyArchitect,
    fake_llm: FakeLLMProvider,
    ready_mission: Mission,
    database: Database,
) -> None:
    assert ready_mission.spec is not None
    fake_llm.enqueue(
        {"strategies": [*DISTINCT, proposal("Bundle everything", "lowest total cost", "x")]}
    )
    fake_llm.enqueue(
        {"strategies": [proposal("Wait for a better window", "lowest risk", "prices fall later")]}
    )
    candidates = await architect.generate_strategies(ready_mission.spec)
    assert [c.title for c in candidates] == [
        *(p["title"] for p in DISTINCT),
        "Wait for a better window",
    ]
    assert len({normalize_label(c.objective_axis) for c in candidates}) == 4
    assert all(c.status is StrategyStatus.PROPOSED for c in candidates)
    assert fake_llm.remaining == 0
    replacement_call = fake_llm.calls[1]
    assert replacement_call.inputs["replacements_needed"] == "1"
    assert "Bundle everything" in replacement_call.inputs["rejections"]
    async with database.session() as s:
        stored = await StrategyCandidateRepository(s).list_by_mission(ready_mission.id)
        mission = await MissionRepository(s).get(ready_mission.id)
        events = await ActivityEventRepository(s).list_by_mission(ready_mission.id)
    assert {c.id for c in stored} == {c.id for c in candidates}
    assert mission is not None and mission.status is MissionStatus.STRATEGY_SET_READY
    summaries = [e.summary for e in events if e.event_type is ActivityEventType.STRATEGY_UPDATE]
    assert any("rejected 'Bundle everything'" in s for s in summaries)
    assert any("requested 1 replacement" in s for s in summaries)
    assert sum("Strategy proposed" in s for s in summaries) == 4
    statuses = [
        e.payload["to_status"]
        for e in events
        if e.event_type is ActivityEventType.MISSION_STATUS_CHANGED
    ]
    assert statuses == ["STRATEGY_DISCOVERY_RUNNING", "STRATEGY_SET_READY"]


async def test_generate_fails_when_replacement_is_also_a_duplicate(
    architect: StrategyArchitect, fake_llm: FakeLLMProvider, ready_mission: Mission
) -> None:
    assert ready_mission.spec is not None
    fake_llm.enqueue({"strategies": [*DISTINCT[:2], proposal("Copy", "highest quality", "q")]})
    fake_llm.enqueue({"strategies": [proposal("Copy again", "Highest quality", "q")]})
    with pytest.raises(StrategyGenerationFailed):
        await architect.generate_strategies(ready_mission.spec)
    assert fake_llm.remaining == 0


async def test_generate_truncates_to_five_and_requires_spec_ready(
    architect: StrategyArchitect,
    fake_llm: FakeLLMProvider,
    ready_mission: Mission,
    mission: Mission,
) -> None:
    assert ready_mission.spec is not None
    many = [
        *DISTINCT,
        proposal("Four", "fastest completion", "f"),
        proposal("Five", "lowest risk", "g"),
        proposal("Six", "widest choice", "h"),
    ]
    fake_llm.enqueue({"strategies": many})
    candidates = await architect.generate_strategies(ready_mission.spec)
    assert len(candidates) == 5
    fake_llm.enqueue({"strategies": many})
    with pytest.raises(IllegalTransition):
        await architect.generate_strategies(ready_mission.spec)  # now STRATEGY_SET_READY


# --- prune / revive ----------------------------------------------------------------


async def test_prune_and_revive_persist_reasons(
    architect: StrategyArchitect,
    fake_llm: FakeLLMProvider,
    ready_mission: Mission,
    database: Database,
) -> None:
    assert ready_mission.spec is not None
    fake_llm.enqueue({"strategies": DISTINCT})
    candidates = await architect.generate_strategies(ready_mission.spec)
    target = candidates[0]
    pruned = await architect.prune(target.id, "no supplier covers every item")
    assert pruned.status is StrategyStatus.PRUNED
    assert pruned.status_reason == "no supplier covers every item"
    assert len(await architect.surviving(ready_mission.id)) == 2
    with pytest.raises(ValueError, match="cannot move"):
        await architect.prune(target.id, "twice")
    with pytest.raises(ValueError, match="evidence"):
        await architect.revive(target.id, "found one", "")
    revived = await architect.revive(target.id, "one supplier quoted all items", "claim:abc")
    assert revived.status is StrategyStatus.ACTIVE
    assert revived.status_reason == "one supplier quoted all items"
    assert revived.revival_evidence_ref == "claim:abc"
    async with database.session() as s:
        stored = await StrategyCandidateRepository(s).get(target.id)
        events = await ActivityEventRepository(s).list_by_mission(ready_mission.id)
    assert stored == revived
    summaries = [e.summary for e in events]
    assert any(s.startswith("Strategy pruned:") for s in summaries)
    assert any(s.startswith("Strategy revived:") for s in summaries)
    revive_event = next(e for e in events if e.summary.startswith("Strategy revived:"))
    assert revive_event.payload["evidence_ref"] == "claim:abc"
    impossible = await architect.mark_impossible(candidates[1].id, "constraint cannot be met")
    assert impossible.status is StrategyStatus.IMPOSSIBLE
    with pytest.raises(ValueError, match="reason"):
        await architect.prune(candidates[2].id, " ")
    with pytest.raises(KeyError):
        await architect.prune("missing", "x")
