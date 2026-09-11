"""Strategy candidates produced by the Strategy Architect."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from callswarm.models.base import IdentifiedModel, utcnow
from callswarm.models.enums import StrategyStatus


class StrategyCandidate(IdentifiedModel):
    mission_id: str
    title: str = Field(min_length=1)
    description: str = ""
    assumptions: list[str] = Field(default_factory=list)
    benefits: list[str] = Field(default_factory=list)
    drawbacks: list[str] = Field(default_factory=list)
    required_information: list[str] = Field(default_factory=list)
    expected_dependencies: list[str] = Field(default_factory=list)
    objective_axis: str = Field(
        default="", description="The objective this candidate optimizes for; unique per set"
    )
    status: StrategyStatus = StrategyStatus.PROPOSED
    status_reason: str | None = None
    revival_evidence_ref: str | None = Field(
        default=None, description="Reference to the evidence that revived a pruned strategy"
    )
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
