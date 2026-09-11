"""Call intents and call runs. Status values mirror CALL-E exactly."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from callswarm.models.base import DomainModel, IdentifiedModel, utcnow
from callswarm.models.enums import (
    CallAuthorizationState,
    CallPattern,
    CallStatus,
    RecipientStatus,
)


class CallRecipient(DomainModel):
    entity_id: str | None = None
    phone_e164: str = Field(pattern=r"^\+[1-9]\d{6,14}$")
    locale: str | None = None
    region: str | None = None


class CallValueFactors(DomainModel):
    """Individually estimated factors. The final score is computed by code."""

    mission_impact: float = Field(ge=0.0, le=1.0)
    uncertainty: float = Field(ge=0.0, le=1.0)
    time_sensitivity: float = Field(ge=0.0, le=1.0)
    expected_value: float = Field(ge=0.0, le=1.0)
    strategy_changing_potential: float = Field(ge=0.0, le=1.0)
    redundancy: float = Field(ge=0.0, le=1.0)
    call_cost: float = Field(ge=0.0, le=1.0)


class CallIntent(IdentifiedModel):
    mission_id: str
    recipients: list[CallRecipient] = Field(min_length=1)
    purpose: str = Field(min_length=1)
    information_gaps: list[str] = Field(default_factory=list, description="InformationGap ids")
    expected_decision_impact: str = ""
    priority_factors: CallValueFactors | None = None
    priority_score: float | None = None
    call_pattern: CallPattern = CallPattern.ONE_SHOT
    authorization_state: CallAuthorizationState = CallAuthorizationState.NOT_REQUESTED
    call_goal: str = Field(min_length=1, description="The task text handed to the call provider")
    result_schema: dict[str, Any] = Field(default_factory=dict)
    recipient_result_schema: dict[str, Any] | None = None
    rejection_reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class CompletionConfidence(DomainModel):
    score: float = Field(ge=0.0, le=1.0)
    label: str = ""


class RecipientResult(IdentifiedModel):
    """One entry per recipient of a fan-out call."""

    call_run_id: str | None = None
    recipient_ref: str
    phone_masked: str
    status: RecipientStatus
    structured_result: dict[str, Any] | None = None
    summary: str = ""


class CallRun(IdentifiedModel):
    mission_id: str
    call_intent_id: str
    calle_call_id: str | None = Field(
        default=None,
        description="CALL-E CallTask id. Not the spec's attempt-level provider_call_id.",
    )
    status: CallStatus = CallStatus.QUEUED
    recipient_masked: str = ""
    started_at: datetime | None = None
    completed_at: datetime | None = None
    structured_result: dict[str, Any] | None = None
    summary: str = ""
    task_completed: bool | None = None
    recipient_results: list[RecipientResult] = Field(default_factory=list)
    transcript_reference: str | None = None
    evidence: list[str] = Field(default_factory=list)
    confidence: CompletionConfidence | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    is_simulated: bool = Field(
        description="True for every FakeCallProvider result. Never defaults silently."
    )
    created_at: datetime = Field(default_factory=utcnow)
