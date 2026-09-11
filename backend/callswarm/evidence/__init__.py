"""Evidence layer: the Reality Graph and its single write path."""

from callswarm.evidence.engine import (
    NON_ATTRIBUTE_PREDICATES,
    PHONE_ALLOWED_STATUSES,
    ClaimPage,
    ConflictNotice,
    EvidenceEngine,
    EvidenceTrace,
    IngestResult,
    TraceStep,
    normalize_predicate,
    normalize_subject,
    reconcile_group,
)

__all__ = [
    "NON_ATTRIBUTE_PREDICATES",
    "PHONE_ALLOWED_STATUSES",
    "ClaimPage",
    "ConflictNotice",
    "EvidenceEngine",
    "EvidenceTrace",
    "IngestResult",
    "TraceStep",
    "normalize_predicate",
    "normalize_subject",
    "reconcile_group",
]
