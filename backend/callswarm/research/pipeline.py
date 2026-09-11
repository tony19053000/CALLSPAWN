"""Raw research results → comparable candidate entities → constraint shortlist.

Domain-agnostic by construction: no attribute name appears in this module.
The model proposes entities and claims from fenced, untrusted text; code
decides everything else — provenance inheritance, phone handling, dedup and
every hard-constraint verdict.

Phone rule (mirrors the CALL-E rule): a phone is stored as ``phone_e164`` only
when the extracted string already *is* E.164. Anything else is kept verbatim
in ``attributes.raw_phone`` — never reformatted or guessed.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from callswarm.evidence.normalize import normalize_key
from callswarm.llm import LLMProvider
from callswarm.models import (
    CandidateEntity,
    ConstraintOperator,
    ContactInfo,
    DomainModel,
    EvidenceClaim,
    EvidenceStatus,
    ExtractedClaim,
    HardConstraint,
    JsonValue,
    RawResult,
    ResearchArtifact,
)

logger = logging.getLogger(__name__)

E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
RAW_PHONE_KEY = "raw_phone"
DEFAULT_BATCH_SIZE = 8

# --- model-facing schema ------------------------------------------------------------


class ClaimExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    predicate: str = Field(min_length=1)
    value: JsonValue = None
    quote: str = Field(default="", description="The exact source text the claim rests on.")


class ExtractedEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_index: int = Field(ge=0, description="Index of the research result this came from.")
    kind: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    phone: str | None = Field(default=None, description="Phone exactly as written, or null.")
    website: str | None = None
    extracted_claims: list[ClaimExtraction] = Field(default_factory=list)


class NormalizationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entities: list[ExtractedEntity] = Field(default_factory=list)


NORMALIZE_INSTRUCTION = (
    "You extract structured entities from research results. Each result in the data block "
    "is numbered by 'index'. For every distinct real-world entity you can identify, return one "
    "entry with: source_index (the result it came from), kind (a short generic type), "
    "display_name, attributes (an open object of the facts stated about it, using short "
    "snake_case keys and literal values), phone (copied exactly as written, or null), website, "
    "and extracted_claims (one per stated fact: predicate = the attribute key, value, and the "
    "quote it rests on). Never invent values; omit what is not stated. Return only JSON."
)


# --- results ---------------------------------------------------------------------------


class NormalizationResult(DomainModel):
    candidates: list[CandidateEntity] = Field(default_factory=list)
    artifacts: list[ResearchArtifact] = Field(default_factory=list)
    claims: list[EvidenceClaim] = Field(default_factory=list)
    raw_count: int = 0
    extracted_count: int = 0

    @property
    def merged_count(self) -> int:
        return self.extracted_count - len(self.candidates)


class ConstraintExclusion(DomainModel):
    candidate_id: str
    display_name: str
    constraint_key: str
    operator: ConstraintOperator
    expected: JsonValue = None
    actual_value: JsonValue = None
    reason: str = Field(min_length=1)


class ConstraintFilterResult(NamedTuple):
    shortlist: list[CandidateEntity]
    exclusions: list[ConstraintExclusion]


# --- helpers -----------------------------------------------------------------------------


def normalize_name(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", name.lower()).split())


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    if not host:
        return None
    return f"{host}{parts.path.rstrip('/')}"


def lookup_attribute(attributes: dict[str, Any], key: str) -> tuple[bool, Any]:
    """Case/punctuation-insensitive attribute lookup. ``(present, value)``;
    a ``None`` value counts as absent."""
    wanted = normalize_key(key)
    for name, value in attributes.items():
        if normalize_key(name) == wanted:
            return value is not None, value
    return False, None


def _result_for_model(index: int, result: RawResult) -> dict[str, Any]:
    return {
        "index": index,
        "title": result.title,
        "url": result.url,
        "snippet": result.snippet,
        "data": result.data,
    }


def artifact_for(mission_id: str, result: RawResult) -> ResearchArtifact:
    prov = result.provenance
    return ResearchArtifact(
        mission_id=mission_id,
        source=result.url or prov.reference or prov.provider_name,
        source_type=prov.source_type,
        retrieved_at=prov.retrieved_at,
        provenance=prov,
    )


def _apply_phone(entity: ExtractedEntity, attributes: dict[str, Any]) -> ContactInfo:
    raw = entity.phone.strip() if isinstance(entity.phone, str) else None
    if raw and E164_RE.match(raw):
        phone_e164: str | None = raw
    else:
        phone_e164 = None
        if raw:
            attributes[RAW_PHONE_KEY] = raw
    return ContactInfo(phone_e164=phone_e164, website=entity.website or None)


# --- normalize -----------------------------------------------------------------------------


async def normalize(
    llm: LLMProvider,
    mission_id: str,
    raw_results: list[RawResult],
    kind_hint: str | None = None,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> NormalizationResult:
    """One structured model call per batch; provenance and claims assigned in code."""
    artifacts = [artifact_for(mission_id, r) for r in raw_results]
    candidates: list[CandidateEntity] = []
    claims: list[EvidenceClaim] = []
    extracted_count = 0
    for start in range(0, len(raw_results), batch_size):
        batch = raw_results[start : start + batch_size]
        payload = [_result_for_model(start + i, r) for i, r in enumerate(batch)]
        instruction = NORMALIZE_INSTRUCTION
        if kind_hint:
            instruction += f" The mission is looking for entities of kind: {kind_hint!r}."
        parsed = await llm.generate_structured(
            instruction,
            {"research results": json.dumps(payload, ensure_ascii=False)},
            NormalizationBatch,
        )
        for entity in parsed.entities:
            if not (start <= entity.source_index < start + len(batch)):
                logger.warning("entity %r cites a result outside its batch", entity.display_name)
                continue
            artifact = artifacts[entity.source_index]
            extracted_count += 1
            attributes: dict[str, Any] = dict(entity.attributes)
            contact = _apply_phone(entity, attributes)
            candidate = CandidateEntity(
                mission_id=mission_id,
                kind=entity.kind,
                display_name=entity.display_name,
                attributes=attributes,
                contact=contact,
                source_refs=[artifact.id],
                source_types=[artifact.source_type],
            )
            if artifact.entity_ref is None:
                artifact.entity_ref = candidate.id
            candidates.append(candidate)
            claims.extend(_claims_for(candidate, entity, artifact))
    merged_candidates, merged_claims = deduplicate(candidates, claims)
    return NormalizationResult(
        candidates=merged_candidates,
        artifacts=artifacts,
        claims=merged_claims,
        raw_count=len(raw_results),
        extracted_count=extracted_count,
    )


def _claims_for(
    candidate: CandidateEntity, entity: ExtractedEntity, artifact: ResearchArtifact
) -> list[EvidenceClaim]:
    claimed: dict[str, ClaimExtraction] = {}
    for claim in entity.extracted_claims:
        claimed.setdefault(normalize_key(claim.predicate), claim)
    # Attributes the model stated but did not claim explicitly still enter the
    # Reality Graph, so every attribute is traceable to its artifact.
    for key, value in candidate.attributes.items():
        if key != RAW_PHONE_KEY and normalize_key(key) not in claimed and value is not None:
            claimed[normalize_key(key)] = ClaimExtraction(predicate=key, value=value)
    out: list[EvidenceClaim] = []
    for claim in claimed.values():
        artifact.extracted_claims.append(
            ExtractedClaim(
                subject=candidate.display_name,
                predicate=claim.predicate,
                value=claim.value,
                quote=claim.quote,
            )
        )
        out.append(
            EvidenceClaim(
                mission_id=candidate.mission_id,
                subject=candidate.display_name,
                predicate=claim.predicate,
                value=claim.value,
                source_type=artifact.source_type,
                source_reference=f"artifact:{artifact.id}",
                timestamp=artifact.retrieved_at,
                evidence_status=EvidenceStatus.WEB_SUPPORTED,
                entity_id=candidate.id,
            )
        )
    return out


# --- dedup -----------------------------------------------------------------------------


def _match_keys(candidate: CandidateEntity) -> set[str]:
    keys = {f"name:{normalize_name(candidate.display_name)}"}
    if candidate.contact.phone_e164:
        keys.add(f"phone:{candidate.contact.phone_e164}")
    site = normalize_url(candidate.contact.website)
    if site:
        keys.add(f"url:{site}")
    return keys


def deduplicate(
    candidates: list[CandidateEntity], claims: list[EvidenceClaim]
) -> tuple[list[CandidateEntity], list[EvidenceClaim]]:
    """Merge candidates sharing a normalized name, phone or website. Attributes
    union (first value wins; disagreements stay visible as claims), provenance
    union. Deterministic: input order decides the survivor."""
    survivors: list[CandidateEntity] = []
    index: dict[str, int] = {}
    remap: dict[str, str] = {}
    for candidate in candidates:
        keys = _match_keys(candidate)
        hit = next((index[k] for k in keys if k in index), None)
        if hit is None:
            survivors.append(candidate)
            for key in keys:
                index[key] = len(survivors) - 1
            continue
        survivor = survivors[hit]
        merged_attributes = dict(survivor.attributes)
        for key, value in candidate.attributes.items():
            merged_attributes.setdefault(key, value)
        contact = survivor.contact.model_copy(
            update={
                "phone_e164": survivor.contact.phone_e164 or candidate.contact.phone_e164,
                "website": survivor.contact.website or candidate.contact.website,
            }
        )
        survivors[hit] = survivor.model_copy(
            update={
                "attributes": merged_attributes,
                "contact": contact,
                "source_refs": _union(survivor.source_refs, candidate.source_refs),
                "source_types": _union(survivor.source_types, candidate.source_types),
            }
        )
        remap[candidate.id] = survivor.id
        for key in _match_keys(survivors[hit]):
            index.setdefault(key, hit)
    merged_claims = [
        c.model_copy(update={"entity_id": remap[c.entity_id]}) if c.entity_id in remap else c
        for c in claims
    ]
    return survivors, merged_claims


def _union(first: list[Any], second: list[Any]) -> list[Any]:
    out = list(first)
    for item in second:
        if item not in out:
            out.append(item)
    return out


# --- hard constraints ----------------------------------------------------------------------


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _same(a: Any, b: Any) -> bool:
    na, nb = _number(a), _number(b)
    if na is not None and nb is not None:
        return na == nb
    if isinstance(a, str) and isinstance(b, str):
        return normalize_name(a) == normalize_name(b)
    return bool(a == b)


def evaluate_constraint(operator: ConstraintOperator, expected: Any, actual: Any) -> bool | None:
    """``True``/``False`` verdict, or ``None`` when the values cannot be compared."""
    if operator in (ConstraintOperator.EQ, ConstraintOperator.NE):
        verdict = _same(actual, expected)
        return verdict if operator is ConstraintOperator.EQ else not verdict
    if operator in (
        ConstraintOperator.LT,
        ConstraintOperator.LE,
        ConstraintOperator.GT,
        ConstraintOperator.GE,
    ):
        na, ne = _number(actual), _number(expected)
        if na is None or ne is None:
            return None
        return {
            ConstraintOperator.LT: na < ne,
            ConstraintOperator.LE: na <= ne,
            ConstraintOperator.GT: na > ne,
            ConstraintOperator.GE: na >= ne,
        }[operator]
    if operator in (ConstraintOperator.IN, ConstraintOperator.NOT_IN):
        if not isinstance(expected, list):
            return None
        member = any(_same(actual, item) for item in expected)
        return member if operator is ConstraintOperator.IN else not member
    if operator is ConstraintOperator.CONTAINS:
        if isinstance(actual, list):
            return any(_same(item, expected) for item in actual)
        if isinstance(actual, str) and isinstance(expected, str):
            return normalize_name(expected) in normalize_name(actual)
        return None
    # ConstraintOperator.REQUIRED: the attribute is present (absent values never
    # reach this function), so it fails only when it is explicitly falsy.
    return bool(actual)


def apply_hard_constraints(
    candidates: list[CandidateEntity], constraints: list[HardConstraint]
) -> ConstraintFilterResult:
    """Code-enforced pass/fail. A known value that fails excludes the candidate
    with a recorded reason; an absent or incomparable value never excludes —
    it is flagged in ``unverified_constraint_keys`` for the gap engine."""
    shortlist: list[CandidateEntity] = []
    exclusions: list[ConstraintExclusion] = []
    for candidate in candidates:
        failures: list[ConstraintExclusion] = []
        unverified: list[str] = []
        for constraint in constraints:
            present, actual = lookup_attribute(candidate.attributes, constraint.key)
            if not present:
                unverified.append(constraint.key)
                continue
            verdict = evaluate_constraint(constraint.operator, constraint.value, actual)
            if verdict is None:
                unverified.append(constraint.key)
                continue
            if not verdict:
                failures.append(
                    ConstraintExclusion(
                        candidate_id=candidate.id,
                        display_name=candidate.display_name,
                        constraint_key=constraint.key,
                        operator=constraint.operator,
                        expected=constraint.value,
                        actual_value=actual,
                        reason=(
                            f"{constraint.key} is {actual!r}, which fails "
                            f"{constraint.operator.value} {constraint.value!r}"
                        ),
                    )
                )
        if failures:
            exclusions.extend(failures)
            continue
        shortlist.append(
            candidate.model_copy(
                update={"passed_hard_constraints": True, "unverified_constraint_keys": unverified}
            )
        )
    return ConstraintFilterResult(shortlist, exclusions)
