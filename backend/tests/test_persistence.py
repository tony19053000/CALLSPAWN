"""CS-003: repository CRUD, mission cascade and suppression survival."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect as sa_inspect

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
    CallIntent,
    CallRecipient,
    CallRun,
    CallStatus,
    CandidateEntity,
    ContactInfo,
    EvidenceClaim,
    InformationGap,
    Mission,
    MissionStatus,
    MissionTransition,
    PlanOption,
    Provenance,
    RecipientResult,
    RecipientStatus,
    ResearchArtifact,
    ScheduledJob,
    ScheduledJobStatus,
    SourceType,
    StrategyCandidate,
    SuppressionEntry,
    hash_phone,
)
from callswarm.persistence import (
    MISSION_SCOPED_TABLES,
    ActivityEventRepository,
    AgentRequestRepository,
    AgentRunRepository,
    AgentSpecRepository,
    ApprovalRepository,
    Base,
    CallIntentRepository,
    CallRunRepository,
    CandidateEntityRepository,
    Database,
    EvidenceClaimRepository,
    InformationGapRepository,
    MissionRepository,
    MissionTransitionRepository,
    PlanOptionRepository,
    ResearchArtifactRepository,
    ScheduledJobRepository,
    StrategyCandidateRepository,
    SuppressionEntryRepository,
)
from callswarm.persistence.orm import SuppressionEntryRow
from callswarm.sanitize import ReasoningLeakError

TEST_PHONE = "+15550000123"
NOW = datetime(2026, 9, 11, 10, 0, tzinfo=UTC)


async def _populate(database: Database, mission_id: str) -> dict[str, str]:
    """Insert one row of every mission-scoped aggregate. Returns ids by table."""
    ids: dict[str, str] = {}
    async with database.session() as s:
        ids["mission_transitions"] = (
            await MissionTransitionRepository(s).add(
                MissionTransition(
                    mission_id=mission_id,
                    from_status=MissionStatus.MISSION_CREATED,
                    to_status=MissionStatus.GOAL_UNDERSTANDING,
                    trigger="test",
                )
            )
        ).id
        ids["strategy_candidates"] = (
            await StrategyCandidateRepository(s).add(
                StrategyCandidate(mission_id=mission_id, title="t")
            )
        ).id
        spec = await AgentSpecRepository(s).add(
            AgentSpec(mission_id=mission_id, name="n", role="r", objective="o", why_needed="w")
        )
        ids["agent_specs"] = spec.id
        ids["agent_requests"] = (
            await AgentRequestRepository(s).add(
                AgentRequest(
                    mission_id=mission_id,
                    requesting_agent_id=spec.id,
                    proposed_role="p",
                    justification="j",
                )
            )
        ).id
        ids["agent_runs"] = (
            await AgentRunRepository(s).add(
                AgentRun(mission_id=mission_id, agent_id=spec.id, output_artifact={"ok": True})
            )
        ).id
        ids["research_artifacts"] = (
            await ResearchArtifactRepository(s).add(
                ResearchArtifact(
                    mission_id=mission_id,
                    source="fixture://x",
                    source_type=SourceType.FIXTURE,
                    provenance=Provenance(
                        source_type=SourceType.FIXTURE, provider_name="fixture", retrieved_at=NOW
                    ),
                )
            )
        ).id
        ids["candidate_entities"] = (
            await CandidateEntityRepository(s).add(
                CandidateEntity(
                    mission_id=mission_id,
                    kind="k",
                    display_name="d",
                    contact=ContactInfo(phone_e164=TEST_PHONE),
                )
            )
        ).id
        ids["information_gaps"] = (
            await InformationGapRepository(s).add(
                InformationGap(mission_id=mission_id, question="q")
            )
        ).id
        intent = await CallIntentRepository(s).add(
            CallIntent(
                mission_id=mission_id,
                recipients=[CallRecipient(phone_e164=TEST_PHONE)],
                purpose="p",
                call_goal="g",
            )
        )
        ids["call_intents"] = intent.id
        run = await CallRunRepository(s).add(
            CallRun(
                mission_id=mission_id,
                call_intent_id=intent.id,
                calle_call_id="call_abc",
                is_simulated=True,
                recipient_results=[
                    RecipientResult(
                        recipient_ref="a",
                        phone_masked="+1 ••••• ••123",
                        status=RecipientStatus.COMPLETED,
                    )
                ],
            )
        )
        ids["call_runs"] = run.id
        ids["recipient_results"] = run.recipient_results[0].id
        ids["evidence_claims"] = (
            await EvidenceClaimRepository(s).add(
                EvidenceClaim(
                    mission_id=mission_id,
                    subject="s",
                    predicate="p",
                    value=1,
                    source_type=SourceType.SIMULATED,
                    timestamp=NOW,
                )
            )
        ).id
        ids["plan_options"] = (
            await PlanOptionRepository(s).add(PlanOption(mission_id=mission_id, name="n"))
        ).id
        ids["approvals"] = (
            await ApprovalRepository(s).add(
                Approval(
                    mission_id=mission_id,
                    subject_type=ApprovalSubjectType.CALL_INTENT,
                    subject_id=intent.id,
                    requested_at=NOW,
                )
            )
        ).id
        ids["activity_events"] = (
            await ActivityEventRepository(s).add(
                ActivityEvent(
                    mission_id=mission_id, event_type=ActivityEventType.SYSTEM, summary="hi"
                )
            )
        ).id
        ids["scheduled_jobs"] = (
            await ScheduledJobRepository(s).add(
                ScheduledJob(mission_id=mission_id, job_type="follow_up", due_at=NOW)
            )
        ).id
    return ids


async def _count(database: Database, table: type[Base]) -> int:
    from sqlalchemy import func, select

    async with database.session() as s:
        return int((await s.execute(select(func.count()).select_from(table))).scalar_one())


async def test_schema_covers_every_persisted_entity(database: Database) -> None:
    async with database.engine.connect() as conn:
        names = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
    assert names == {
        "missions",
        "strategy_candidates",
        "agent_specs",
        "agent_requests",
        "agent_runs",
        "research_artifacts",
        "candidate_entities",
        "information_gaps",
        "call_intents",
        "call_runs",
        "recipient_results",
        "evidence_claims",
        "plan_options",
        "approvals",
        "activity_events",
        "suppression_entries",
        "scheduled_jobs",
        "mission_transitions",
    }


async def test_mission_crud_round_trip(database: Database) -> None:
    mission = Mission(user_goal="find something", priority_weights={"cost": 0.7})
    async with database.session() as s:
        stored = await MissionRepository(s).add(mission)
    assert stored == mission
    async with database.session() as s:
        loaded = await MissionRepository(s).get(mission.id)
    assert loaded == mission
    assert loaded is not None and loaded.created_at.tzinfo is not None
    updated = mission.model_copy(update={"status": MissionStatus.GOAL_UNDERSTANDING})
    async with database.session() as s:
        await MissionRepository(s).update(updated)
    async with database.session() as s:
        loaded = await MissionRepository(s).get(mission.id)
        listed = await MissionRepository(s).list_all()
    assert loaded is not None and loaded.status is MissionStatus.GOAL_UNDERSTANDING
    assert [m.id for m in listed] == [mission.id]
    async with database.session() as s:
        assert await MissionRepository(s).delete(mission.id) is True
        assert await MissionRepository(s).get(mission.id) is None


async def test_every_entity_persists_and_reloads(database: Database, mission: Mission) -> None:
    ids = await _populate(database, mission.id)
    assert set(ids) == {t.__tablename__ for t in MISSION_SCOPED_TABLES}
    async with database.session() as s:
        run = await CallRunRepository(s).get(ids["call_runs"])
        assert run is not None
        assert run.calle_call_id == "call_abc"
        assert run.is_simulated is True
        assert [r.recipient_ref for r in run.recipient_results] == ["a"]
        by_calle = await CallRunRepository(s).get_by_calle_call_id("call_abc")
        assert by_calle is not None and by_calle.id == run.id
        entity = await CandidateEntityRepository(s).get(ids["candidate_entities"])
        assert entity is not None and entity.contact.phone_e164 == TEST_PHONE
        claim = await EvidenceClaimRepository(s).get(ids["evidence_claims"])
        assert claim is not None and claim.source_type is SourceType.SIMULATED
        assert claim.timestamp == NOW
        job = await ScheduledJobRepository(s).get(ids["scheduled_jobs"])
        assert job is not None and job.status is ScheduledJobStatus.PENDING
        due = await ScheduledJobRepository(s).list_due(NOW + timedelta(minutes=1))
        assert [j.id for j in due] == [job.id]
        event = await ActivityEventRepository(s).get(ids["activity_events"])
        assert event is not None and event.sequence == 1
        approvals = await ApprovalRepository(s).list_by_subject(ids["call_intents"])
        assert [a.status for a in approvals] == [ApprovalStatus.PENDING]
        runs = await AgentRunRepository(s).list_by_agent(ids["agent_specs"])
        assert len(runs) == 1 and runs[0].status is AgentState.CREATED


async def test_mission_survives_reopen(settings: object, tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'restart.db'}"
    db = Database(url)
    await db.create_schema()
    mission = Mission(user_goal="persist me")
    async with db.session() as s:
        await MissionRepository(s).add(mission)
    await db.dispose()

    reopened = Database(url)
    await reopened.create_schema()  # idempotent
    async with reopened.session() as s:
        loaded = await MissionRepository(s).get(mission.id)
    await reopened.dispose()
    assert loaded == mission


async def test_call_run_update_replaces_recipient_results(
    database: Database, mission: Mission
) -> None:
    run = CallRun(mission_id=mission.id, call_intent_id="ci", is_simulated=True)
    async with database.session() as s:
        await CallRunRepository(s).add(run)
    updated = run.model_copy(
        update={
            "status": CallStatus.COMPLETED,
            "recipient_results": [
                RecipientResult(
                    recipient_ref="x",
                    phone_masked="+1 ••••• ••001",
                    status=RecipientStatus.COMPLETED,
                ),
                RecipientResult(
                    recipient_ref="y", phone_masked="+1 ••••• ••002", status=RecipientStatus.SKIPPED
                ),
            ],
        }
    )
    async with database.session() as s:
        await CallRunRepository(s).update(updated)
    async with database.session() as s:
        loaded = await CallRunRepository(s).get(run.id)
    assert loaded is not None
    assert loaded.status is CallStatus.COMPLETED
    assert sorted(r.recipient_ref for r in loaded.recipient_results) == ["x", "y"]


async def test_cascade_delete_removes_children_but_not_suppression(
    database: Database, mission: Mission
) -> None:
    ids = await _populate(database, mission.id)
    other = Mission(user_goal="unrelated")
    async with database.session() as s:
        await MissionRepository(s).add(other)
    other_ids = await _populate(database, other.id)
    suppression = SuppressionEntry(
        phone_hash=hash_phone(TEST_PHONE), reason="opt-out", source="call"
    )
    async with database.session() as s:
        await SuppressionEntryRepository(s).add(suppression)

    for table in MISSION_SCOPED_TABLES:
        assert await _count(database, table) == 2, table.__tablename__

    async with database.session() as s:
        assert await MissionRepository(s).delete_cascade(mission.id) is True

    for table in MISSION_SCOPED_TABLES:
        assert await _count(database, table) == 1, table.__tablename__
    async with database.session() as s:
        assert await MissionRepository(s).get(mission.id) is None
        assert await MissionRepository(s).get(other.id) is not None
        assert await CallRunRepository(s).get(ids["call_runs"]) is None
        assert await CallRunRepository(s).get(other_ids["call_runs"]) is not None
        assert await ScheduledJobRepository(s).get(ids["scheduled_jobs"]) is None
        survivors = await SuppressionEntryRepository(s).list_all()
        assert [e.id for e in survivors] == [suppression.id]
        assert await SuppressionEntryRepository(s).is_suppressed(hash_phone(TEST_PHONE)) is True
    assert await _count(database, SuppressionEntryRow) == 1


def test_suppression_is_structurally_mission_independent() -> None:
    assert SuppressionEntryRow not in MISSION_SCOPED_TABLES
    assert "mission_id" not in {c.name for c in SuppressionEntryRow.__table__.columns}
    scoped = {t.__tablename__ for t in MISSION_SCOPED_TABLES}
    assert "suppression_entries" not in scoped
    assert "scheduled_jobs" in scoped
    for table in Base.metadata.sorted_tables:
        if table.name in ("missions", "suppression_entries"):
            continue
        assert table.name in scoped, f"{table.name} has no cascade coverage"


async def test_delete_cascade_unknown_mission_is_false(database: Database) -> None:
    async with database.session() as s:
        assert await MissionRepository(s).delete_cascade("nope") is False


async def test_agent_run_artifact_is_sanitized_on_persistence(
    database: Database, mission: Mission
) -> None:
    run = AgentRun(
        mission_id=mission.id,
        agent_id="a",
        activity_summary=f"Called {TEST_PHONE} for a quote",
        output_artifact={"contact": TEST_PHONE, "nested": [{"phone": TEST_PHONE}]},
    )
    async with database.session() as s:
        stored = await AgentRunRepository(s).add(run)
    assert TEST_PHONE not in stored.model_dump_json()
    assert stored.activity_summary == "Called +1 ••••• ••123 for a quote"
    assert stored.output_artifact == {
        "contact": "+1 ••••• ••123",
        "nested": [{"phone": "+1 ••••• ••123"}],
    }


async def test_agent_run_with_reasoning_marker_is_rejected(
    database: Database, mission: Mission
) -> None:
    leaky = AgentRun(
        mission_id=mission.id,
        agent_id="a",
        output_artifact={"notes": "<thinking>secret plan</thinking> result: 5"},
    )
    with pytest.raises(ReasoningLeakError):
        async with database.session() as s:
            await AgentRunRepository(s).add(leaky)
    async with database.session() as s:
        assert await AgentRunRepository(s).get(leaky.id) is None
    leaky_summary = AgentRun(
        mission_id=mission.id, agent_id="a", activity_summary="Let me think..."
    )
    with pytest.raises(ReasoningLeakError):
        async with database.session() as s:
            await AgentRunRepository(s).add(leaky_summary)
