"""Research layer: providers with mandatory provenance, normalization, gaps."""

from callswarm.research.fixture import FixtureResearchProvider
from callswarm.research.gaps import (
    DecisionAttribute,
    GapReport,
    KnowledgeTable,
    decision_attributes_from_mission,
    identify_gaps,
)
from callswarm.research.live import GeminiGroundedResearchProvider
from callswarm.research.pipeline import (
    ConstraintExclusion,
    NormalizationResult,
    apply_hard_constraints,
    normalize,
)
from callswarm.research.provider import (
    PageFetchRefused,
    ResearchError,
    ResearchNotConfigured,
    ResearchProvider,
)
from callswarm.research.service import (
    ResearchPassResult,
    ResearchSelection,
    ResearchService,
    select_research_provider,
)

__all__ = [
    "ConstraintExclusion",
    "DecisionAttribute",
    "FixtureResearchProvider",
    "GapReport",
    "GeminiGroundedResearchProvider",
    "KnowledgeTable",
    "NormalizationResult",
    "PageFetchRefused",
    "ResearchError",
    "ResearchNotConfigured",
    "ResearchPassResult",
    "ResearchProvider",
    "ResearchSelection",
    "ResearchService",
    "apply_hard_constraints",
    "decision_attributes_from_mission",
    "identify_gaps",
    "normalize",
    "select_research_provider",
]
