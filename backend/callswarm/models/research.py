"""Research artifacts, generic candidate entities and information gaps."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from callswarm.models.base import DomainModel, IdentifiedModel, JsonValue, utcnow
from callswarm.models.enums import GapStatus, Importance, SourceType


class Provenance(DomainModel):
    provider: str = Field(min_length=1)
    reference: str = ""
    query: str | None = None
    retrieved_at: datetime = Field(default_factory=utcnow)


class ExtractedClaim(DomainModel):
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    value: JsonValue = None


class ResearchArtifact(IdentifiedModel):
    mission_id: str
    source: str = Field(min_length=1)
    source_type: SourceType
    retrieved_at: datetime = Field(default_factory=utcnow)
    entity_ref: str | None = None
    extracted_claims: list[ExtractedClaim] = Field(default_factory=list)
    provenance: Provenance


class ContactInfo(DomainModel):
    """Contact details. ``phone_e164`` is stored in full only in the database and
    is masked by the shared sanitizer everywhere it is emitted."""

    phone_e164: str | None = Field(default=None, pattern=r"^\+[1-9]\d{6,14}$")
    region: str | None = None
    locale: str | None = None
    website: str | None = None


class CandidateEntity(IdentifiedModel):
    """Deliberately generic: a ``kind`` string plus open ``attributes``."""

    mission_id: str
    kind: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    attributes: dict[str, Any] = Field(default_factory=dict)
    contact: ContactInfo = Field(default_factory=ContactInfo)
    source_refs: list[str] = Field(default_factory=list)
    source_types: list[SourceType] = Field(default_factory=list)
    passed_hard_constraints: bool | None = None
    created_at: datetime = Field(default_factory=utcnow)


class InformationGap(IdentifiedModel):
    mission_id: str
    question: str = Field(min_length=1)
    affected_decision: str = ""
    importance: Importance = Importance.MEDIUM
    current_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    possible_resolution_methods: list[str] = Field(default_factory=list)
    entity_id: str | None = None
    status: GapStatus = GapStatus.OPEN
    created_at: datetime = Field(default_factory=utcnow)
