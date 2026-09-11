"""Runtime agents as data: AgentSpec, AgentRequest and AgentRun."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from callswarm.models.base import IdentifiedModel, utcnow
from callswarm.models.enums import AgentRequestStatus, AgentState, RiskLevel


class AgentSpec(IdentifiedModel):
    """A generated specialist. Never a Python class; always a row."""

    mission_id: str
    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    why_needed: str = Field(min_length=1)
    owns: str = Field(default="", description="The exact problem this agent owns")
    strategy_id: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    required_inputs: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list, description="AgentSpec ids")
    expected_output_schema: dict[str, Any] = Field(default_factory=dict)
    does_not_control: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    state: AgentState = AgentState.CREATED
    state_reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class AgentRequest(IdentifiedModel):
    """A specialist's request that a new specialist be created.

    Only the Main Orchestrator may act on it; a specialist cannot spawn.
    """

    mission_id: str
    requesting_agent_id: str
    proposed_role: str = Field(min_length=1)
    justification: str = Field(min_length=1)
    required_inputs: list[str] = Field(default_factory=list)
    status: AgentRequestStatus = AgentRequestStatus.PENDING
    decision_reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class AgentRun(IdentifiedModel):
    mission_id: str
    agent_id: str
    status: AgentState = AgentState.CREATED
    started_at: datetime | None = None
    completed_at: datetime | None = None
    activity_summary: str = ""
    output_artifact: dict[str, Any] | None = None
    error: str | None = None
    stop_reason: str | None = None
    call_intent_id: str | None = Field(
        default=None, description="Set when the run is WAITING_FOR_CALL on a requested intent"
    )
