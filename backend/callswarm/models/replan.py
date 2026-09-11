"""Replan decisions (CS-041) and constraint revisions (CS-042)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from callswarm.models.base import DomainModel, IdentifiedModel, JsonValue, utcnow
from callswarm.models.enums import ReplanAction, ReplanTrigger
from callswarm.models.mission import ConstraintOperator


class ReplanDecision(IdentifiedModel):
    """One persisted replan decision: what triggered it, what the model
    proposed, what code actually applied, and how it ended."""

    mission_id: str
    trigger: ReplanTrigger
    trigger_refs: list[str] = Field(default_factory=list, description="Triggering artifact ids")
    action: ReplanAction
    target_id: str | None = Field(
        default=None, description="Agent, strategy or specialist id the action acted on"
    )
    proposed_action: ReplanAction | None = Field(
        default=None, description="The model's proposal when code rewrote it"
    )
    rationale: str = Field(default="", description="Activity summary, never reasoning")
    rewrite_reason: str | None = Field(
        default=None, description="Why code replaced the proposal (budget, loop guard, ...)"
    )
    details: dict[str, Any] = Field(
        default_factory=dict, description="Action parameters (queries, gap ids, ...)"
    )
    applied_at: datetime | None = None
    outcome: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class ConstraintUpdate(DomainModel):
    key: str = Field(min_length=1, max_length=200)
    operator: ConstraintOperator
    value: JsonValue = None


class ConstraintChange(DomainModel):
    """A typed, structured revision request. No free text: the chat layer
    translates before this is built."""

    updates: list[ConstraintUpdate] = Field(default_factory=list)
    locks: list[str] = Field(default_factory=list)
    unlocks: list[str] = Field(default_factory=list)
    preference_changes: dict[str, float] = Field(
        default_factory=dict, description="Soft preference key -> new weight"
    )
