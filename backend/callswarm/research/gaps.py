"""Information-gap engine: what is known, unknown or conflicted per candidate.

For every shortlisted candidate x decision attribute the evidence claims are
classified in code:

* ``KNOWN`` — at least one claim and all values agree (or, with no claim, the
  attribute is present on the candidate);
* ``CONFLICTED`` — claims with different values. Both are kept, every one of
  them is marked ``CONFLICTED`` and cross-linked; nothing is averaged;
* ``UNKNOWN`` — no claim and no attribute.

Importance and confidence are code-computed from decision weights and source
counts. The compact per-candidate table is a structured artifact for the UI.
"""

from __future__ import annotations

from pydantic import Field

from callswarm.evidence.normalize import canonical_value, normalize_key
from callswarm.models import (
    AttributeKnowledge,
    CandidateEntity,
    DomainModel,
    EvidenceClaim,
    EvidenceStatus,
    Importance,
    InformationGap,
    MissionSpec,
    StrategyCandidate,
)
from callswarm.research.pipeline import lookup_attribute

DEFAULT_RESOLUTION_METHODS: tuple[str, ...] = ("web", "call", "user")

# Weights assigned when decision attributes are derived from a mission.
HARD_CONSTRAINT_WEIGHT = 1.0
SOFT_PREFERENCE_CEILING = 0.8
STRATEGY_INFORMATION_WEIGHT = 0.5

# Code-computed confidence per classification.
CONFIDENCE_UNKNOWN = 0.0
CONFIDENCE_CONFLICTED = 0.2
CONFIDENCE_ATTRIBUTE_ONLY = 0.5
CONFIDENCE_SINGLE_SOURCE = 0.6
CONFIDENCE_MULTI_SOURCE = 0.8


class DecisionAttribute(DomainModel):
    """An attribute some decision depends on. ``weight`` in [0, 1]."""

    key: str = Field(min_length=1)
    decision: str = Field(min_length=1)
    weight: float = Field(default=0.5, ge=0.0, le=1.0)
    resolution_methods: list[str] = Field(default_factory=lambda: list(DEFAULT_RESOLUTION_METHODS))


class KnowledgeRow(DomainModel):
    candidate_id: str
    display_name: str
    statuses: dict[str, AttributeKnowledge] = Field(default_factory=dict)


class KnowledgeTable(DomainModel):
    """Compact KNOWN/UNKNOWN/CONFLICTED grid, one row per shortlisted candidate."""

    attribute_keys: list[str] = Field(default_factory=list)
    rows: list[KnowledgeRow] = Field(default_factory=list)
    totals: dict[str, int] = Field(default_factory=dict)


class GapReport(DomainModel):
    gaps: list[InformationGap] = Field(default_factory=list)
    table: KnowledgeTable = Field(default_factory=KnowledgeTable)
    conflicted_claims: list[EvidenceClaim] = Field(
        default_factory=list, description="Claims re-marked CONFLICTED; persist these updates."
    )


# --- decision attributes from the mission ----------------------------------------------


def importance_from_weight(weight: float) -> Importance:
    if weight >= 0.9:
        return Importance.CRITICAL
    if weight >= 0.6:
        return Importance.HIGH
    if weight >= 0.3:
        return Importance.MEDIUM
    return Importance.LOW


def decision_attributes_from_mission(
    spec: MissionSpec | None, strategies: list[StrategyCandidate]
) -> list[DecisionAttribute]:
    """Derive decision attributes: hard constraints (weight 1.0), soft
    preferences (scaled so the heaviest is 0.8), priority weights (scaled to
    1.0) and each strategy's ``required_information`` (0.5). Duplicate keys
    keep the highest weight and merge their decisions."""
    merged: dict[str, DecisionAttribute] = {}

    def add(key: str, decision: str, weight: float) -> None:
        norm = normalize_key(key)
        if not norm:
            return
        weight = max(0.0, min(1.0, weight))
        existing = merged.get(norm)
        if existing is None:
            merged[norm] = DecisionAttribute(key=key, decision=decision, weight=weight)
            return
        decisions = (
            existing.decision
            if decision in existing.decision
            else (f"{existing.decision}; {decision}")
        )
        merged[norm] = existing.model_copy(
            update={"decision": decisions, "weight": max(existing.weight, weight)}
        )

    if spec is not None:
        for constraint in spec.hard_constraints:
            add(constraint.key, f"hard constraint: {constraint.key}", HARD_CONSTRAINT_WEIGHT)
        max_soft = max((p.weight for p in spec.soft_preferences), default=0.0)
        for pref in spec.soft_preferences:
            scaled = (pref.weight / max_soft) * SOFT_PREFERENCE_CEILING if max_soft else 0.0
            add(pref.key, f"soft preference: {pref.key}", scaled)
        max_priority = max(spec.priority_weights.values(), default=0.0)
        for key, weight in spec.priority_weights.items():
            add(key, f"priority: {key}", weight / max_priority if max_priority else 0.0)
    for strategy in strategies:
        for item in strategy.required_information:
            add(item, f"strategy: {strategy.title}", STRATEGY_INFORMATION_WEIGHT)
    return list(merged.values())


# --- classification -----------------------------------------------------------------


def _claims_for(
    claims: list[EvidenceClaim], candidate: CandidateEntity, key: str
) -> list[EvidenceClaim]:
    wanted = normalize_key(key)
    return [
        c
        for c in claims
        if c.entity_id == candidate.id
        and normalize_key(c.predicate) == wanted
        and c.evidence_status is not EvidenceStatus.REJECTED
    ]


def classify(
    candidate: CandidateEntity, key: str, claims: list[EvidenceClaim]
) -> tuple[AttributeKnowledge, float, list[EvidenceClaim]]:
    """``(status, confidence, relevant_claims)`` for one candidate x attribute."""
    relevant = _claims_for(claims, candidate, key)
    if not relevant:
        present, _ = lookup_attribute(candidate.attributes, key)
        if present:
            return AttributeKnowledge.KNOWN, CONFIDENCE_ATTRIBUTE_ONLY, []
        return AttributeKnowledge.UNKNOWN, CONFIDENCE_UNKNOWN, []
    distinct = {canonical_value(c.value) for c in relevant}
    if len(distinct) > 1:
        return AttributeKnowledge.CONFLICTED, CONFIDENCE_CONFLICTED, relevant
    sources = {c.source_reference for c in relevant}
    confidence = CONFIDENCE_MULTI_SOURCE if len(sources) > 1 else CONFIDENCE_SINGLE_SOURCE
    return AttributeKnowledge.KNOWN, confidence, relevant


def _mark_conflicted(relevant: list[EvidenceClaim]) -> list[EvidenceClaim]:
    ids = [c.id for c in relevant]
    return [
        c.model_copy(
            update={
                "evidence_status": EvidenceStatus.CONFLICTED,
                "conflicts": [other for other in ids if other != c.id],
            }
        )
        for c in relevant
    ]


def identify_gaps(
    mission_id: str,
    shortlist: list[CandidateEntity],
    decision_attributes: list[DecisionAttribute],
    claims: list[EvidenceClaim],
) -> GapReport:
    """Classify every shortlisted candidate x decision attribute. A candidate
    whose attributes are all KNOWN yields no gaps."""
    keys = [a.key for a in decision_attributes]
    rows: list[KnowledgeRow] = []
    gaps: list[InformationGap] = []
    conflicted: dict[str, EvidenceClaim] = {}
    totals = {status.value: 0 for status in AttributeKnowledge}
    for candidate in shortlist:
        statuses: dict[str, AttributeKnowledge] = {}
        for attribute in decision_attributes:
            status, confidence, relevant = classify(candidate, attribute.key, claims)
            statuses[attribute.key] = status
            totals[status.value] += 1
            if status is AttributeKnowledge.KNOWN:
                continue
            if status is AttributeKnowledge.CONFLICTED:
                for claim in _mark_conflicted(relevant):
                    conflicted[claim.id] = claim
                values = ", ".join(repr(c.value) for c in relevant)
                question = (
                    f"Sources disagree on '{attribute.key}' for {candidate.display_name} "
                    f"({values}). Which is correct?"
                )
            else:
                question = f"What is '{attribute.key}' for {candidate.display_name}?"
            gaps.append(
                InformationGap(
                    mission_id=mission_id,
                    question=question,
                    affected_decision=attribute.decision,
                    importance=importance_from_weight(attribute.weight),
                    current_confidence=confidence,
                    possible_resolution_methods=list(attribute.resolution_methods),
                    entity_id=candidate.id,
                )
            )
        rows.append(
            KnowledgeRow(
                candidate_id=candidate.id, display_name=candidate.display_name, statuses=statuses
            )
        )
    return GapReport(
        gaps=gaps,
        table=KnowledgeTable(attribute_keys=keys, rows=rows, totals=totals),
        conflicted_claims=list(conflicted.values()),
    )
