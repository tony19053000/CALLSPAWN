"""Provider selection and the mission-level research pass.

Selection rule (``RESEARCH_PROVIDER``):

* ``fixture`` → :class:`FixtureResearchProvider`;
* ``gemini_grounded`` (alias ``live``) → :class:`GeminiGroundedResearchProvider`
  when Gemini credentials exist. Without them the instance degrades to
  fixtures **and** ``fallback_reason`` is set; the service records that
  reason as a blocker activity event on every mission that researches, so a
  fixture-backed run is never silent.

A live selection with credentials never yields the fixture provider.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import Field

from callswarm.config.settings import Settings
from callswarm.events import ActivityEventEmitter
from callswarm.evidence import EvidenceEngine
from callswarm.llm import LLMProvider
from callswarm.models import (
    ActivityEvent,
    ActivityEventType,
    CandidateEntity,
    DomainModel,
    HardConstraint,
    InformationGap,
    Mission,
    RawPage,
    RawResult,
    ResearchArtifact,
    ResearchQuery,
    SourceType,
)
from callswarm.persistence import (
    CandidateEntityRepository,
    Database,
    InformationGapRepository,
    ResearchArtifactRepository,
)
from callswarm.research.fixture import FixtureResearchProvider
from callswarm.research.gaps import DecisionAttribute, GapReport, identify_gaps
from callswarm.research.live import GeminiGroundedResearchProvider
from callswarm.research.pipeline import (
    ConstraintExclusion,
    NormalizationResult,
    apply_hard_constraints,
    artifact_for,
    normalize,
)
from callswarm.research.provider import PageFetchRefused, ResearchError, ResearchProvider

logger = logging.getLogger(__name__)

FALLBACK_PREFIX = "live research unavailable"


@dataclass(frozen=True)
class ResearchSelection:
    provider: ResearchProvider
    requested: str
    fallback_reason: str | None = None


def select_research_provider(settings: Settings) -> ResearchSelection:
    requested = settings.research_provider
    if requested == "fixture":
        return ResearchSelection(FixtureResearchProvider(settings.research_fixture_dir), requested)
    try:
        live = GeminiGroundedResearchProvider(settings)
    except ResearchError as exc:
        reason = f"{FALLBACK_PREFIX}: {exc}; using labelled fixtures"
        logger.error("%s", reason)
        return ResearchSelection(
            FixtureResearchProvider(settings.research_fixture_dir), requested, reason
        )
    return ResearchSelection(live, requested)


class ResearchPassResult(DomainModel):
    discovered: int = 0
    normalized: int = 0
    shortlisted: int = 0
    excluded: int = 0
    gap_count: int = 0
    conflicted_claims: int = 0
    source_types: list[str] = Field(default_factory=list)
    artifacts: list[ResearchArtifact] = Field(default_factory=list)
    shortlist: list[CandidateEntity] = Field(default_factory=list)
    exclusions: list[ConstraintExclusion] = Field(default_factory=list)
    gaps: list[InformationGap] = Field(default_factory=list)
    report: GapReport = Field(default_factory=GapReport)


class ResearchService:
    """The one door to research for tools and the orchestrator."""

    def __init__(
        self,
        provider: ResearchProvider,
        llm: LLMProvider,
        database: Database,
        emitter: ActivityEventEmitter,
        *,
        fallback_reason: str | None = None,
        evidence: EvidenceEngine | None = None,
    ) -> None:
        self.provider = provider
        self.fallback_reason = fallback_reason
        self._llm = llm
        self._database = database
        self._emitter = emitter
        # Claims are never written here directly: the evidence engine is the
        # single write path and owns reconciliation.
        self._evidence = evidence or EvidenceEngine(database, emitter)
        self._fallback_recorded: set[str] = set()

    # --- blocker -----------------------------------------------------------------
    async def record_fallback(self, mission_id: str) -> None:
        """Emit the fallback blocker once per mission per process."""
        if self.fallback_reason is None or mission_id in self._fallback_recorded:
            return
        self._fallback_recorded.add(mission_id)
        await self._emitter.emit(
            ActivityEvent(
                mission_id=mission_id,
                event_type=ActivityEventType.SYSTEM,
                summary=self.fallback_reason,
                payload={
                    "blocker": True,
                    "research_provider": self.provider.name,
                    "source_type": _source_type_of(self.provider),
                },
            )
        )

    # --- primitives used by tools ---------------------------------------------------
    async def search(
        self, mission_id: str, query: ResearchQuery, *, agent_id: str | None = None
    ) -> list[RawResult]:
        await self.record_fallback(mission_id)
        results = await self.provider.search(query)
        async with self._database.session() as session:
            repo = ResearchArtifactRepository(session)
            for result in results:
                await repo.add(artifact_for(mission_id, result))
        await self._emitter.emit(
            ActivityEvent(
                mission_id=mission_id,
                event_type=ActivityEventType.RESEARCH_EVENT,
                summary=(
                    f"search via {self.provider.name}: {len(results)} result(s) for "
                    f"{query.text[:80]!r}"
                ),
                agent_id=agent_id,
                payload={
                    "provider": self.provider.name,
                    "source_types": sorted({r.provenance.source_type.value for r in results}),
                    "result_count": len(results),
                },
            )
        )
        return results

    async def fetch_public_page(
        self, mission_id: str, url: str, *, agent_id: str | None = None
    ) -> RawPage | None:
        await self.record_fallback(mission_id)
        try:
            page = await self.provider.fetch_public_page(url)
        except PageFetchRefused as exc:
            await self._emit_fetch(mission_id, url, agent_id, f"refused ({exc.reason})")
            raise
        await self._emit_fetch(
            mission_id, url, agent_id, "fetched" if page is not None else "unavailable"
        )
        return page

    async def _emit_fetch(
        self, mission_id: str, url: str, agent_id: str | None, outcome: str
    ) -> None:
        await self._emitter.emit(
            ActivityEvent(
                mission_id=mission_id,
                event_type=ActivityEventType.RESEARCH_EVENT,
                summary=f"page fetch via {self.provider.name}: {outcome}",
                agent_id=agent_id,
                payload={"provider": self.provider.name, "url": url, "outcome": outcome},
            )
        )

    # --- full pass ------------------------------------------------------------------
    async def run_pass(
        self,
        mission: Mission,
        queries: list[ResearchQuery],
        *,
        kind_hint: str | None = None,
        constraints: list[HardConstraint] | None = None,
        decision_attributes: list[DecisionAttribute] | None = None,
    ) -> ResearchPassResult:
        """search → normalize → hard constraints → gaps, persisted, with one
        counted activity event."""
        await self.record_fallback(mission.id)
        raw: list[RawResult] = []
        for query in queries:
            raw.extend(await self.provider.search(query))
        normalized: NormalizationResult = await normalize(self._llm, mission.id, raw, kind_hint)
        shortlist, exclusions = apply_hard_constraints(
            normalized.candidates, constraints if constraints is not None else []
        )
        excluded_ids = {e.candidate_id for e in exclusions}
        report = identify_gaps(mission.id, shortlist, decision_attributes or [], normalized.claims)
        async with self._database.session() as session:
            artifacts = ResearchArtifactRepository(session)
            for artifact in normalized.artifacts:
                await artifacts.add(artifact)
            candidates = CandidateEntityRepository(session)
            for candidate in shortlist:
                await candidates.add(candidate)
            for candidate in normalized.candidates:
                if candidate.id in excluded_ids:
                    await candidates.add(
                        candidate.model_copy(update={"passed_hard_constraints": False})
                    )
            gap_repo = InformationGapRepository(session)
            for gap in report.gaps:
                await gap_repo.add(gap)
        # The engine reconciles against everything already known for the
        # mission, so a conflict with an earlier pass is caught here too.
        await self._evidence.ingest(normalized.claims, source=f"research_pass:{self.provider.name}")
        source_types = sorted({a.source_type.value for a in normalized.artifacts})
        result = ResearchPassResult(
            discovered=len(raw),
            normalized=len(normalized.candidates),
            shortlisted=len(shortlist),
            excluded=len(excluded_ids),
            gap_count=len(report.gaps),
            conflicted_claims=len(report.conflicted_claims),
            source_types=source_types,
            artifacts=normalized.artifacts,
            shortlist=shortlist,
            exclusions=exclusions,
            gaps=report.gaps,
            report=report,
        )
        await self._emitter.emit(
            ActivityEvent(
                mission_id=mission.id,
                event_type=ActivityEventType.RESEARCH_EVENT,
                summary=(
                    f"research pass via {self.provider.name}: {result.discovered} discovered → "
                    f"{result.normalized} normalized → {result.shortlisted} shortlisted → "
                    f"{result.gap_count} gap(s)"
                ),
                payload={
                    "provider": self.provider.name,
                    "source_types": source_types,
                    "discovered": result.discovered,
                    "normalized": result.normalized,
                    "shortlisted": result.shortlisted,
                    "excluded": result.excluded,
                    "gaps": result.gap_count,
                    "conflicted_claims": result.conflicted_claims,
                    "exclusions": [e.model_dump(mode="json") for e in exclusions],
                    "knowledge_table": report.table.model_dump(mode="json"),
                },
            )
        )
        return result


def _source_type_of(provider: ResearchProvider) -> str | None:
    source_type = getattr(provider, "source_type", None)
    return source_type.value if isinstance(source_type, SourceType) else None
