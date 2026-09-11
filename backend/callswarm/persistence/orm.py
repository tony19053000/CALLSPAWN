"""SQLAlchemy 2.x ORM tables mirroring the domain models.

Column types are PostgreSQL-compatible (``String``, ``Text``, ``Boolean``,
``Integer``, ``Float``, timezone-aware ``DateTime`` and generic ``JSON``).
Nested structures are stored as JSON columns; every scalar domain field has a
column of the same name, which lets the repositories map rows generically.

``MISSION_SCOPED_TABLES`` is the explicit cascade list. ``SuppressionEntry`` is
deliberately absent from it: an opt-out must outlive the mission that recorded
it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend, including SQLite."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {
        datetime: UTCDateTime,
        dict[str, Any]: JSON,
        list[Any]: JSON,
    }


def _mission_fk() -> Mapped[str]:
    return mapped_column(
        String(64), ForeignKey("missions.id", ondelete="CASCADE"), index=True, nullable=False
    )


class MissionRow(Base):
    __tablename__ = "missions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    authority_policy: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    call_budget: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    hard_constraints: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    soft_preferences: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    priority_weights: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    spec: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blocker: Mapped[str | None] = mapped_column(Text, nullable=True)


class MissionTransitionRow(Base):
    """Append-only log of applied mission state transitions."""

    __tablename__ = "mission_transitions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = _mission_fk()
    from_status: Mapped[str] = mapped_column(String(64), nullable=False)
    to_status: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)


class StrategyCandidateRow(Base):
    __tablename__ = "strategy_candidates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = _mission_fk()
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    assumptions: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    benefits: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    drawbacks: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    required_information: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    expected_dependencies: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    objective_axis: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    revival_evidence_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stale_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class AgentSpecRow(Base):
    __tablename__ = "agent_specs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = _mission_fk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(255), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    why_needed: Mapped[str] = mapped_column(Text, nullable=False)
    owns: Mapped[str] = mapped_column(Text, nullable=False, default="")
    strategy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    allowed_tools: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    required_inputs: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    dependencies: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    expected_output_schema: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    does_not_control: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    stop_conditions: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    state_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class AgentRequestRow(Base):
    __tablename__ = "agent_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = _mission_fk()
    requesting_agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_role: Mapped[str] = mapped_column(String(255), nullable=False)
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    required_inputs: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class AgentRunRow(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = _mission_fk()
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    activity_summary: Mapped[str] = mapped_column(Text, nullable=False)
    output_artifact: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_intent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stale_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ResearchArtifactRow(Base):
    __tablename__ = "research_artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = _mission_fk()
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    entity_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extracted_claims: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class CandidateEntityRow(Base):
    __tablename__ = "candidate_entities"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = _mission_fk()
    kind: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    contact: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_refs: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    source_types: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    passed_hard_constraints: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    unverified_constraint_keys: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class InformationGapRow(Base):
    __tablename__ = "information_gaps"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = _mission_fk()
    question: Mapped[str] = mapped_column(Text, nullable=False)
    affected_decision: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[str] = mapped_column(String(16), nullable=False)
    current_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    possible_resolution_methods: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class CallIntentRow(Base):
    __tablename__ = "call_intents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = _mission_fk()
    recipients: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    information_gaps: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    expected_decision_impact: Mapped[str] = mapped_column(Text, nullable=False)
    priority_factors: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    priority_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    call_pattern: Mapped[str] = mapped_column(String(32), nullable=False)
    authorization_state: Mapped[str] = mapped_column(String(32), nullable=False)
    call_goal: Mapped[str] = mapped_column(Text, nullable=False)
    result_schema: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    recipient_result_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    pattern_progress: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class CallRunRow(Base):
    __tablename__ = "call_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = _mission_fk()
    call_intent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    calle_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    recipient_masked: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    structured_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    task_completed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    transcript_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    confidence: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_simulated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class RecipientResultRow(Base):
    __tablename__ = "recipient_results"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = _mission_fk()
    call_run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("call_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    recipient_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    phone_masked: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    structured_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)


class EvidenceClaimRow(Base):
    __tablename__ = "evidence_claims"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = _mission_fk()
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    predicate: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_reference: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    freshness: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_status: Mapped[str] = mapped_column(String(32), nullable=False)
    conflicts: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    derived_from: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    simulated_lineage: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PlanOptionRow(Base):
    __tablename__ = "plan_options"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = _mission_fk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    components: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    total_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    hard_constraints_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    soft_score: Mapped[float] = mapped_column(Float, nullable=False)
    uncertainties: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    evidence_summary: Mapped[str] = mapped_column(Text, nullable=False)
    tradeoffs: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    constraint_keys: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stale_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class ReplanDecisionRow(Base):
    """Append-only log of replan decisions (CS-041)."""

    __tablename__ = "replan_decisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = _mission_fk()
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_refs: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    proposed_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    rewrite_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)


class ApprovalRow(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = _mission_fk()
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ActivityEventRow(Base):
    __tablename__ = "activity_events"

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    mission_id: Mapped[str] = _mission_fk()
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class ScheduledJobRow(Base):
    __tablename__ = "scheduled_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = _mission_fk()
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class SuppressionEntryRow(Base):
    """Mission-independent. No mission foreign key; not in the cascade list."""

    __tablename__ = "suppression_entries"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    phone_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class WebhookReceiptRow(Base):
    """One row per CALL-E webhook event id, written before any side effect so a
    duplicate delivery is a no-op. Not mission-scoped: the receipt is recorded
    before the payload is correlated to a mission, and it must survive."""

    __tablename__ = "webhook_receipts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    call_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)


# Explicit cascade list, deleted in this order (children before parents).
# SuppressionEntryRow is intentionally NOT here.
MISSION_SCOPED_TABLES: tuple[type[Base], ...] = (
    RecipientResultRow,
    CallRunRow,
    CallIntentRow,
    ScheduledJobRow,
    ApprovalRow,
    ReplanDecisionRow,
    PlanOptionRow,
    EvidenceClaimRow,
    InformationGapRow,
    CandidateEntityRow,
    ResearchArtifactRow,
    AgentRunRow,
    AgentRequestRow,
    AgentSpecRow,
    StrategyCandidateRow,
    MissionTransitionRow,
    ActivityEventRow,
)
