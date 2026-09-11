"""CS-030: deterministic priority, selection with reasons, the Call Strategy."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from callswarm import approvals as approvals_pkg
from callswarm import calls as calls_pkg
from callswarm.calls.scoring import (
    REDUNDANT_THRESHOLD,
    PriorityWeights,
    compute_priority,
    select_calls,
)
from callswarm.calls.strategy import (
    CallStrategy,
    call_resolvable_gaps,
    phone_number_gap,
    validate_pattern,
)
from callswarm.config.settings import Settings
from callswarm.events import ActivityEventEmitter
from callswarm.llm import FakeLLMProvider
from callswarm.models import (
    ActivityEventType,
    AuthorityPolicy,
    CallAuthorizationState,
    CallBudget,
    CallIntent,
    CallPattern,
    CallRecipient,
    CallValueFactors,
    CandidateEntity,
    ContactInfo,
    InformationGap,
    Mission,
    MissionStatus,
)
from callswarm.orchestrator.state_machine import MissionStateMachine
from callswarm.persistence import (
    ActivityEventRepository,
    CallIntentRepository,
    Database,
    InformationGapRepository,
    MissionRepository,
)
from callswarm.research.gaps import GapReport
from tests.conftest import TEST_PHONE, set_mission_status

FACTOR_NAMES = [
    "mission_impact",
    "uncertainty",
    "time_sensitivity",
    "expected_value",
    "strategy_change_potential",
    "evidence_importance",
]


def factors(**overrides: float) -> CallValueFactors:
    base = dict.fromkeys(FACTOR_NAMES, 0.5)
    base.update({"redundancy": 0.2, "call_cost": 0.2})
    base.update(overrides)
    return CallValueFactors(**base)


def intent(mission_id: str, f: CallValueFactors | None, *, purpose: str = "ask") -> CallIntent:
    return CallIntent(
        mission_id=mission_id,
        recipients=[CallRecipient(phone_e164=TEST_PHONE)],
        purpose=purpose,
        call_goal="goal",
        priority_factors=f,
    )


# --- compute_priority --------------------------------------------------------------


def test_priority_is_deterministic_and_reproducible() -> None:
    w = PriorityWeights()
    f = factors(mission_impact=0.9, uncertainty=0.7, redundancy=0.1, call_cost=0.3)
    assert compute_priority(f, w) == compute_priority(f, w)
    assert compute_priority(f, w) == compute_priority(CallValueFactors(**f.model_dump()), w)
    expected = (0.25 * 0.9 + 0.15 * 0.7 + 0.10 * 0.5 + 0.20 * 0.5 + 0.15 * 0.5 + 0.15 * 0.5) - (
        0.5 * 0.1 + 0.3 * 0.3
    )
    assert compute_priority(f, w) == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize("name", FACTOR_NAMES)
def test_priority_is_monotone_in_each_positive_factor(name: str) -> None:
    w = PriorityWeights()
    values = [compute_priority(factors(**{name: v}), w) for v in (0.0, 0.25, 0.5, 0.75, 1.0)]
    assert values == sorted(values) and values[0] < values[-1]


@pytest.mark.parametrize("name", ["redundancy", "call_cost"])
def test_priority_is_anti_monotone_in_penalties(name: str) -> None:
    w = PriorityWeights()
    values = [compute_priority(factors(**{name: v}), w) for v in (0.0, 0.25, 0.5, 0.75, 1.0)]
    assert values == sorted(values, reverse=True) and values[0] > values[-1]


def test_priority_is_clamped() -> None:
    w = PriorityWeights()
    assert (
        compute_priority(
            factors(**dict.fromkeys(FACTOR_NAMES, 0.0), redundancy=1.0, call_cost=1.0), w
        )
        == 0.0
    )
    assert (
        compute_priority(
            factors(**dict.fromkeys(FACTOR_NAMES, 1.0), redundancy=0.0, call_cost=0.0), w
        )
        == 1.0
    )


def test_weights_come_from_settings(settings: Settings) -> None:
    w = PriorityWeights.from_settings(settings)
    assert w == PriorityWeights()
    assert settings.call_min_priority == 0.35


# --- select_calls ----------------------------------------------------------------------


def test_budget_cap_is_never_exceeded() -> None:
    policy = AuthorityPolicy(calls_allowed=True, max_call_count=5)
    intents = [intent("m", factors(mission_impact=1.0)) for _ in range(6)]
    selection = select_calls(intents, CallBudget(max_calls=2, calls_used=0), policy)
    assert len(selection.selected) == 2
    assert len(selection.rejected) == 4 and all(
        "over call budget" in r.reason for r in selection.rejected
    )
    used = select_calls(intents, CallBudget(max_calls=5, calls_used=4), policy)
    assert len(used.selected) == 1
    policy_cap = select_calls(
        intents, CallBudget(max_calls=5), AuthorityPolicy(calls_allowed=True, max_call_count=1)
    )
    assert len(policy_cap.selected) == 1
    hard = select_calls(intents, CallBudget(max_calls=5), policy, hard_cap=3)
    assert len(hard.selected) == 3


def test_nine_possible_three_selected_with_a_reason_for_every_rejection() -> None:
    policy = AuthorityPolicy(calls_allowed=True, max_call_count=3)
    strong = [
        intent("m", factors(mission_impact=1.0, expected_value=1.0), purpose=f"strong {i}")
        for i in range(4)
    ]
    weak = intent("m", factors(**dict.fromkeys(FACTOR_NAMES, 0.2)), purpose="weak")
    redundant = intent("m", factors(redundancy=0.95), purpose="redundant")
    unscored = intent("m", None, purpose="unscored")
    prohibited = intent("m", factors(mission_impact=1.0), purpose="collect a password")
    blocked = intent("m", factors(mission_impact=1.0), purpose="blocked").model_copy(
        update={"authorization_state": CallAuthorizationState.BLOCKED, "rejection_reason": "x"}
    )
    intents = [*strong, weak, redundant, unscored, prohibited, blocked]
    assert len(intents) == 9
    selection = select_calls(
        intents,
        CallBudget(max_calls=3),
        policy,
        prohibited_purposes=["password collection", "collect a password"],
    )
    assert len(selection.selected) == 3
    assert len(selection.rejected) == 6
    assert selection.considered == 9
    reasons = {r.intent_id: r.reason for r in selection.rejected}
    assert all(reasons.values())
    assert "below minimum priority" in reasons[weak.id]
    assert "redundant" in reasons[redundant.id]
    assert "no priority factors" in reasons[unscored.id]
    assert reasons[prohibited.id].startswith("blocked: prohibited purpose")
    assert reasons[blocked.id].startswith("blocked:")
    over = [r for r in selection.rejected if "over call budget" in r.reason]
    assert len(over) == 1 and over[0].intent_id in {s.id for s in strong}
    assert all(s.priority_score is not None for s in selection.selected)
    scores = [s.priority_score or 0 for s in selection.selected]
    assert scores == sorted(scores, reverse=True)


def test_blocked_intent_with_priority_one_is_never_selected() -> None:
    perfect = factors(**dict.fromkeys(FACTOR_NAMES, 1.0), redundancy=0.0, call_cost=0.0)
    assert compute_priority(perfect, PriorityWeights()) == 1.0
    blocked = intent("m", perfect).model_copy(
        update={"authorization_state": CallAuthorizationState.BLOCKED}
    )
    selection = select_calls(
        [blocked], CallBudget(max_calls=5), AuthorityPolicy(calls_allowed=True, max_call_count=5)
    )
    assert selection.selected == [] and selection.rejected[0].reason.startswith("blocked:")
    no_calls = select_calls(
        [intent("m", perfect)],
        CallBudget(max_calls=5),
        AuthorityPolicy(calls_allowed=False, max_call_count=5),
    )
    assert no_calls.selected == [] and "does not allow calls" in no_calls.rejected[0].reason


def test_redundant_threshold_drops_already_known() -> None:
    policy = AuthorityPolicy(calls_allowed=True, max_call_count=5)
    edge = intent(
        "m", factors(redundancy=REDUNDANT_THRESHOLD, mission_impact=1.0, expected_value=1.0)
    )
    selection = select_calls([edge], CallBudget(max_calls=5), policy)
    assert selection.selected == [] and "already known" in selection.rejected[0].reason


# --- pattern validation and gap grouping ---------------------------------------------


def test_pattern_validation_is_code_side() -> None:
    policy = AuthorityPolicy(calls_allowed=True)
    assert validate_pattern("fan_out", 1, policy) == (
        CallPattern.ONE_SHOT,
        "fan_out needs at least 2 recipients",
    )
    assert validate_pattern("fan_out", 2, policy)[0] is CallPattern.FAN_OUT
    assert validate_pattern("cascade", 1, policy)[0] is CallPattern.ONE_SHOT
    assert validate_pattern("negotiation", 1, policy)[0] is CallPattern.ONE_SHOT
    assert (
        validate_pattern("negotiation", 1, AuthorityPolicy(negotiation_allowed=True))[0]
        is CallPattern.NEGOTIATION_ROUND
    )
    assert validate_pattern("follow_up", 1, policy)[0] is CallPattern.ONE_SHOT
    assert (
        validate_pattern("verification", 1, AuthorityPolicy(confirmation_calls_allowed=True))[0]
        is CallPattern.VERIFICATION
    )
    assert validate_pattern("clarification", 1, policy) == (CallPattern.CLARIFICATION, None)
    assert validate_pattern("nonsense", 1, policy)[0] is CallPattern.ONE_SHOT


def test_only_open_call_resolvable_gaps_with_an_entity_are_grouped() -> None:
    gaps = [
        InformationGap(
            mission_id="m",
            question="a?",
            entity_id="c1",
            possible_resolution_methods=["web", "call"],
        ),
        InformationGap(
            mission_id="m", question="b?", entity_id="c1", possible_resolution_methods=["web"]
        ),
        InformationGap(
            mission_id="m", question="c?", entity_id=None, possible_resolution_methods=["call"]
        ),
        InformationGap(
            mission_id="m", question="d?", entity_id="c2", possible_resolution_methods=["CALL"]
        ),
    ]
    grouped = call_resolvable_gaps(GapReport(gaps=gaps))
    assert {k: [g.question for g in v] for k, v in grouped.items()} == {"c1": ["a?"], "c2": ["d?"]}


# --- CallStrategy.plan ------------------------------------------------------------------


def candidate(
    mission_id: str, name: str, *, phone: str | None, raw: str | None = None
) -> CandidateEntity:
    attributes: dict[str, Any] = {}
    if raw is not None:
        attributes["raw_phone"] = raw
    return CandidateEntity(
        mission_id=mission_id,
        kind="option",
        display_name=name,
        attributes=attributes,
        contact=ContactInfo(phone_e164=phone, region="SG"),
    )


def gap_for(mission_id: str, c: CandidateEntity) -> InformationGap:
    return InformationGap(
        mission_id=mission_id,
        question=f"What is 'availability' for {c.display_name}?",
        affected_decision="hard constraint: availability",
        entity_id=c.id,
        possible_resolution_methods=["web", "call", "user"],
    )


def proposal(**factor_overrides: float) -> dict[str, Any]:
    return {
        "purpose": "confirm availability",
        "call_goal": (
            "This is an AI assistant calling on behalf of a user; please confirm availability."
        ),
        "expected_decision_impact": "decides shortlist",
        "call_pattern": "fan_out",
        "factors": factors(**factor_overrides).model_dump(),
    }


SCHEMA_PROPOSAL = {
    "fields": [
        {
            "name": "availability",
            "kind": "enum",
            "enum_values": ["yes", "no", "unknown"],
            "description": "Whether available.",
            "required": True,
        }
    ]
}


@pytest.fixture
async def gaps_mission(database: Database) -> Mission:
    async with database.session() as session:
        return await MissionRepository(session).add(
            Mission(
                user_goal="test goal",
                status=MissionStatus.INFORMATION_GAPS_READY,
                authority_policy=AuthorityPolicy(calls_allowed=True, max_call_count=2),
                call_budget=CallBudget(max_calls=2),
            )
        )


def strategy(
    database: Database,
    emitter: ActivityEventEmitter,
    llm: FakeLLMProvider,
    settings: Settings,
    machine: MissionStateMachine,
) -> CallStrategy:
    return CallStrategy(database, emitter, llm, settings, machine)


async def test_mission_needing_one_call_selects_one(
    database: Database,
    emitter: ActivityEventEmitter,
    settings: Settings,
    state_machine: MissionStateMachine,
    gaps_mission: Mission,
) -> None:
    c = candidate(gaps_mission.id, "Option A", phone=TEST_PHONE)
    report = GapReport(gaps=[gap_for(gaps_mission.id, c)])
    llm = FakeLLMProvider([proposal(mission_impact=1.0, expected_value=1.0), SCHEMA_PROPOSAL])
    result = await strategy(database, emitter, llm, settings, state_machine).plan(
        gaps_mission, report, [c]
    )
    assert len(result.selected) == 1 and result.rejected == []
    chosen = result.selected[0]
    assert chosen.priority_score is not None and chosen.priority_score > settings.call_min_priority
    assert chosen.call_pattern is CallPattern.ONE_SHOT  # fan_out downgraded: one recipient
    assert result.pattern_adjustments[0].proposed == "fan_out"
    assert chosen.result_schema["properties"]["availability"]["enum"] == ["yes", "no", "unknown"]
    assert chosen.recipients[0].phone_e164 == TEST_PHONE
    async with database.session() as session:
        stored = await CallIntentRepository(session).list_by_mission(gaps_mission.id)
        mission = await MissionRepository(session).get(gaps_mission.id)
        events = await ActivityEventRepository(session).list_by_mission(gaps_mission.id)
    assert [i.authorization_state for i in stored] == [CallAuthorizationState.PENDING]
    assert mission is not None and mission.status is MissionStatus.CALL_PLAN_READY
    summaries = [e.summary for e in events if e.event_type is ActivityEventType.CALL_EVENT]
    assert any(s.startswith("Selected 1 of 1 possible calls") for s in summaries)
    assert all(TEST_PHONE not in e.summary and TEST_PHONE not in str(e.payload) for e in events)
    # Untrusted inputs are fenced; the model never saw a raw score to echo.
    assert "BEGIN UNTRUSTED" in llm.calls[0].inputs["candidate"]


async def test_raw_phone_candidate_yields_a_gap_not_an_intent(
    database: Database,
    emitter: ActivityEventEmitter,
    settings: Settings,
    state_machine: MissionStateMachine,
    gaps_mission: Mission,
) -> None:
    c = candidate(gaps_mission.id, "Option B", phone=None, raw="0123 456 789 (ask for front desk)")
    report = GapReport(gaps=[gap_for(gaps_mission.id, c)])
    llm = FakeLLMProvider([])
    result = await strategy(database, emitter, llm, settings, state_machine).plan(
        gaps_mission, report, [c]
    )
    assert result.selected == [] and result.considered == 0
    assert len(result.number_gaps) == 1
    gap = result.number_gaps[0]
    assert "E.164" in gap.question and "not reformatted" in gap.question
    assert gap.entity_id == c.id and "call" not in gap.possible_resolution_methods
    assert llm.calls == []
    async with database.session() as session:
        assert await CallIntentRepository(session).list_by_mission(gaps_mission.id) == []
        persisted = await InformationGapRepository(session).list_by_mission(gaps_mission.id)
        mission = await MissionRepository(session).get(gaps_mission.id)
    assert [g.id for g in persisted] == [gap.id]
    assert mission is not None and mission.status is MissionStatus.REPLAN_DECISION_RUNNING
    assert phone_number_gap(candidate("m", "X", phone=None)).question.endswith(
        "No number was found."
    )


async def test_strategy_rejections_are_persisted_with_reasons(
    database: Database,
    emitter: ActivityEventEmitter,
    settings: Settings,
    state_machine: MissionStateMachine,
    gaps_mission: Mission,
) -> None:
    cands = [candidate(gaps_mission.id, f"Option {i}", phone=TEST_PHONE) for i in range(4)]
    report = GapReport(gaps=[gap_for(gaps_mission.id, c) for c in cands])
    llm = FakeLLMProvider(
        [
            proposal(mission_impact=1.0, expected_value=1.0),
            proposal(**dict.fromkeys(FACTOR_NAMES, 0.1)),  # below minimum
            proposal(redundancy=0.95),  # redundant
            proposal(mission_impact=0.9, expected_value=0.9),
            SCHEMA_PROPOSAL,
            SCHEMA_PROPOSAL,
        ]
    )
    result = await strategy(database, emitter, llm, settings, state_machine).plan(
        gaps_mission, report, cands
    )
    assert len(result.selected) == 2 and len(result.rejected) == 2
    async with database.session() as session:
        stored = await CallIntentRepository(session).list_by_mission(gaps_mission.id)
        events = await ActivityEventRepository(session).list_by_mission(gaps_mission.id)
    by_state = {
        s: [i for i in stored if i.authorization_state is s] for s in CallAuthorizationState
    }
    assert len(by_state[CallAuthorizationState.PENDING]) == 2
    assert len(by_state[CallAuthorizationState.NOT_REQUESTED]) == 2
    assert all(i.rejection_reason for i in by_state[CallAuthorizationState.NOT_REQUESTED])
    summary = next(e.summary for e in events if e.summary.startswith("Selected"))
    assert summary.startswith("Selected 2 of 4 possible calls; rejected:")
    assert "below minimum priority" in summary and "redundant" in summary


async def test_strategy_requires_a_legal_starting_state(
    database: Database,
    emitter: ActivityEventEmitter,
    settings: Settings,
    state_machine: MissionStateMachine,
    gaps_mission: Mission,
) -> None:
    from callswarm.orchestrator.state_machine import IllegalTransition

    await set_mission_status(database, gaps_mission, MissionStatus.MISSION_CREATED)
    with pytest.raises(IllegalTransition):
        await strategy(database, emitter, FakeLLMProvider([]), settings, state_machine).plan(
            gaps_mission, GapReport(), []
        )


# --- static: framework code names no domain -------------------------------------------

DOMAIN_PATTERN = re.compile(
    r"venue|caterer|\bgpu\b|\bcpu\b|motherboard|hotel|law firm|lawyer|photographer|wedding|"
    r"anniversary|prospect|\blead\b|café|cafe|restaurant|clinic|dealership",
    re.IGNORECASE,
)


@pytest.mark.parametrize("package", [calls_pkg, approvals_pkg], ids=lambda p: p.__name__)
def test_calls_and_approvals_source_contains_no_domain_nouns(package: Any) -> None:
    package_dir = Path(package.__file__).parent
    offenders: list[str] = []
    for path in sorted(package_dir.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if DOMAIN_PATTERN.search(line):
                offenders.append(f"{path.relative_to(package_dir.parent)}:{lineno}: {line.strip()}")
    assert offenders == []
