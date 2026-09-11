"""CS-040: the evidence engine is the single write path into the Reality Graph."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from callswarm.events import ActivityEventEmitter
from callswarm.evidence import (
    PHONE_ALLOWED_STATUSES,
    EvidenceEngine,
    normalize_predicate,
    normalize_subject,
    reconcile_group,
)
from callswarm.evidence.engine import source_key
from callswarm.models import (
    ActivityEventType,
    EvidenceClaim,
    EvidenceStatus,
    HardConstraint,
    Mission,
    MissionSpec,
    SourceType,
    StrategyCandidate,
    utcnow,
)
from callswarm.models.mission import ConstraintOperator
from callswarm.persistence import (
    ActivityEventRepository,
    Database,
    EvidenceClaimRepository,
    MissionRepository,
)


@pytest.fixture
async def engine(database: Database, emitter: ActivityEventEmitter) -> EvidenceEngine:
    return EvidenceEngine(database, emitter)


def claim(
    mission_id: str,
    subject: str,
    predicate: str,
    value: Any,
    source_type: SourceType,
    reference: str,
    **extra: Any,
) -> EvidenceClaim:
    return EvidenceClaim(
        mission_id=mission_id,
        subject=subject,
        predicate=predicate,
        value=value,
        source_type=source_type,
        source_reference=reference,
        **extra,
    )


async def all_claims(database: Database, mission_id: str) -> dict[str, EvidenceClaim]:
    async with database.session() as s:
        return {c.id: c for c in await EvidenceClaimRepository(s).list_by_mission(mission_id)}


# --- normalization --------------------------------------------------------------------


def test_normalization_keeps_units_and_collapses_case_and_whitespace() -> None:
    assert normalize_predicate("Quoted Price (INR)") == "quoted_price_inr"
    assert normalize_predicate("  capacity_per_hour ") == "capacity_per_hour"
    assert normalize_subject("  Option   A ") == "Option A"


async def test_ingest_normalizes_and_links_source(
    engine: EvidenceEngine, database: Database, mission: Mission
) -> None:
    out = await engine.ingest(
        [claim(mission.id, "  Thing  One ", "Price (USD)", 10, SourceType.WEB, "")],
        source="artifact:a1",
    )
    stored = out.claims[0]
    assert stored.subject == "Thing One"
    assert stored.predicate == "price_usd"
    assert stored.source_reference == "artifact:a1"
    assert stored.evidence_status is EvidenceStatus.WEB_SUPPORTED
    assert stored.simulated_lineage is False
    with pytest.raises(ValueError):
        await engine.ingest(
            [
                claim(mission.id, "x", "p", 1, SourceType.WEB, "r"),
                claim("other-mission", "x", "p", 1, SourceType.WEB, "r"),
            ],
            source="mixed",
        )


# --- reconcile rules ------------------------------------------------------------------


async def test_single_phone_is_phone_supported_and_single_fixture_is_web_supported(
    engine: EvidenceEngine, mission: Mission
) -> None:
    phone = await engine.ingest(
        [claim(mission.id, "A", "p", 1, SourceType.PHONE, "call_run:1")], source="call_run:1"
    )
    fixture = await engine.ingest(
        [claim(mission.id, "B", "p", 1, SourceType.FIXTURE, "artifact:f")], source="artifact:f"
    )
    assert phone.claims[0].evidence_status is EvidenceStatus.PHONE_SUPPORTED
    assert fixture.claims[0].evidence_status is EvidenceStatus.WEB_SUPPORTED
    assert fixture.claims[0].simulated_lineage is True


async def test_multi_source_requires_independent_sources(
    engine: EvidenceEngine, mission: Mission, database: Database
) -> None:
    # Same source twice (same type, same base reference) is not corroboration:
    # the newer claim supersedes the older one instead.
    first = await engine.ingest(
        [claim(mission.id, "A", "p", 5, SourceType.WEB, "artifact:x")], source="artifact:x"
    )
    second = await engine.ingest(
        [
            claim(
                mission.id,
                "A",
                "p",
                5,
                SourceType.WEB,
                "artifact:x;fragment",
                timestamp=utcnow() + timedelta(seconds=1),
            )
        ],
        source="artifact:x",
    )
    stored = await all_claims(database, mission.id)
    assert stored[first.claims[0].id].evidence_status is EvidenceStatus.STALE
    assert stored[second.claims[0].id].evidence_status is EvidenceStatus.WEB_SUPPORTED

    # A different reference of the same type is independent.
    third = await engine.ingest(
        [claim(mission.id, "A", "p", 5, SourceType.WEB, "artifact:y")], source="artifact:y"
    )
    stored = await all_claims(database, mission.id)
    assert stored[third.claims[0].id].evidence_status is EvidenceStatus.MULTI_SOURCE_SUPPORTED
    assert stored[second.claims[0].id].evidence_status is EvidenceStatus.MULTI_SOURCE_SUPPORTED
    assert stored[first.claims[0].id].evidence_status is EvidenceStatus.STALE

    # A different source type is independent too.
    phone = await engine.ingest(
        [claim(mission.id, "B", "p", "7", SourceType.PHONE, "call_run:1")], source="call_run:1"
    )
    web = await engine.ingest(
        [claim(mission.id, "B", "p", 7.0, SourceType.WEB, "artifact:z")], source="artifact:z"
    )
    stored = await all_claims(database, mission.id)
    assert stored[phone.claims[0].id].evidence_status is EvidenceStatus.MULTI_SOURCE_SUPPORTED
    assert stored[web.claims[0].id].evidence_status is EvidenceStatus.MULTI_SOURCE_SUPPORTED


async def test_conflicting_values_are_both_kept_cross_linked_and_never_averaged(
    engine: EvidenceEngine, mission: Mission, database: Database
) -> None:
    a = await engine.ingest(
        [claim(mission.id, "A", "cost", 100, SourceType.WEB, "artifact:1")], source="artifact:1"
    )
    b = await engine.ingest(
        [claim(mission.id, "A", "cost", 200, SourceType.PHONE, "call_run:2")], source="call_run:2"
    )
    stored = await all_claims(database, mission.id)
    assert len(stored) == 2
    ca, cb = stored[a.claims[0].id], stored[b.claims[0].id]
    assert ca.evidence_status is EvidenceStatus.CONFLICTED
    assert cb.evidence_status is EvidenceStatus.CONFLICTED
    assert ca.conflicts == [cb.id] and cb.conflicts == [ca.id]
    assert {ca.value, cb.value} == {100, 200}
    assert b.conflicted_ids == sorted([ca.id, cb.id])
    # Rejecting one side dissolves the conflict; the other returns to its own status.
    await engine.reject(ca.id, "page was out of date")
    stored = await all_claims(database, mission.id)
    assert stored[ca.id].evidence_status is EvidenceStatus.REJECTED
    assert stored[ca.id].status_reason == "page was out of date"
    assert stored[cb.id].evidence_status is EvidenceStatus.PHONE_SUPPORTED
    assert stored[cb.id].conflicts == []


async def test_reject_requires_reason(engine: EvidenceEngine, mission: Mission) -> None:
    out = await engine.ingest(
        [claim(mission.id, "A", "p", 1, SourceType.WEB, "artifact:1")], source="artifact:1"
    )
    with pytest.raises(ValueError):
        await engine.reject(out.claims[0].id, "   ")
    with pytest.raises(KeyError):
        await engine.reject("missing", "reason")


async def test_prose_claims_are_stored_but_never_reconciled(
    engine: EvidenceEngine, mission: Mission, database: Database
) -> None:
    for run in ("1", "2"):
        await engine.ingest(
            [
                claim(
                    mission.id,
                    "A",
                    "call_summary",
                    f"summary {run}",
                    SourceType.SIMULATED,
                    f"call_run:{run};low-confidence",
                    evidence_status=EvidenceStatus.UNKNOWN,
                )
            ],
            source=f"call_run:{run}",
        )
    stored = await all_claims(database, mission.id)
    assert len(stored) == 2
    assert all(c.evidence_status is EvidenceStatus.UNKNOWN for c in stored.values())


# --- phone claims never become verified truth ---------------------------------------------


@pytest.mark.parametrize(
    "scenario",
    ["single", "corroborated_by_web", "corroborated_by_phone", "conflicted", "superseded"],
)
async def test_phone_claims_never_exceed_phone_allowed_statuses(
    engine: EvidenceEngine, mission: Mission, database: Database, scenario: str
) -> None:
    base = claim(mission.id, "A", "p", 1, SourceType.PHONE, "call_run:1")
    await engine.ingest([base], source="call_run:1")
    if scenario == "corroborated_by_web":
        await engine.ingest(
            [claim(mission.id, "A", "p", 1, SourceType.WEB, "artifact:1")], source="artifact:1"
        )
    elif scenario == "corroborated_by_phone":
        await engine.ingest(
            [claim(mission.id, "A", "p", 1, SourceType.PHONE, "call_run:2")], source="call_run:2"
        )
    elif scenario == "conflicted":
        await engine.ingest(
            [claim(mission.id, "A", "p", 2, SourceType.WEB, "artifact:1")], source="artifact:1"
        )
    elif scenario == "superseded":
        await engine.ingest(
            [
                claim(
                    mission.id,
                    "A",
                    "p",
                    3,
                    SourceType.PHONE,
                    "call_run:1",
                    timestamp=utcnow() + timedelta(seconds=1),
                )
            ],
            source="call_run:1",
        )
    stored = await all_claims(database, mission.id)
    phone_claims = [c for c in stored.values() if c.source_type is SourceType.PHONE]
    assert phone_claims
    for c in phone_claims:
        assert c.evidence_status in PHONE_ALLOWED_STATUSES
        assert c.evidence_status is not EvidenceStatus.WEB_SUPPORTED


def test_reconcile_group_is_pure_and_ignores_history_and_derived() -> None:
    m = "m"
    stale = claim(m, "A", "p", 1, SourceType.WEB, "r1", evidence_status=EvidenceStatus.STALE)
    rejected = claim(m, "A", "p", 9, SourceType.WEB, "r2", evidence_status=EvidenceStatus.REJECTED)
    derived = claim(m, "A", "p", 42, SourceType.DERIVED, "agent:x")
    live = claim(m, "A", "p", 1, SourceType.PHONE, "call_run:1")
    updates = reconcile_group([stale, rejected, derived, live])
    assert [u.id for u in updates] == [live.id]
    assert updates[0].evidence_status is EvidenceStatus.PHONE_SUPPORTED
    assert stale.evidence_status is EvidenceStatus.STALE  # inputs untouched
    assert source_key(claim(m, "A", "p", 1, SourceType.PHONE, "call_run:1;recipient:0")) == (
        "PHONE",
        "call_run:1",
    )


# --- staleness -------------------------------------------------------------------------


async def test_mark_stale_superseded_by_keeps_the_older_claim(
    engine: EvidenceEngine, mission: Mission, database: Database
) -> None:
    first = await engine.ingest(
        [claim(mission.id, "A", "quote", 500, SourceType.PHONE, "call_run:12")],
        source="call_run:12",
    )
    later = await engine.ingest(
        [
            claim(
                mission.id,
                "A",
                "quote",
                450,
                SourceType.PHONE,
                "call_run:19",
                timestamp=utcnow() + timedelta(seconds=1),
            )
        ],
        source="call_run:19",
    )
    stored = await all_claims(database, mission.id)
    assert stored[first.claims[0].id].evidence_status is EvidenceStatus.CONFLICTED
    stale = await engine.mark_stale(mission.id, superseded_by=later.claims[0].id)
    assert [c.id for c in stale] == [first.claims[0].id]
    stored = await all_claims(database, mission.id)
    assert len(stored) == 2  # not deleted
    old = stored[first.claims[0].id]
    assert old.evidence_status is EvidenceStatus.STALE
    assert old.superseded_by == later.claims[0].id
    assert old.status_reason
    # The survivor is re-reconciled: a single phone statement again.
    assert stored[later.claims[0].id].evidence_status is EvidenceStatus.PHONE_SUPPORTED
    assert stored[later.claims[0].id].conflicts == []


async def test_mark_stale_older_than_and_argument_validation(
    engine: EvidenceEngine, mission: Mission, database: Database
) -> None:
    old_stamp = utcnow() - timedelta(days=2)
    await engine.ingest(
        [claim(mission.id, "A", "p", 1, SourceType.WEB, "artifact:1", timestamp=old_stamp)],
        source="artifact:1",
    )
    fresh = await engine.ingest(
        [claim(mission.id, "B", "p", 1, SourceType.WEB, "artifact:2")], source="artifact:2"
    )
    stale = await engine.mark_stale(
        mission.id, older_than=utcnow() - timedelta(days=1), reason="refresh"
    )
    assert len(stale) == 1 and stale[0].status_reason == "refresh"
    stored = await all_claims(database, mission.id)
    assert stored[fresh.claims[0].id].evidence_status is EvidenceStatus.WEB_SUPPORTED
    with pytest.raises(ValueError):
        await engine.mark_stale(mission.id)
    with pytest.raises(KeyError):
        await engine.mark_stale(mission.id, superseded_by="nope")


# --- derivation and provenance ----------------------------------------------------------


async def test_simulated_lineage_propagates_into_derived_claims(
    engine: EvidenceEngine, mission: Mission
) -> None:
    real = await engine.ingest(
        [claim(mission.id, "A", "p", 1, SourceType.WEB, "artifact:1")], source="artifact:1"
    )
    simulated = await engine.ingest(
        [claim(mission.id, "A", "q", 2, SourceType.SIMULATED, "call_run:1")], source="call_run:1"
    )
    clean = await engine.derive(
        mission.id, "A", "total", 1, derived_from=[real.claims[0].id], source_reference="agent:x"
    )
    assert clean.source_type is SourceType.DERIVED
    assert clean.simulated_lineage is False
    assert clean.derived_from == [real.claims[0].id]
    tainted = await engine.derive(
        mission.id,
        "A",
        "total",
        3,
        derived_from=[real.claims[0].id, simulated.claims[0].id],
        source_reference="agent:x",
    )
    assert tainted.simulated_lineage is True
    # Two levels deep: derived from a derived-and-tainted claim.
    deeper = await engine.derive(
        mission.id, "A", "ratio", 0.5, derived_from=[tainted.id], source_reference="agent:y"
    )
    assert deeper.simulated_lineage is True and deeper.is_simulated_or_fixture
    with pytest.raises(KeyError):
        await engine.derive(
            mission.id, "A", "x", 1, derived_from=["missing"], source_reference="agent:z"
        )


async def test_derived_claims_do_not_add_support(
    engine: EvidenceEngine, mission: Mission, database: Database
) -> None:
    base = await engine.ingest(
        [claim(mission.id, "A", "p", 1, SourceType.PHONE, "call_run:1")], source="call_run:1"
    )
    await engine.derive(
        mission.id, "A", "p", 1, derived_from=[base.claims[0].id], source_reference="agent:x"
    )
    group = await engine.reconcile(mission.id, "A", "p")
    by_type = {c.source_type: c.evidence_status for c in group}
    assert by_type[SourceType.PHONE] is EvidenceStatus.PHONE_SUPPORTED
    assert by_type[SourceType.DERIVED] is EvidenceStatus.UNKNOWN


# --- trace ---------------------------------------------------------------------------------


async def test_trace_orders_the_source_chain_oldest_first(
    engine: EvidenceEngine, mission: Mission
) -> None:
    t0 = utcnow()
    quoted = await engine.ingest(
        [claim(mission.id, "A", "quote", 500, SourceType.PHONE, "call_run:12", timestamp=t0)],
        source="call_run:12",
    )
    negotiated = await engine.ingest(
        [
            claim(
                mission.id,
                "A",
                "Quote",
                450,
                SourceType.PHONE,
                "call_run:19",
                timestamp=t0 + timedelta(minutes=5),
            )
        ],
        source="call_run:19",
    )
    await engine.mark_stale(mission.id, superseded_by=negotiated.claims[0].id)
    trace = await engine.trace(mission.id, "a", "QUOTE")
    assert [s.claim_id for s in trace.steps] == [quoted.claims[0].id, negotiated.claims[0].id]
    assert [s.source_reference for s in trace.steps] == ["call_run:12", "call_run:19"]
    assert trace.steps[0].evidence_status is EvidenceStatus.STALE
    assert trace.steps[0].superseded_by == negotiated.claims[0].id
    assert trace.current_status is EvidenceStatus.PHONE_SUPPORTED
    assert trace.contains_simulated_or_fixture is False
    assert trace.predicate == "quote" and trace.subject == "a"


# --- conflict notices ----------------------------------------------------------------------


async def test_conflicts_affecting_decisions_uses_decision_attributes(
    engine: EvidenceEngine, mission: Mission, database: Database
) -> None:
    spec = MissionSpec(
        mission_id=mission.id,
        hard_constraints=[HardConstraint(key="cost", operator=ConstraintOperator.LE, value=10)],
    )
    async with database.session() as s:
        mission = await MissionRepository(s).update(mission.model_copy(update={"spec": spec}))
    strategy = StrategyCandidate(mission_id=mission.id, title="t", required_information=["lead"])
    for predicate in ("cost", "lead", "colour"):
        await engine.ingest(
            [claim(mission.id, "A", predicate, 1, SourceType.WEB, "artifact:1")],
            source="artifact:1",
        )
        await engine.ingest(
            [claim(mission.id, "A", predicate, 2, SourceType.PHONE, "call_run:1")],
            source="call_run:1",
        )
    notices = await engine.conflicts_affecting_decisions(mission, [strategy])
    assert {n.predicate for n in notices} == {"cost", "lead"}
    cost = next(n for n in notices if n.predicate == "cost")
    assert cost.weight == 1.0 and len(cost.claim_ids) == 2 and set(cost.values) == {1, 2}
    await engine.notify_conflicts(mission, notices)
    async with database.session() as s:
        events = await ActivityEventRepository(s).list_by_mission(mission.id)
    conflict_events = [
        e
        for e in events
        if e.event_type is ActivityEventType.EVIDENCE_UPDATE and "Conflicting" in e.summary
    ]
    assert len(conflict_events) == 2


# --- API ------------------------------------------------------------------------------------


async def test_evidence_api_paginates_and_filters(
    client: AsyncClient, database: Database, emitter: ActivityEventEmitter
) -> None:
    async with database.session() as s:
        mission = await MissionRepository(s).add(Mission(user_goal="g"))
    engine = EvidenceEngine(database, emitter)
    for i in range(5):
        await engine.ingest(
            [claim(mission.id, "A", "p", i, SourceType.WEB, f"artifact:{i}")],
            source=f"artifact:{i}",
        )
    await engine.ingest(
        [claim(mission.id, "B", "q", 1, SourceType.PHONE, "call_run:1")], source="call_run:1"
    )
    page = (await client.get(f"/api/missions/{mission.id}/evidence?limit=2")).json()
    assert page["total"] == 6 and len(page["items"]) == 2 and page["offset"] == 0
    page2 = (await client.get(f"/api/missions/{mission.id}/evidence?limit=2&offset=4")).json()
    assert len(page2["items"]) == 2
    assert {i["id"] for i in page["items"]}.isdisjoint({i["id"] for i in page2["items"]})
    by_subject = (await client.get(f"/api/missions/{mission.id}/evidence?subject=b")).json()
    assert by_subject["total"] == 1 and by_subject["items"][0]["predicate"] == "q"
    conflicted = (
        await client.get(f"/api/missions/{mission.id}/evidence?status=CONFLICTED&predicate=P")
    ).json()
    assert conflicted["total"] == 5
    assert all(i["evidence_status"] == "CONFLICTED" for i in conflicted["items"])
    assert (await client.get(f"/api/missions/{mission.id}/evidence?status=nope")).status_code == 422
    assert (await client.get("/api/missions/missing/evidence")).status_code == 404

    claim_id = by_subject["items"][0]["id"]
    trace = (await client.get(f"/api/missions/{mission.id}/evidence/{claim_id}/trace")).json()
    assert trace["subject"] == "B" and [s["claim_id"] for s in trace["steps"]] == [claim_id]
    assert trace["current_status"] == "PHONE_SUPPORTED"
    assert (
        await client.get(f"/api/missions/{mission.id}/evidence/missing/trace")
    ).status_code == 404
    # A claim id from another mission is not reachable through this mission.
    async with database.session() as s:
        other = await MissionRepository(s).add(Mission(user_goal="g2"))
    assert (
        await client.get(f"/api/missions/{other.id}/evidence/{claim_id}/trace")
    ).status_code == 404
