"""Evidence claims (the Reality Graph) and plan options."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from callswarm.models.base import DomainModel, IdentifiedModel, JsonValue, utcnow
from callswarm.models.enums import EvidenceStatus, Freshness, SourceType


class EvidenceClaim(IdentifiedModel):
    mission_id: str
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    value: JsonValue = None
    source_type: SourceType
    source_reference: str = ""
    timestamp: datetime = Field(default_factory=utcnow)
    freshness: Freshness = Freshness.FRESH
    evidence_status: EvidenceStatus = EvidenceStatus.UNKNOWN
    conflicts: list[str] = Field(default_factory=list, description="Conflicting claim ids")
    entity_id: str | None = None


class PlanComponent(DomainModel):
    name: str = Field(min_length=1)
    entity_id: str | None = None
    cost: float | None = None
    source_types: list[SourceType] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    locked: bool = False
    notes: str = ""


class PlanOption(IdentifiedModel):
    mission_id: str
    name: str = Field(min_length=1)
    components: list[PlanComponent] = Field(default_factory=list)
    total_cost: float | None = None
    currency: str | None = None
    hard_constraints_passed: bool = False
    soft_score: float = 0.0
    uncertainties: list[str] = Field(default_factory=list)
    evidence_summary: str = ""
    tradeoffs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def source_types(self) -> list[SourceType]:
        """Provenance union of every component; FIXTURE/SIMULATED surface here."""
        seen: dict[SourceType, None] = {}
        for component in self.components:
            for source_type in component.source_types:
                seen.setdefault(source_type, None)
        return list(seen)

    @property
    def contains_simulated_or_fixture(self) -> bool:
        return any(st in (SourceType.FIXTURE, SourceType.SIMULATED) for st in self.source_types)
