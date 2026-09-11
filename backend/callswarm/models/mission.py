"""Mission, MissionSpec, AuthorityPolicy and CallBudget."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from callswarm.models.base import DomainModel, IdentifiedModel, JsonValue, utcnow
from callswarm.models.enums import MissionStatus


class ConstraintOperator(StrEnum):
    EQ = "EQ"
    NE = "NE"
    LT = "LT"
    LE = "LE"
    GT = "GT"
    GE = "GE"
    IN = "IN"
    NOT_IN = "NOT_IN"
    CONTAINS = "CONTAINS"
    REQUIRED = "REQUIRED"


class HardConstraint(DomainModel):
    """A pass/fail rule enforced by deterministic code. Domain-agnostic key/value."""

    key: str = Field(min_length=1)
    operator: ConstraintOperator
    value: JsonValue = None
    description: str = ""
    locked: bool = False


class SoftPreference(DomainModel):
    """A weighted preference scored by the optimizer."""

    key: str = Field(min_length=1)
    direction: str = Field(default="maximize", pattern="^(maximize|minimize)$")
    weight: float = Field(default=1.0, ge=0.0)
    description: str = ""


class AuthorityPolicy(DomainModel):
    """What the user permits CallSwarm to do for a mission.

    Every field can only *narrow* CallSwarm's behaviour. There is deliberately no
    field that waives an approval: a consequential action always requires an
    ``APPROVED`` record regardless of this policy.
    """

    research_allowed: bool = True
    calls_allowed: bool = False
    max_call_count: int = Field(default=0, ge=0)
    negotiation_allowed: bool = False
    scheduled_follow_up_allowed: bool = False
    confirmation_calls_allowed: bool = False


class CallBudget(DomainModel):
    max_calls: int = Field(default=0, ge=0)
    calls_used: int = Field(default=0, ge=0)

    @property
    def remaining(self) -> int:
        return max(self.max_calls - self.calls_used, 0)


class ClarificationQuestion(IdentifiedModel):
    question: str = Field(min_length=1)
    unblocks_decision: str = ""
    critical: bool = False
    answer: str | None = None


class MissionSpec(DomainModel):
    """Validated representation of what the user actually wants."""

    mission_id: str
    summary: str = ""
    objectives: list[str] = Field(default_factory=list)
    hard_constraints: list[HardConstraint] = Field(default_factory=list)
    soft_preferences: list[SoftPreference] = Field(default_factory=list)
    priority_weights: dict[str, float] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    clarification_questions: list[ClarificationQuestion] = Field(default_factory=list)
    authority_policy: AuthorityPolicy = Field(default_factory=AuthorityPolicy)


class Mission(IdentifiedModel):
    user_goal: str = Field(min_length=1)
    status: MissionStatus = MissionStatus.MISSION_CREATED
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    authority_policy: AuthorityPolicy = Field(default_factory=AuthorityPolicy)
    call_budget: CallBudget = Field(default_factory=CallBudget)
    hard_constraints: list[HardConstraint] = Field(default_factory=list)
    soft_preferences: list[SoftPreference] = Field(default_factory=list)
    priority_weights: dict[str, float] = Field(default_factory=dict)
    spec: MissionSpec | None = None
    sensitive: bool = False
    blocker: str | None = None
