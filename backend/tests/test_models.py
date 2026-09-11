"""CS-002: domain model round-trips, enum exactness and the no-domain-fields rule."""

from __future__ import annotations

import inspect
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import callswarm.models as models
from callswarm.models import (
    ActivityEvent,
    ActivityEventType,
    AgentRequest,
    AgentRun,
    AgentSpec,
    AgentState,
    Approval,
    ApprovalStatus,
    ApprovalSubjectType,
    AttemptStatus,
    AuthorityPolicy,
    CallBudget,
    CallIntent,
    CallRecipient,
    CallRun,
    CallStatus,
    CallValueFactors,
    CandidateEntity,
    ConstraintOperator,
    ContactInfo,
    EvidenceClaim,
    EvidenceStatus,
    HardConstraint,
    InformationGap,
    Mission,
    MissionSpec,
    MissionStatus,
    PlanComponent,
    PlanOption,
    Provenance,
    RecipientResult,
    RecipientStatus,
    ResearchArtifact,
    ScheduledJob,
    SoftPreference,
    SourceType,
    StrategyCandidate,
    SuppressionEntry,
    WebhookEventType,
    hash_phone,
)

TEST_PHONE = "+15550000123"  # reserved-style placeholder, not a real number


def _all_domain_models() -> list[type[BaseModel]]:
    found: list[type[BaseModel]] = []
    for name in models.__all__:
        obj = getattr(models, name)
        if inspect.isclass(obj) and issubclass(obj, BaseModel):
            found.append(obj)
    return found


def _sample_instances(mission_id: str = "m1") -> list[BaseModel]:
    now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    return [
        Mission(
            user_goal="goal",
            hard_constraints=[
                HardConstraint(key="budget", operator=ConstraintOperator.LE, value=1000)
            ],
            soft_preferences=[SoftPreference(key="quality", weight=2.0)],
            priority_weights={"cost": 0.5},
            spec=MissionSpec(mission_id=mission_id, summary="s"),
        ),
        StrategyCandidate(mission_id=mission_id, title="t", assumptions=["a"]),
        AgentSpec(
            mission_id=mission_id,
            name="n",
            role="r",
            objective="o",
            why_needed="w",
            expected_output_schema={"type": "object"},
        ),
        AgentRequest(
            mission_id=mission_id, requesting_agent_id="a1", proposed_role="r", justification="j"
        ),
        AgentRun(mission_id=mission_id, agent_id="a1", output_artifact={"k": [1, 2]}),
        ResearchArtifact(
            mission_id=mission_id,
            source="src",
            source_type=SourceType.FIXTURE,
            provenance=Provenance(
                source_type=SourceType.FIXTURE, provider_name="fixture", retrieved_at=now
            ),
        ),
        CandidateEntity(
            mission_id=mission_id,
            kind="anything",
            display_name="d",
            attributes={"open": {"nested": True}},
            contact=ContactInfo(phone_e164=TEST_PHONE),
        ),
        InformationGap(mission_id=mission_id, question="q?"),
        CallIntent(
            mission_id=mission_id,
            recipients=[CallRecipient(phone_e164=TEST_PHONE)],
            purpose="p",
            call_goal="g",
            priority_factors=CallValueFactors(
                mission_impact=0.5,
                uncertainty=0.5,
                time_sensitivity=0.5,
                expected_value=0.5,
                strategy_changing_potential=0.5,
                redundancy=0.5,
                call_cost=0.5,
            ),
        ),
        CallRun(
            mission_id=mission_id,
            call_intent_id="ci",
            is_simulated=True,
            recipient_results=[
                RecipientResult(
                    recipient_ref="r1",
                    phone_masked="+1 ••••• ••123",
                    status=RecipientStatus.SKIPPED,
                )
            ],
        ),
        EvidenceClaim(
            mission_id=mission_id,
            subject="s",
            predicate="p",
            value={"amount": 3},
            source_type=SourceType.SIMULATED,
            timestamp=now,
        ),
        PlanOption(
            mission_id=mission_id,
            name="n",
            components=[PlanComponent(name="c", source_types=[SourceType.SIMULATED])],
        ),
        Approval(
            mission_id=mission_id, subject_type=ApprovalSubjectType.CALL_INTENT, subject_id="x"
        ),
        SuppressionEntry(phone_hash=hash_phone(TEST_PHONE)),
        ActivityEvent(mission_id=mission_id, event_type=ActivityEventType.SYSTEM, summary="s"),
        ScheduledJob(mission_id=mission_id, job_type="follow_up", due_at=now),
    ]


def test_every_listed_model_has_a_sample() -> None:
    required = {
        "Mission",
        "MissionSpec",
        "AuthorityPolicy",
        "CallBudget",
        "StrategyCandidate",
        "AgentSpec",
        "AgentRequest",
        "AgentRun",
        "ResearchArtifact",
        "CandidateEntity",
        "InformationGap",
        "CallIntent",
        "CallRun",
        "RecipientResult",
        "EvidenceClaim",
        "PlanOption",
        "Approval",
        "SuppressionEntry",
        "ActivityEvent",
        "ScheduledJob",
    }
    assert required <= set(models.__all__)


@pytest.mark.parametrize("instance", _sample_instances(), ids=lambda i: type(i).__name__)
def test_models_round_trip_through_json(instance: BaseModel) -> None:
    payload = instance.model_dump_json()
    restored = type(instance).model_validate_json(payload)
    assert restored == instance
    assert restored.model_dump_json() == payload


def test_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Mission(user_goal="g", unknown_field=1)  # type: ignore[call-arg]


def test_call_status_enums_match_calle_exactly() -> None:
    assert [s.value for s in CallStatus] == [
        "queued",
        "in_progress",
        "completed",
        "failed",
        "canceled",
    ]
    assert [s.value for s in RecipientStatus] == [
        "pending",
        "in_progress",
        "completed",
        "failed",
        "skipped",
    ]
    assert [s.value for s in AttemptStatus] == [
        "queued",
        "dialing",
        "in_progress",
        "completed",
        "failed",
        "canceled",
    ]
    assert [s.value for s in WebhookEventType] == [
        "call.completed",
        "call.failed",
        "call.result_validation_failed",
    ]


def test_source_type_and_evidence_status_values() -> None:
    assert [s.value for s in SourceType] == [
        "WEB",
        "PHONE",
        "USER",
        "DERIVED",
        "FIXTURE",
        "SIMULATED",
    ]
    assert {s.value for s in EvidenceStatus} == {
        "UNKNOWN",
        "WEB_SUPPORTED",
        "PHONE_SUPPORTED",
        "MULTI_SOURCE_SUPPORTED",
        "CONFLICTED",
        "STALE",
        "REJECTED",
    }


def test_agent_lifecycle_and_approval_states() -> None:
    assert {s.value for s in AgentState} == {
        "CREATED",
        "WAITING",
        "READY",
        "WORKING",
        "BLOCKED",
        "WAITING_FOR_DEPENDENCY",
        "WAITING_FOR_CALL",
        "REVIEWING",
        "COMPLETE",
        "STOPPED",
        "FAILED",
    }
    assert {s.value for s in ApprovalStatus} == {"PENDING", "APPROVED", "REJECTED", "EXPIRED"}


def test_mission_states_exclude_final_execution() -> None:
    values = {s.value for s in MissionStatus}
    assert not any(v.startswith("FINAL_EXECUTION") for v in values)
    for expected in (
        "MISSION_CREATED",
        "GOAL_UNDERSTANDING",
        "CLARIFICATION_REQUIRED",
        "MISSION_SPEC_READY",
        "STRATEGY_DISCOVERY_RUNNING",
        "SWARM_READY",
        "CALL_AUTHORIZATION_PENDING",
        "CALL_AUTHORIZED",
        "EVIDENCE_UPDATE_RUNNING",
        "REVIEW_FAILED",
        "PLAN_OPTIONS_READY",
        "USER_DECISION_PENDING",
        "COMPLETE",
        "BLOCKED",
        "CANCELED",
    ):
        assert expected in values


def test_call_run_represents_fan_out_and_simulation() -> None:
    run = CallRun(
        mission_id="m",
        call_intent_id="ci",
        calle_call_id="call_123",
        status=CallStatus.COMPLETED,
        is_simulated=True,
        recipient_results=[
            RecipientResult(
                recipient_ref="a", phone_masked="+1 ••••• ••001", status=RecipientStatus.COMPLETED
            ),
            RecipientResult(
                recipient_ref="b", phone_masked="+1 ••••• ••002", status=RecipientStatus.FAILED
            ),
        ],
    )
    assert len(run.recipient_results) == 2
    assert run.is_simulated is True
    assert "provider_call_id" not in CallRun.model_fields
    with pytest.raises(ValidationError):
        CallRun(mission_id="m", call_intent_id="ci")  # type: ignore[call-arg]


def test_candidate_entity_is_generic() -> None:
    fields = set(CandidateEntity.model_fields)
    assert {"kind", "attributes", "display_name", "contact", "source_refs"} <= fields
    assert CandidateEntity.model_fields["kind"].annotation is str


def test_plan_option_propagates_simulated_provenance() -> None:
    option = PlanOption(
        mission_id="m",
        name="n",
        components=[
            PlanComponent(name="a", source_types=[SourceType.WEB]),
            PlanComponent(name="b", source_types=[SourceType.SIMULATED]),
        ],
    )
    assert option.contains_simulated_or_fixture is True
    assert SourceType.SIMULATED in option.source_types


def test_authority_policy_can_only_narrow() -> None:
    forbidden_fragments = ("without_approval", "skip_approval", "auto_")
    for name, field in AuthorityPolicy.model_fields.items():
        for fragment in forbidden_fragments:
            assert fragment not in name, f"AuthorityPolicy.{name} could waive approval"
        assert field.annotation in (bool, int), f"AuthorityPolicy.{name} is not a narrowing flag"
    assert "allow_booking_without_approval" not in AuthorityPolicy.model_fields
    defaults = AuthorityPolicy()
    assert defaults.calls_allowed is False
    assert defaults.max_call_count == 0


def test_call_budget_defaults_to_zero_and_reports_remaining() -> None:
    assert CallBudget().remaining == 0
    assert CallBudget(max_calls=3, calls_used=1).remaining == 2


DOMAIN_NOUNS = ("venue", "gpu", "caterer", "law", "firm", "hotel")


def test_no_domain_specific_field_names() -> None:
    for model in _all_domain_models():
        for field_name in model.model_fields:
            tokens = field_name.lower().split("_")
            for noun in DOMAIN_NOUNS:
                assert noun not in tokens, f"{model.__name__}.{field_name} is domain-specific"


def test_no_domain_nouns_in_models_source() -> None:
    package_dir = Path(models.__file__).parent
    pattern = re.compile(r"\b(" + "|".join(DOMAIN_NOUNS) + r")s?\b", re.IGNORECASE)
    for path in package_dir.glob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            assert not pattern.search(line), (
                f"{path.name}:{lineno} mentions a domain noun: {line!r}"
            )


def test_hash_phone_is_stable_and_non_reversible() -> None:
    digest = hash_phone(TEST_PHONE)
    assert digest == hash_phone(f" {TEST_PHONE} ")
    assert TEST_PHONE not in digest
    assert len(digest) == 64
