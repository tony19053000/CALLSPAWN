"""Evidence engine (CS-040): the single write path into the Reality Graph.

Every claim — from research, from a call, from an agent's derivation — enters
through :meth:`EvidenceEngine.ingest` or :meth:`EvidenceEngine.derive`. The
engine normalizes subject and predicate, links the source reference, and then
reconciles every claim that shares a subject and predicate in deterministic
code:

* one value stated by two or more **independent** sources (a different
  ``source_type`` or a different source reference) → ``MULTI_SOURCE_SUPPORTED``;
* different values → every claim ``CONFLICTED`` and cross-linked through
  ``conflicts``; all of them are kept and nothing is averaged;
* a single phone (or simulated) claim → ``PHONE_SUPPORTED``; a single web or
  fixture claim → ``WEB_SUPPORTED``.

A phone claim records what was *said*. There is no status that would let it
pass for verified truth, and the engine never assigns one. Staleness marks a
claim ``STALE`` and keeps it; rejection marks it ``REJECTED`` with a reason.
Provenance survives derivation: a ``DERIVED`` claim records its inputs and
inherits ``simulated_lineage`` when any input is fixture or simulated.

Nothing here names a domain: subjects, predicates and values are opaque.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import datetime

from pydantic import Field

from callswarm.events import ActivityEventEmitter
from callswarm.evidence.normalize import canonical_value, normalize_key
from callswarm.models import (
    ActivityEvent,
    ActivityEventType,
    DomainModel,
    EvidenceClaim,
    EvidenceStatus,
    JsonValue,
    Mission,
    SourceType,
    StrategyCandidate,
)
from callswarm.persistence import Database, EvidenceClaimRepository

logger = logging.getLogger(__name__)

# Predicates that carry prose (a call summary, a quoted line of evidence)
# rather than an attribute value. They are stored for the trace and the UI
# but never reconciled: two summaries are not two "values" of one fact.
NON_ATTRIBUTE_PREDICATES: frozenset[str] = frozenset({"call_summary", "call_evidence"})

# Source types that count as a source when reconciling. DERIVED claims are
# restatements of other claims and never add support on their own.
SOURCE_BEARING: frozenset[SourceType] = frozenset(
    {SourceType.WEB, SourceType.PHONE, SourceType.USER, SourceType.FIXTURE, SourceType.SIMULATED}
)
SIMULATED_TYPES: frozenset[SourceType] = frozenset({SourceType.FIXTURE, SourceType.SIMULATED})
PHONE_TYPES: frozenset[SourceType] = frozenset({SourceType.PHONE, SourceType.SIMULATED})
WEB_TYPES: frozenset[SourceType] = frozenset({SourceType.WEB, SourceType.FIXTURE})

# The only statuses a phone-sourced claim may ever hold. Asserted in code so a
# future edit cannot quietly launder a phone statement into a fact.
PHONE_ALLOWED_STATUSES: frozenset[EvidenceStatus] = frozenset(
    {
        EvidenceStatus.UNKNOWN,
        EvidenceStatus.PHONE_SUPPORTED,
        EvidenceStatus.MULTI_SOURCE_SUPPORTED,
        EvidenceStatus.CONFLICTED,
        EvidenceStatus.STALE,
        EvidenceStatus.REJECTED,
    }
)

# Statuses that take part in reconciliation. STALE and REJECTED are history.
ACTIVE_STATUSES: frozenset[EvidenceStatus] = frozenset(
    {
        EvidenceStatus.UNKNOWN,
        EvidenceStatus.WEB_SUPPORTED,
        EvidenceStatus.PHONE_SUPPORTED,
        EvidenceStatus.MULTI_SOURCE_SUPPORTED,
        EvidenceStatus.CONFLICTED,
    }
)


# --- normalization ---------------------------------------------------------------------


def normalize_subject(subject: str) -> str:
    """Collapse whitespace; keep the original case for display."""
    return " ".join(subject.split())


def subject_key(subject: str) -> str:
    return normalize_subject(subject).lower()


def normalize_predicate(predicate: str) -> str:
    """Lower-case snake_case. Units survive because they are alphanumeric:
    ``"Quoted price (INR)"`` → ``"quoted_price_inr"``."""
    return normalize_key(predicate) or predicate.strip().lower()


def source_key(claim: EvidenceClaim) -> tuple[str, str]:
    """What counts as one source: the source type plus the base reference (the
    part before any ``;`` qualifier such as ``recipient:`` or a confidence
    label). Two claims from the same call, the same page or the same fixture
    are one source."""
    return claim.source_type.value, claim.source_reference.split(";", 1)[0].strip()


def group_key(claim: EvidenceClaim) -> tuple[str, str]:
    return subject_key(claim.subject), normalize_predicate(claim.predicate)


def is_attribute_claim(claim: EvidenceClaim) -> bool:
    return (
        normalize_predicate(claim.predicate) not in NON_ATTRIBUTE_PREDICATES
        and claim.source_type in SOURCE_BEARING
    )


def single_source_status(source_type: SourceType) -> EvidenceStatus:
    if source_type in PHONE_TYPES:
        return EvidenceStatus.PHONE_SUPPORTED
    if source_type in WEB_TYPES:
        return EvidenceStatus.WEB_SUPPORTED
    # USER: a stated preference is neither web- nor phone-supported evidence.
    return EvidenceStatus.UNKNOWN


def reconcile_group(claims: Sequence[EvidenceClaim]) -> list[EvidenceClaim]:
    """Pure reconciliation of every claim sharing a subject and predicate.

    Returns copies of the claims whose status or cross-links changed. Claims
    that are STALE, REJECTED, prose, or DERIVED are left untouched and never
    counted as support.
    """
    active = [c for c in claims if c.evidence_status in ACTIVE_STATUSES and is_attribute_claim(c)]
    if not active:
        return []
    distinct = {canonical_value(c.value) for c in active}
    updates: list[EvidenceClaim] = []
    if len(distinct) > 1:
        ids = [c.id for c in active]
        for claim in active:
            conflicts = sorted(other for other in ids if other != claim.id)
            if (
                claim.evidence_status is not EvidenceStatus.CONFLICTED
                or claim.conflicts != conflicts
            ):
                updates.append(
                    claim.model_copy(
                        update={
                            "evidence_status": EvidenceStatus.CONFLICTED,
                            "conflicts": conflicts,
                        }
                    )
                )
        return updates
    independent = {source_key(c) for c in active}
    for claim in active:
        if len(independent) >= 2:
            status = EvidenceStatus.MULTI_SOURCE_SUPPORTED
        else:
            status = single_source_status(claim.source_type)
        if claim.source_type in PHONE_TYPES:
            assert status in PHONE_ALLOWED_STATUSES
        if claim.evidence_status is not status or claim.conflicts:
            updates.append(claim.model_copy(update={"evidence_status": status, "conflicts": []}))
    return updates


def lineage_is_simulated(inputs: Iterable[EvidenceClaim]) -> bool:
    return any(c.is_simulated_or_fixture for c in inputs)


# --- results ------------------------------------------------------------------------------


class IngestResult(DomainModel):
    claims: list[EvidenceClaim] = Field(default_factory=list, description="The stored claims")
    reconciled: list[EvidenceClaim] = Field(
        default_factory=list, description="Every claim whose status changed, new or prior"
    )
    conflicted_ids: list[str] = Field(default_factory=list)
    corroborated_ids: list[str] = Field(default_factory=list)
    stale_ids: list[str] = Field(default_factory=list)


class ConflictNotice(DomainModel):
    """A conflict on a predicate a decision depends on. Consumed by the
    Orchestrator as a ``CONFLICT`` replan trigger."""

    mission_id: str
    subject: str
    predicate: str
    claim_ids: list[str]
    decision: str
    weight: float
    values: list[JsonValue] = Field(default_factory=list)


class TraceStep(DomainModel):
    claim_id: str
    source_type: SourceType
    source_reference: str
    value: JsonValue = None
    evidence_status: EvidenceStatus
    timestamp: datetime
    derived_from: list[str] = Field(default_factory=list)
    simulated_lineage: bool = False
    status_reason: str | None = None
    superseded_by: str | None = None
    conflicts: list[str] = Field(default_factory=list)


class EvidenceTrace(DomainModel):
    """The ordered source chain for one subject/predicate, oldest first —
    e.g. a call quoted X, a later call negotiated Y — including stale and
    rejected steps so the history is visible."""

    mission_id: str
    subject: str
    predicate: str
    steps: list[TraceStep] = Field(default_factory=list)
    current_status: EvidenceStatus = EvidenceStatus.UNKNOWN
    contains_simulated_or_fixture: bool = False


class ClaimPage(DomainModel):
    items: list[EvidenceClaim]
    total: int
    offset: int
    limit: int


# --- engine -------------------------------------------------------------------------------


class EvidenceEngine:
    def __init__(self, database: Database, emitter: ActivityEventEmitter) -> None:
        self._database = database
        self._emitter = emitter

    async def _emit(self, mission_id: str, summary: str, **payload: object) -> None:
        await self._emitter.emit(
            ActivityEvent(
                mission_id=mission_id,
                event_type=ActivityEventType.EVIDENCE_UPDATE,
                summary=summary,
                payload=dict(payload),
            )
        )

    # --- ingest --------------------------------------------------------------------
    @staticmethod
    def _normalized(claim: EvidenceClaim, source: str) -> EvidenceClaim:
        update: dict[str, object] = {
            "subject": normalize_subject(claim.subject),
            "predicate": normalize_predicate(claim.predicate),
            "simulated_lineage": claim.simulated_lineage or claim.source_type in SIMULATED_TYPES,
        }
        if not claim.source_reference.strip():
            update["source_reference"] = source
        return claim.model_copy(update=update)

    async def ingest(self, claims: Sequence[EvidenceClaim], source: str) -> IngestResult:
        """Normalize, store, supersede and reconcile ``claims`` from one producer.

        ``source`` is the producer's reference (``artifact:<id>``,
        ``call_run:<id>``); a claim without its own ``source_reference`` gets
        it. A newer claim from the same source for the same subject and
        predicate makes the older one STALE. Then every touched subject and
        predicate is reconciled.
        """
        if not claims:
            return IngestResult()
        mission_ids = {c.mission_id for c in claims}
        if len(mission_ids) != 1:
            raise ValueError("one ingest batch belongs to exactly one mission")
        mission_id = mission_ids.pop()
        normalized = [self._normalized(c, source) for c in claims]
        stored: list[EvidenceClaim] = []
        stale_ids: list[str] = []
        touched: dict[tuple[str, str], None] = {}
        async with self._database.session() as session:
            repo = EvidenceClaimRepository(session)
            existing = await repo.list_by_mission(mission_id)
            by_group: dict[tuple[str, str], list[EvidenceClaim]] = {}
            for claim in existing:
                by_group.setdefault(group_key(claim), []).append(claim)
            for claim in normalized:
                key = group_key(claim)
                touched[key] = None
                if is_attribute_claim(claim):
                    for older in by_group.get(key, []):
                        if (
                            older.id != claim.id
                            and older.evidence_status in ACTIVE_STATUSES
                            and source_key(older) == source_key(claim)
                            and older.timestamp <= claim.timestamp
                        ):
                            superseded = older.model_copy(
                                update={
                                    "evidence_status": EvidenceStatus.STALE,
                                    "status_reason": f"superseded by claim {claim.id}",
                                    "superseded_by": claim.id,
                                    "conflicts": [],
                                }
                            )
                            await repo.update(superseded)
                            stale_ids.append(older.id)
                            by_group[key] = [
                                superseded if c.id == older.id else c for c in by_group[key]
                            ]
                added = await repo.add(claim)
                stored.append(added)
                by_group.setdefault(key, []).append(added)
            reconciled = await self._reconcile_groups(repo, by_group, list(touched))
        by_id = {c.id: c for c in reconciled}
        stored = [by_id.get(c.id, c) for c in stored]
        result = IngestResult(
            claims=stored,
            reconciled=reconciled,
            conflicted_ids=sorted(
                c.id for c in reconciled if c.evidence_status is EvidenceStatus.CONFLICTED
            ),
            corroborated_ids=sorted(
                c.id
                for c in reconciled
                if c.evidence_status is EvidenceStatus.MULTI_SOURCE_SUPPORTED
            ),
            stale_ids=stale_ids,
        )
        await self._emit(
            mission_id,
            f"Evidence updated from {source}: {len(stored)} claim(s) ingested, "
            f"{len(result.conflicted_ids)} conflicted, {len(result.corroborated_ids)} "
            f"multi-source, {len(stale_ids)} superseded.",
            source=source,
            ingested=len(stored),
            conflicted=len(result.conflicted_ids),
            corroborated=len(result.corroborated_ids),
            stale=len(stale_ids),
            source_types=sorted({c.source_type.value for c in stored}),
            simulated_or_fixture=any(c.is_simulated_or_fixture for c in stored),
        )
        return result

    @staticmethod
    async def _reconcile_groups(
        repo: EvidenceClaimRepository,
        by_group: dict[tuple[str, str], list[EvidenceClaim]],
        keys: Sequence[tuple[str, str]],
    ) -> list[EvidenceClaim]:
        changed: list[EvidenceClaim] = []
        for key in keys:
            for update in reconcile_group(by_group.get(key, [])):
                changed.append(await repo.update(update))
        return changed

    async def reconcile(self, mission_id: str, subject: str, predicate: str) -> list[EvidenceClaim]:
        """Re-run reconciliation for one subject/predicate; returns the group."""
        key = (subject_key(subject), normalize_predicate(predicate))
        async with self._database.session() as session:
            repo = EvidenceClaimRepository(session)
            group = [c for c in await repo.list_by_mission(mission_id) if group_key(c) == key]
            changed = {c.id: c for c in await self._reconcile_groups(repo, {key: group}, [key])}
        return [changed.get(c.id, c) for c in group]

    # --- derive ---------------------------------------------------------------------
    async def derive(
        self,
        mission_id: str,
        subject: str,
        predicate: str,
        value: JsonValue,
        *,
        derived_from: Sequence[str],
        source_reference: str,
        entity_id: str | None = None,
    ) -> EvidenceClaim:
        """Record a DERIVED claim. Its inputs must exist in the mission; the
        simulated/fixture marker of any input is inherited."""
        async with self._database.session() as session:
            repo = EvidenceClaimRepository(session)
            inputs: list[EvidenceClaim] = []
            for claim_id in derived_from:
                claim = await repo.get(claim_id)
                if claim is None or claim.mission_id != mission_id:
                    raise KeyError(f"derived_from claim {claim_id!r} is not in this mission")
                inputs.append(claim)
            derived = EvidenceClaim(
                mission_id=mission_id,
                subject=normalize_subject(subject),
                predicate=normalize_predicate(predicate),
                value=value,
                source_type=SourceType.DERIVED,
                source_reference=source_reference,
                evidence_status=EvidenceStatus.UNKNOWN,
                entity_id=entity_id,
                derived_from=[c.id for c in inputs],
                simulated_lineage=lineage_is_simulated(inputs),
            )
            stored = await repo.add(derived)
        return stored

    # --- staleness and rejection ---------------------------------------------------
    async def mark_stale(
        self,
        mission_id: str,
        *,
        older_than: datetime | None = None,
        superseded_by: str | None = None,
        reason: str = "",
    ) -> list[EvidenceClaim]:
        """Mark claims STALE — never delete them.

        ``older_than``: every active attribute claim of the mission with an
        earlier timestamp. ``superseded_by``: every older active claim with
        the same subject, predicate and source type as the named claim (a
        later call replaces an earlier quote from the phone; a later page
        replaces an earlier one). Touched groups are re-reconciled.
        """
        if older_than is None and superseded_by is None:
            raise ValueError("mark_stale needs older_than or superseded_by")
        stale: list[EvidenceClaim] = []
        async with self._database.session() as session:
            repo = EvidenceClaimRepository(session)
            claims = await repo.list_by_mission(mission_id)
            newer: EvidenceClaim | None = None
            if superseded_by is not None:
                newer = next((c for c in claims if c.id == superseded_by), None)
                if newer is None:
                    raise KeyError(f"claim {superseded_by!r} not found in mission {mission_id}")
            touched: dict[tuple[str, str], None] = {}
            for claim in claims:
                if claim.evidence_status not in ACTIVE_STATUSES or not is_attribute_claim(claim):
                    continue
                if newer is not None:
                    if (
                        claim.id == newer.id
                        or group_key(claim) != group_key(newer)
                        or claim.source_type is not newer.source_type
                        or claim.timestamp > newer.timestamp
                    ):
                        continue
                    why = reason or f"superseded by claim {newer.id}"
                elif older_than is not None and claim.timestamp < older_than:
                    why = reason or f"older than {older_than.isoformat()}"
                else:
                    continue
                updated = await repo.update(
                    claim.model_copy(
                        update={
                            "evidence_status": EvidenceStatus.STALE,
                            "status_reason": why,
                            "superseded_by": newer.id if newer is not None else None,
                            "conflicts": [],
                        }
                    )
                )
                stale.append(updated)
                touched[group_key(updated)] = None
            if touched:
                fresh = await repo.list_by_mission(mission_id)
                by_group: dict[tuple[str, str], list[EvidenceClaim]] = {}
                for claim in fresh:
                    by_group.setdefault(group_key(claim), []).append(claim)
                await self._reconcile_groups(repo, by_group, list(touched))
        if stale:
            await self._emit(
                mission_id,
                f"{len(stale)} claim(s) marked STALE (kept, not deleted).",
                stale_ids=[c.id for c in stale],
                superseded_by=superseded_by,
            )
        return stale

    async def reject(self, claim_id: str, reason: str) -> EvidenceClaim:
        if not reason.strip():
            raise ValueError("a rejection reason is required")
        async with self._database.session() as session:
            repo = EvidenceClaimRepository(session)
            claim = await repo.get(claim_id)
            if claim is None:
                raise KeyError(f"claim {claim_id!r} not found")
            rejected = await repo.update(
                claim.model_copy(
                    update={
                        "evidence_status": EvidenceStatus.REJECTED,
                        "status_reason": reason,
                        "conflicts": [],
                    }
                )
            )
            key = group_key(rejected)
            group = [c for c in await repo.list_by_mission(claim.mission_id) if group_key(c) == key]
            await self._reconcile_groups(repo, {key: group}, [key])
        await self._emit(
            claim.mission_id,
            f"Claim rejected: {rejected.predicate} for {rejected.subject} — {reason}",
            claim_id=claim_id,
            reason=reason,
        )
        return rejected

    # --- queries --------------------------------------------------------------------
    async def list_claims(
        self,
        mission_id: str,
        *,
        subject: str | None = None,
        predicate: str | None = None,
        status: EvidenceStatus | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> ClaimPage:
        async with self._database.session() as session:
            claims = await EvidenceClaimRepository(session).list_by_mission(
                mission_id, statuses=None if status is None else [status]
            )
        if subject is not None:
            wanted = subject_key(subject)
            claims = [c for c in claims if subject_key(c.subject) == wanted]
        if predicate is not None:
            wanted_predicate = normalize_predicate(predicate)
            claims = [c for c in claims if normalize_predicate(c.predicate) == wanted_predicate]
        return ClaimPage(
            items=claims[offset : offset + limit], total=len(claims), offset=offset, limit=limit
        )

    async def get_claim(self, mission_id: str, claim_id: str) -> EvidenceClaim | None:
        async with self._database.session() as session:
            claim = await EvidenceClaimRepository(session).get(claim_id)
        return claim if claim is not None and claim.mission_id == mission_id else None

    async def trace(self, mission_id: str, subject: str, predicate: str) -> EvidenceTrace:
        key = (subject_key(subject), normalize_predicate(predicate))
        async with self._database.session() as session:
            claims = await EvidenceClaimRepository(session).list_by_mission(mission_id)
        group = sorted(
            (c for c in claims if group_key(c) == key), key=lambda c: (c.timestamp, c.id)
        )
        steps = [
            TraceStep(
                claim_id=c.id,
                source_type=c.source_type,
                source_reference=c.source_reference,
                value=c.value,
                evidence_status=c.evidence_status,
                timestamp=c.timestamp,
                derived_from=list(c.derived_from),
                simulated_lineage=c.simulated_lineage,
                status_reason=c.status_reason,
                superseded_by=c.superseded_by,
                conflicts=list(c.conflicts),
            )
            for c in group
        ]
        active = [c for c in group if c.evidence_status in ACTIVE_STATUSES]
        current = EvidenceStatus.UNKNOWN
        if active:
            ranked = sorted(active, key=lambda c: (c.timestamp, c.id))
            current = ranked[-1].evidence_status
            if any(c.evidence_status is EvidenceStatus.CONFLICTED for c in active):
                current = EvidenceStatus.CONFLICTED
        return EvidenceTrace(
            mission_id=mission_id,
            subject=normalize_subject(subject),
            predicate=normalize_predicate(predicate),
            steps=steps,
            current_status=current,
            contains_simulated_or_fixture=any(c.is_simulated_or_fixture for c in group),
        )

    async def conflicts_affecting_decisions(
        self, mission: Mission, strategies: Sequence[StrategyCandidate] = ()
    ) -> list[ConflictNotice]:
        """Conflicts on predicates that are decision attributes of the mission.
        The Orchestrator turns each into a ``CONFLICT`` replan trigger."""
        # Research depends on evidence, not the reverse; this one lookup goes
        # the other way at call time, so the import is local.
        from callswarm.research.gaps import decision_attributes_from_mission

        attributes = decision_attributes_from_mission(mission.spec, list(strategies))
        by_key = {normalize_key(a.key): a for a in attributes}
        async with self._database.session() as session:
            claims = await EvidenceClaimRepository(session).list_by_mission(
                mission.id, statuses=[EvidenceStatus.CONFLICTED]
            )
        groups: dict[tuple[str, str], list[EvidenceClaim]] = {}
        for claim in claims:
            groups.setdefault(group_key(claim), []).append(claim)
        notices: list[ConflictNotice] = []
        for (_, predicate), members in groups.items():
            attribute = by_key.get(predicate)
            if attribute is None:
                continue
            notices.append(
                ConflictNotice(
                    mission_id=mission.id,
                    subject=members[0].subject,
                    predicate=predicate,
                    claim_ids=sorted(c.id for c in members),
                    decision=attribute.decision,
                    weight=attribute.weight,
                    values=[c.value for c in members],
                )
            )
        return notices

    async def notify_conflicts(self, mission: Mission, notices: Sequence[ConflictNotice]) -> None:
        for notice in notices:
            await self._emit(
                mission.id,
                f"Conflicting evidence on '{notice.predicate}' for {notice.subject} affects "
                f"{notice.decision}; both values are kept for a decision.",
                subject=notice.subject,
                predicate=notice.predicate,
                claim_ids=notice.claim_ids,
                decision=notice.decision,
                weight=notice.weight,
            )
