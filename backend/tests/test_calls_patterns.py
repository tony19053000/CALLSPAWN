"""CS-035: every call pattern against the fake provider, through the one gate."""

from __future__ import annotations

import inspect
import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from callswarm import calls as calls_pkg
from callswarm.approvals import ApprovalService
from callswarm.calls import patterns as patterns_module
from callswarm.calls.fake import FakeCallProvider, FakeScript
from callswarm.calls.patterns import (
    COUNTER_OFFER_FIELD,
    ESCALATION_CONTACTS_ATTRIBUTE,
    FOLLOW_UP_JOB_TYPE,
    HANDLERS,
    PassCondition,
    PatternOptions,
    PatternRunner,
    derive_step_approval,
    escalation_contacts,
)
from callswarm.calls.service import CallService
from callswarm.config.settings import Settings
from callswarm.events import ActivityEventEmitter
from callswarm.llm.prompt import BEGIN_FENCE, END_FENCE
from callswarm.models import (
    ActivityEventType,
    ApprovalStatus,
    AuthorityPolicy,
    CallAuthorizationState,
    CallBudget,
    CallIntent,
    CallPattern,
    CallRecipient,
    CandidateEntity,
    ContactInfo,
    EvidenceClaim,
    EvidenceStatus,
    Mission,
    MissionStatus,
    ScheduledJobStatus,
    SourceType,
    hash_phone,
    utcnow,
)
from callswarm.persistence import (
    ActivityEventRepository,
    ApprovalRepository,
    CallIntentRepository,
    CallRunRepository,
    CandidateEntityRepository,
    Database,
    EvidenceClaimRepository,
    MissionRepository,
    ScheduledJobRepository,
)
from tests.conftest import TEST_PHONE, TEST_PHONE_2, daytime_region, make_intent, persist_intent
from tests.test_calls_provider import GOOD_SCHEMA, approved

ESCALATION_PHONE = "+15550000789"


@pytest.fixture
async def runner(
    call_service: CallService,
    approval_service: ApprovalService,
    database: Database,
    emitter: ActivityEventEmitter,
    settings: Settings,
) -> PatternRunner:
    return PatternRunner(call_service, approval_service, database, emitter, settings)


async def mission_with(database: Database, **policy: Any) -> Mission:
    async with database.session() as session:
        return await MissionRepository(session).add(
            Mission(
                user_goal="test goal",
                status=MissionStatus.CALL_AUTHORIZED,
                authority_policy=AuthorityPolicy(calls_allowed=True, max_call_count=5, **policy),
                call_budget=CallBudget(max_calls=5),
            )
        )


async def candidate_for(database: Database, mission: Mission, **attributes: Any) -> CandidateEntity:
    async with database.session() as session:
        return await CandidateEntityRepository(session).add(
            CandidateEntity(
                mission_id=mission.id,
                kind="option",
                display_name="Option A",
                attributes=attributes,
                contact=ContactInfo(phone_e164=TEST_PHONE, region=daytime_region()),
            )
        )


async def intent_for(
    database: Database,
    mission: Mission,
    pattern: CallPattern,
    *,
    entity_id: str | None = None,
    phones: tuple[str, ...] = (TEST_PHONE,),
) -> CallIntent:
    intent = make_intent(mission.id, entity_id=entity_id, result_schema=GOOD_SCHEMA)
    intent = intent.model_copy(
        update={
            "call_pattern": pattern,
            "recipients": [
                CallRecipient(phone_e164=p, region=daytime_region(), entity_id=entity_id)
                for p in phones
            ],
        }
    )
    return await persist_intent(database, intent)


async def prior_claim(
    database: Database,
    mission: Mission,
    entity_id: str,
    predicate: str,
    value: Any,
    status: EvidenceStatus = EvidenceStatus.PHONE_SUPPORTED,
) -> EvidenceClaim:
    async with database.session() as session:
        return await EvidenceClaimRepository(session).add(
            EvidenceClaim(
                mission_id=mission.id,
                subject="Option A",
                predicate=predicate,
                value=value,
                source_type=SourceType.SIMULATED,
                source_reference="call_run:earlier",
                evidence_status=status,
                entity_id=entity_id,
            )
        )


async def runs_for(database: Database, mission_id: str) -> list[Any]:
    async with database.session() as session:
        return await CallRunRepository(session).list_by_mission(mission_id)


async def stored_intent(database: Database, intent_id: str) -> CallIntent:
    async with database.session() as session:
        intent = await CallIntentRepository(session).get(intent_id)
    assert intent is not None
    return intent


# --- one-shot -----------------------------------------------------------------------


async def test_one_shot_dials_once_through_the_service(
    runner: PatternRunner, fake_provider: FakeCallProvider, database: Database
) -> None:
    mission = await mission_with(database)
    intent = await intent_for(database, mission, CallPattern.ONE_SHOT)
    await approved(database, intent)
    fake_provider.script(FakeScript(structured_result={"answer_status": "yes"}))
    outcome = await runner.run(intent)
    assert outcome.status == "completed" and outcome.pattern is CallPattern.ONE_SHOT
    assert outcome.dialed and len(outcome.results) == 1
    assert fake_provider.executed_intent_ids == [intent.id]
    progress = (await stored_intent(database, intent.id)).pattern_progress
    assert progress["pattern"] == "ONE_SHOT" and progress["status"] == "completed"
    assert progress["run_ids"] == [outcome.results[0].run.id]


async def test_unapproved_one_shot_is_refused_by_the_gate(
    runner: PatternRunner, fake_provider: FakeCallProvider, database: Database
) -> None:
    from callswarm.calls.provider import CallNotAuthorized

    mission = await mission_with(database)
    intent = await intent_for(database, mission, CallPattern.ONE_SHOT)
    with pytest.raises(CallNotAuthorized):
        await runner.run(intent)
    assert fake_provider.executed_intent_ids == []


# --- fan-out --------------------------------------------------------------------------


async def test_fan_out_is_one_create_with_a_recipient_schema(
    runner: PatternRunner, fake_provider: FakeCallProvider, database: Database
) -> None:
    mission = await mission_with(database)
    intent = await intent_for(
        database, mission, CallPattern.FAN_OUT, phones=(TEST_PHONE, TEST_PHONE_2)
    )
    await approved(database, intent)
    fake_provider.script(FakeScript(structured_result={"answer_status": "yes"}))
    outcome = await runner.run(intent)
    assert outcome.status == "completed"
    assert len(outcome.results) == 1, "one create, several recipients"
    assert [s.kind for s in outcome.steps] == ["call", "recipient", "recipient"]
    assert len(outcome.results[0].run.recipient_results) == 2
    updated = await stored_intent(database, intent.id)
    assert updated.recipient_result_schema is not None
    assert set(updated.recipient_result_schema["properties"]) == set(GOOD_SCHEMA["properties"])


async def test_fan_out_needs_two_recipients(runner: PatternRunner, database: Database) -> None:
    mission = await mission_with(database)
    intent = await intent_for(database, mission, CallPattern.FAN_OUT)
    await approved(database, intent)
    outcome = await runner.run(intent)
    assert outcome.status == "refused" and not outcome.dialed


# --- cascade -------------------------------------------------------------------------


async def test_cascade_stops_at_the_first_pass(
    runner: PatternRunner, fake_provider: FakeCallProvider, database: Database
) -> None:
    mission = await mission_with(database)
    intent = await intent_for(
        database, mission, CallPattern.CASCADE, phones=(TEST_PHONE, TEST_PHONE_2)
    )
    await approved(database, intent)
    fake_provider.script(
        FakeScript(structured_result={"answer_status": "no"}), intent_id=f"{intent.id}-step1"
    )
    fake_provider.script(
        FakeScript(structured_result={"answer_status": "yes"}), intent_id=f"{intent.id}-step2"
    )
    condition = PassCondition(field="answer_status", accepted_values=["yes"])
    outcome = await runner.run(intent, PatternOptions(pass_condition=condition))
    assert outcome.status == "completed"
    assert [s.status for s in outcome.steps] == ["failed_condition", "passed"]
    assert fake_provider.executed_intent_ids == [f"{intent.id}-step1", f"{intent.id}-step2"]
    async with database.session() as session:
        approvals = await ApprovalRepository(session).list_by_mission(mission.id)
        children = [await CallIntentRepository(session).get(f"{intent.id}-step{n}") for n in (1, 2)]
    derived = [a for a in approvals if a.reason and a.reason.startswith("derived from")]
    assert len(derived) == 2 and all(a.status is ApprovalStatus.APPROVED for a in derived)
    assert [c.recipients[0].phone_e164 for c in children if c] == [TEST_PHONE, TEST_PHONE_2]

    # A first-step pass never reaches the second recipient (fresh mission: one
    # execution round per mission state cycle).
    other = await mission_with(database)
    second = await intent_for(
        database, other, CallPattern.CASCADE, phones=(TEST_PHONE, TEST_PHONE_2)
    )
    await approved(database, second)
    fake_provider.script(
        FakeScript(structured_result={"answer_status": "yes"}), intent_id=f"{second.id}-step1"
    )
    outcome = await runner.run(second, PatternOptions(pass_condition=condition))
    assert [s.status for s in outcome.steps] == ["passed"]
    assert f"{second.id}-step2" not in fake_provider.executed_intent_ids


async def test_cascade_rerun_after_pending_resumes_with_the_existing_child(
    runner: PatternRunner,
    fake_provider: FakeCallProvider,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Child ids are deterministic; a rerun after a pending step must reuse the
    persisted child (get-or-create) instead of failing the insert."""
    from callswarm.calls.service import CallExecutionResult
    from callswarm.models import CallRun, CallStatus

    mission = await mission_with(database)
    intent = await intent_for(
        database, mission, CallPattern.CASCADE, phones=(TEST_PHONE, TEST_PHONE_2)
    )
    await approved(database, intent)
    condition = PassCondition(field="answer_status", accepted_values=["yes"])

    real_execute = runner.service.execute_call

    async def queued(intent_id: str, *, advance_mission: bool = True) -> CallExecutionResult:
        run = CallRun(
            mission_id=mission.id,
            call_intent_id=intent_id,
            status=CallStatus.QUEUED,
            is_simulated=True,
        )
        return CallExecutionResult(run=run, pending=True)

    monkeypatch.setattr(runner.service, "execute_call", queued)
    first = await runner.run(intent, PatternOptions(pass_condition=condition))
    assert first.status == "pending" and [s.status for s in first.steps] == ["pending"]
    async with database.session() as session:
        child = await CallIntentRepository(session).get(f"{intent.id}-step1")
        approvals_before = len(await ApprovalRepository(session).list_by_mission(mission.id))
    assert child is not None

    monkeypatch.setattr(runner.service, "execute_call", real_execute)
    fake_provider.script(
        FakeScript(structured_result={"answer_status": "no"}), intent_id=f"{intent.id}-step1"
    )
    fake_provider.script(
        FakeScript(structured_result={"answer_status": "yes"}), intent_id=f"{intent.id}-step2"
    )
    second = await runner.run(intent, PatternOptions(pass_condition=condition))
    assert second.status == "completed"
    assert [s.status for s in second.steps] == ["failed_condition", "passed"]
    assert fake_provider.executed_intent_ids == [f"{intent.id}-step1", f"{intent.id}-step2"]
    async with database.session() as session:
        children = [
            c
            for c in await CallIntentRepository(session).list_by_mission(mission.id)
            if c.id.startswith(f"{intent.id}-step")
        ]
        approvals_after = len(await ApprovalRepository(session).list_by_mission(mission.id))
    assert sorted(c.id for c in children) == [f"{intent.id}-step1", f"{intent.id}-step2"]
    assert approvals_after == approvals_before + 1, "step1's approval was reused, step2's added"


async def test_cascade_condition_is_code_and_unknown_never_passes() -> None:
    from callswarm.calls.service import CallExecutionResult
    from callswarm.models import CallRun, CallStatus

    def result(status: CallStatus, structured: dict[str, Any] | None) -> CallExecutionResult:
        return CallExecutionResult(
            run=CallRun(
                mission_id="m",
                call_intent_id="i",
                status=status,
                structured_result=structured,
                is_simulated=True,
            )
        )

    condition = PassCondition(field="answer_status", accepted_values=["yes"])
    assert condition.passes(result(CallStatus.COMPLETED, {"answer_status": "yes"}))
    assert not condition.passes(result(CallStatus.COMPLETED, {"answer_status": "unknown"}))
    assert not condition.passes(result(CallStatus.COMPLETED, None))
    assert not condition.passes(result(CallStatus.FAILED, {"answer_status": "yes"}))
    assert PassCondition().passes(result(CallStatus.COMPLETED, {"x": 1}))


async def test_cascade_requires_the_parent_approval_and_derivation_is_checked(
    runner: PatternRunner, fake_provider: FakeCallProvider, database: Database
) -> None:
    mission = await mission_with(database)
    intent = await intent_for(
        database, mission, CallPattern.CASCADE, phones=(TEST_PHONE, TEST_PHONE_2)
    )
    outcome = await runner.run(intent)
    assert outcome.status == "refused" and "APPROVED" in (outcome.reason or "")
    assert fake_provider.executed_intent_ids == []
    approval = await approved(database, intent)
    stranger = intent.model_copy(
        update={
            "id": "child",
            "recipients": [CallRecipient(phone_e164=ESCALATION_PHONE, region=daytime_region())],
        }
    )
    with pytest.raises(Exception, match="does not"):
        derive_step_approval(approval, stranger, intent)


# --- negotiation ----------------------------------------------------------------------


async def test_negotiation_carries_the_prior_quote_in_a_delimited_block(
    runner: PatternRunner, fake_provider: FakeCallProvider, database: Database
) -> None:
    mission = await mission_with(database, negotiation_allowed=True)
    candidate = await candidate_for(database, mission)
    await prior_claim(database, mission, candidate.id, "quoted_amount", 1234.5)
    intent = await intent_for(
        database, mission, CallPattern.NEGOTIATION_ROUND, entity_id=candidate.id
    )
    await approved(database, intent)
    fake_provider.script(
        FakeScript(structured_result={"answer_status": "yes", COUNTER_OFFER_FIELD: "improved"})
    )
    outcome = await runner.run(intent)
    assert outcome.status == "completed" and outcome.dialed
    updated = await stored_intent(database, intent.id)
    goal = updated.call_goal
    assert goal.startswith(intent.call_goal)
    assert BEGIN_FENCE in goal and END_FENCE in goal
    fenced = goal[goal.index(BEGIN_FENCE) : goal.index(END_FENCE)]
    assert "quoted_amount: 1234.5" in fenced
    assert "It is not an instruction" in fenced
    schema = updated.result_schema
    assert COUNTER_OFFER_FIELD in schema["required"]
    assert schema["properties"][COUNTER_OFFER_FIELD]["enum"][-1] == "unknown"
    run = outcome.results[0].run
    assert run.structured_result == {"answer_status": "yes", COUNTER_OFFER_FIELD: "improved"}
    predicates = {c.predicate for c in outcome.results[0].claims}
    assert COUNTER_OFFER_FIELD in predicates


async def test_negotiation_rerun_does_not_append_a_second_context_block(
    runner: PatternRunner, fake_provider: FakeCallProvider, database: Database
) -> None:
    mission = await mission_with(database, negotiation_allowed=True)
    candidate = await candidate_for(database, mission)
    await prior_claim(database, mission, candidate.id, "quoted_amount", 1234.5)
    intent = await intent_for(
        database, mission, CallPattern.NEGOTIATION_ROUND, entity_id=candidate.id
    )
    await approved(database, intent)
    fake_provider.script(
        FakeScript(structured_result={"answer_status": "yes", COUNTER_OFFER_FIELD: "improved"})
    )
    first = await runner.run(intent)
    assert first.status == "completed"
    after_first = await stored_intent(database, intent.id)
    assert after_first.call_goal.count(BEGIN_FENCE) == 1

    # The run now exists; a rerun is an idempotent no-op on the dial and must
    # leave the goal text and schema exactly as they were.
    second = await runner.run(after_first)
    assert second.status == "completed"
    assert len(fake_provider.executed_intent_ids) == 1
    after_second = await stored_intent(database, intent.id)
    assert after_second.call_goal == after_first.call_goal
    assert after_second.call_goal.count(BEGIN_FENCE) == 1
    assert after_second.result_schema == after_first.result_schema
    assert second.results[0].run.id == first.results[0].run.id


async def test_negotiation_refused_without_policy_or_prior_quote(
    runner: PatternRunner, fake_provider: FakeCallProvider, database: Database
) -> None:
    mission = await mission_with(database, negotiation_allowed=False)
    intent = await intent_for(database, mission, CallPattern.NEGOTIATION_ROUND)
    await approved(database, intent)
    outcome = await runner.run(intent)
    assert outcome.status == "refused" and "policy" in (outcome.reason or "")
    allowed = await mission_with(database, negotiation_allowed=True)
    candidate = await candidate_for(database, allowed)
    intent = await intent_for(
        database, allowed, CallPattern.NEGOTIATION_ROUND, entity_id=candidate.id
    )
    await approved(database, intent)
    outcome = await runner.run(intent)
    assert outcome.status == "refused" and "no prior quote" in (outcome.reason or "")
    assert fake_provider.executed_intent_ids == []


# --- clarification ----------------------------------------------------------------------


async def test_clarification_injects_conflicted_claims(
    runner: PatternRunner, fake_provider: FakeCallProvider, database: Database
) -> None:
    mission = await mission_with(database)
    candidate = await candidate_for(database, mission)
    await prior_claim(
        database, mission, candidate.id, "answer_status", "no", EvidenceStatus.CONFLICTED
    )
    intent = await intent_for(database, mission, CallPattern.CLARIFICATION, entity_id=candidate.id)
    await approved(database, intent)
    fake_provider.script(
        FakeScript(structured_result={"answer_status": "yes", "clarification_status": "clarified"})
    )
    outcome = await runner.run(intent)
    assert outcome.status == "completed"
    goal = (await stored_intent(database, intent.id)).call_goal
    assert BEGIN_FENCE in goal and "answer_status" in goal


async def test_clarification_rerun_does_not_append_a_second_context_block(
    runner: PatternRunner, fake_provider: FakeCallProvider, database: Database
) -> None:
    mission = await mission_with(database)
    candidate = await candidate_for(database, mission)
    await prior_claim(
        database, mission, candidate.id, "answer_status", "no", EvidenceStatus.CONFLICTED
    )
    intent = await intent_for(database, mission, CallPattern.CLARIFICATION, entity_id=candidate.id)
    await approved(database, intent)
    fake_provider.script(
        FakeScript(structured_result={"answer_status": "yes", "clarification_status": "clarified"})
    )
    first = await runner.run(intent)
    assert first.status == "completed"
    after_first = await stored_intent(database, intent.id)
    assert after_first.call_goal.count(BEGIN_FENCE) == 1

    second = await runner.run(after_first)
    assert second.status == "completed"
    assert len(fake_provider.executed_intent_ids) == 1
    after_second = await stored_intent(database, intent.id)
    assert after_second.call_goal == after_first.call_goal
    assert after_second.call_goal.count(BEGIN_FENCE) == 1
    assert after_second.result_schema == after_first.result_schema
    assert second.results[0].run.id == first.results[0].run.id


# --- verification --------------------------------------------------------------------


async def test_verification_mismatch_marks_both_claims_conflicted(
    runner: PatternRunner, fake_provider: FakeCallProvider, database: Database
) -> None:
    mission = await mission_with(database, confirmation_calls_allowed=True)
    candidate = await candidate_for(database, mission)
    earlier = await prior_claim(database, mission, candidate.id, "answer_status", "no")
    intent = await intent_for(database, mission, CallPattern.VERIFICATION, entity_id=candidate.id)
    await approved(database, intent)
    fake_provider.script(FakeScript(structured_result={"answer_status": "yes"}))
    outcome = await runner.run(intent)
    assert outcome.status == "completed"
    assert earlier.id in outcome.conflicted_claim_ids and len(outcome.conflicted_claim_ids) == 2
    async with database.session() as session:
        claims = {
            c.id: c for c in await EvidenceClaimRepository(session).list_by_mission(mission.id)
        }
    fresh_id = next(i for i in outcome.conflicted_claim_ids if i != earlier.id)
    assert claims[earlier.id].evidence_status is EvidenceStatus.CONFLICTED
    assert claims[fresh_id].evidence_status is EvidenceStatus.CONFLICTED
    assert claims[earlier.id].conflicts == [fresh_id]
    assert claims[fresh_id].conflicts == [earlier.id]


async def test_verification_match_corroborates_and_policy_is_required(
    runner: PatternRunner, fake_provider: FakeCallProvider, database: Database
) -> None:
    mission = await mission_with(database, confirmation_calls_allowed=True)
    candidate = await candidate_for(database, mission)
    earlier = await prior_claim(database, mission, candidate.id, "answer_status", "yes")
    intent = await intent_for(database, mission, CallPattern.VERIFICATION, entity_id=candidate.id)
    await approved(database, intent)
    fake_provider.script(FakeScript(structured_result={"answer_status": "yes"}))
    outcome = await runner.run(intent)
    assert outcome.conflicted_claim_ids == []
    assert earlier.id in outcome.corroborated_claim_ids
    async with database.session() as session:
        stored = await EvidenceClaimRepository(session).get(earlier.id)
    assert stored is not None and stored.evidence_status is EvidenceStatus.MULTI_SOURCE_SUPPORTED

    denied = await mission_with(database, confirmation_calls_allowed=False)
    intent = await intent_for(database, denied, CallPattern.VERIFICATION)
    await approved(database, intent)
    outcome = await runner.run(intent)
    assert outcome.status == "refused" and not outcome.dialed


# --- follow-up ------------------------------------------------------------------------


async def test_follow_up_persists_a_job_and_does_not_dial(
    runner: PatternRunner, fake_provider: FakeCallProvider, database: Database
) -> None:
    mission = await mission_with(database, scheduled_follow_up_allowed=True)
    intent = await intent_for(database, mission, CallPattern.FOLLOW_UP)
    await approved(database, intent)
    due = utcnow() + timedelta(hours=3)
    outcome = await runner.run(intent, PatternOptions(follow_up_due_at=due))
    assert outcome.status == "scheduled" and not outcome.dialed
    assert fake_provider.executed_intent_ids == []
    assert await runs_for(database, mission.id) == []
    async with database.session() as session:
        jobs = await ScheduledJobRepository(session).list_by_mission(mission.id)
        events = await ActivityEventRepository(session).list_by_mission(mission.id)
    assert len(jobs) == 1 and jobs[0].id == outcome.scheduled_job_id
    assert jobs[0].job_type == FOLLOW_UP_JOB_TYPE
    assert jobs[0].status is ScheduledJobStatus.PENDING
    assert abs((jobs[0].due_at - due).total_seconds()) < 1
    assert jobs[0].payload["call_intent_id"] == intent.id
    assert any(e.event_type is ActivityEventType.SCHEDULER_EVENT for e in events)


async def test_follow_up_refused_without_policy_or_due_time(
    runner: PatternRunner, database: Database
) -> None:
    mission = await mission_with(database, scheduled_follow_up_allowed=False)
    intent = await intent_for(database, mission, CallPattern.FOLLOW_UP)
    outcome = await runner.run(
        intent, PatternOptions(follow_up_due_at=utcnow() + timedelta(hours=1))
    )
    assert outcome.status == "refused" and "policy" in (outcome.reason or "")
    allowed = await mission_with(database, scheduled_follow_up_allowed=True)
    intent = await intent_for(database, allowed, CallPattern.FOLLOW_UP)
    outcome = await runner.run(intent)
    assert outcome.status == "refused" and "due time" in (outcome.reason or "")
    past = await runner.run(intent, PatternOptions(follow_up_due_at=utcnow() - timedelta(hours=1)))
    assert past.status == "refused"


# --- escalation ------------------------------------------------------------------------


async def test_escalation_refused_without_policy(
    runner: PatternRunner, fake_provider: FakeCallProvider, database: Database
) -> None:
    mission = await mission_with(database)
    candidate = await candidate_for(
        database,
        mission,
        **{ESCALATION_CONTACTS_ATTRIBUTE: [{"phone_e164": ESCALATION_PHONE, "region": "US"}]},
    )
    intent = await intent_for(database, mission, CallPattern.ESCALATION, entity_id=candidate.id)
    await approved(database, intent)
    outcome = await runner.run(intent)
    assert outcome.status == "refused" and "policy" in (outcome.reason or "")
    assert fake_provider.executed_intent_ids == []


async def test_escalation_requests_a_fresh_approval_and_stops(
    runner: PatternRunner,
    fake_provider: FakeCallProvider,
    database: Database,
    approval_service: ApprovalService,
) -> None:
    mission = await mission_with(database, escalation_allowed=True)
    candidate = await candidate_for(
        database,
        mission,
        **{
            ESCALATION_CONTACTS_ATTRIBUTE: [
                {"phone_e164": "0800 123 456", "region": "US"},  # not E.164: skipped
                {"phone_e164": ESCALATION_PHONE, "region": "US", "locale": "en-US"},
            ]
        },
    )
    assert [c.phone_e164 for c in escalation_contacts(candidate)] == [ESCALATION_PHONE]
    intent = await intent_for(database, mission, CallPattern.ESCALATION, entity_id=candidate.id)
    await approved(database, intent)
    outcome = await runner.run(intent)
    assert outcome.status == "stopped" and not outcome.dialed
    assert outcome.approval_id is not None
    assert fake_provider.executed_intent_ids == []
    approval = await approval_service.get(outcome.approval_id)
    assert approval.status is ApprovalStatus.PENDING
    child = await stored_intent(database, approval.subject_id)
    assert child.recipients[0].phone_e164 == ESCALATION_PHONE
    assert child.call_pattern is CallPattern.ONE_SHOT
    assert child.authorization_state is CallAuthorizationState.PENDING
    progress = (await stored_intent(database, intent.id)).pattern_progress
    assert progress["escalated_phone_hashes"] == [hash_phone(ESCALATION_PHONE)]
    assert progress["escalation_intent_ids"] == [child.id]
    assert ESCALATION_PHONE not in json.dumps(progress), "progress holds hashes, not numbers"
    # No second contact exists: another round is refused rather than repeated.
    again = await runner.run(intent)
    assert again.status == "refused"


# --- human gate --------------------------------------------------------------------------


async def test_human_gate_stops_with_a_pending_approval_then_proceeds_once_approved(
    runner: PatternRunner,
    fake_provider: FakeCallProvider,
    database: Database,
    approval_service: ApprovalService,
) -> None:
    mission = await mission_with(database)
    intent = await intent_for(database, mission, CallPattern.HUMAN_GATE)
    outcome = await runner.run(intent)
    assert outcome.status == "stopped" and outcome.approval_id is not None
    assert fake_provider.executed_intent_ids == []
    approval = await approval_service.get(outcome.approval_id)
    assert approval.status is ApprovalStatus.PENDING
    # Still pending: stop again without a new request.
    again = await runner.run(intent)
    assert again.status == "stopped" and again.approval_id == outcome.approval_id
    await approval_service.decide(approval.id, "APPROVED", "tester")
    fake_provider.script(FakeScript(structured_result={"answer_status": "yes"}))
    proceeded = await runner.run(intent)
    assert proceeded.status == "completed" and proceeded.dialed
    assert fake_provider.executed_intent_ids == [intent.id]


# --- structural guarantees ------------------------------------------------------------


def test_every_pattern_has_exactly_one_handler() -> None:
    assert set(HANDLERS) == set(CallPattern)


def test_no_handler_touches_the_dialer_directly() -> None:
    source = Path(inspect.getfile(patterns_module)).read_text()
    assert "provider." not in source
    assert "provider=" not in source
    assert source.count("execute_call(") >= 7, "every dialing handler goes through execute_call"


DOMAIN_PATTERN = re.compile(
    r"venue|caterer|\bgpu\b|\bcpu\b|motherboard|hotel|law firm|lawyer|photographer|wedding|"
    r"anniversary|prospect|\blead\b|café|cafe|restaurant|clinic|dealership",
    re.IGNORECASE,
)


@pytest.mark.parametrize(
    "path",
    [
        Path(inspect.getfile(patterns_module)),
        Path(inspect.getfile(calls_pkg)).parent / "calle.py",
        Path(inspect.getfile(calls_pkg)).parent.parent / "api" / "webhooks.py",
        Path(inspect.getfile(calls_pkg)).parent.parent / "models" / "webhooks.py",
    ],
    ids=["patterns", "calle", "api_webhooks", "models_webhooks"],
)
def test_new_modules_contain_no_domain_nouns(path: Path) -> None:
    offenders = [
        f"{path.name}:{n}: {line.strip()}"
        for n, line in enumerate(path.read_text().splitlines(), start=1)
        if DOMAIN_PATTERN.search(line)
    ]
    assert offenders == []
