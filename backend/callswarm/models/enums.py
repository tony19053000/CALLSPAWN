"""State and status enums shared across CallSwarm.

CALL-E enums mirror the vendor's exact lower-case values (see
``04_CALL_E_INTEGRATION.md``); CallSwarm's own states are upper-case.
"""

from __future__ import annotations

from enum import StrEnum


class MissionStatus(StrEnum):
    """V1 mission state machine. ``FINAL_EXECUTION_*`` is deliberately absent."""

    MISSION_CREATED = "MISSION_CREATED"
    GOAL_UNDERSTANDING = "GOAL_UNDERSTANDING"
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
    CLARIFICATION_COMPLETE = "CLARIFICATION_COMPLETE"
    MISSION_SPEC_READY = "MISSION_SPEC_READY"
    STRATEGY_DISCOVERY_RUNNING = "STRATEGY_DISCOVERY_RUNNING"
    STRATEGY_SET_READY = "STRATEGY_SET_READY"
    SWARM_DESIGN_RUNNING = "SWARM_DESIGN_RUNNING"
    SWARM_READY = "SWARM_READY"
    RESEARCH_RUNNING = "RESEARCH_RUNNING"
    RESEARCH_REVIEW_RUNNING = "RESEARCH_REVIEW_RUNNING"
    INFORMATION_GAPS_READY = "INFORMATION_GAPS_READY"
    CALL_SELECTION_RUNNING = "CALL_SELECTION_RUNNING"
    CALL_PLAN_READY = "CALL_PLAN_READY"
    CALL_AUTHORIZATION_PENDING = "CALL_AUTHORIZATION_PENDING"
    CALL_AUTHORIZED = "CALL_AUTHORIZED"
    CALL_EXECUTION_RUNNING = "CALL_EXECUTION_RUNNING"
    CALL_RESULT_RECEIVED = "CALL_RESULT_RECEIVED"
    EVIDENCE_UPDATE_RUNNING = "EVIDENCE_UPDATE_RUNNING"
    REPLAN_DECISION_RUNNING = "REPLAN_DECISION_RUNNING"
    NEGOTIATION_OR_FOLLOWUP_RUNNING = "NEGOTIATION_OR_FOLLOWUP_RUNNING"
    OPTIMIZATION_RUNNING = "OPTIMIZATION_RUNNING"
    REVIEW_RUNNING = "REVIEW_RUNNING"
    REVIEW_FAILED = "REVIEW_FAILED"
    REVIEW_PASSED = "REVIEW_PASSED"
    PLAN_OPTIONS_READY = "PLAN_OPTIONS_READY"
    USER_DECISION_PENDING = "USER_DECISION_PENDING"
    MISSION_REVISION_RUNNING = "MISSION_REVISION_RUNNING"
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"
    CANCELED = "CANCELED"


class AgentState(StrEnum):
    CREATED = "CREATED"
    WAITING = "WAITING"
    READY = "READY"
    WORKING = "WORKING"
    BLOCKED = "BLOCKED"
    WAITING_FOR_DEPENDENCY = "WAITING_FOR_DEPENDENCY"
    WAITING_FOR_CALL = "WAITING_FOR_CALL"
    REVIEWING = "REVIEWING"
    COMPLETE = "COMPLETE"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class StrategyStatus(StrEnum):
    PROPOSED = "PROPOSED"
    ACTIVE = "ACTIVE"
    PRUNED = "PRUNED"
    IMPOSSIBLE = "IMPOSSIBLE"


class AgentRequestStatus(StrEnum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    MERGED = "MERGED"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Importance(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class GapStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    UNRESOLVABLE = "UNRESOLVABLE"


class AttributeKnowledge(StrEnum):
    """What the evidence says about one decision attribute of one candidate."""

    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    CONFLICTED = "CONFLICTED"


class SourceType(StrEnum):
    """Provenance of a claim. FIXTURE and SIMULATED propagate to the UI badge."""

    WEB = "WEB"
    PHONE = "PHONE"
    USER = "USER"
    DERIVED = "DERIVED"
    FIXTURE = "FIXTURE"
    SIMULATED = "SIMULATED"


class EvidenceStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    WEB_SUPPORTED = "WEB_SUPPORTED"
    PHONE_SUPPORTED = "PHONE_SUPPORTED"
    MULTI_SOURCE_SUPPORTED = "MULTI_SOURCE_SUPPORTED"
    CONFLICTED = "CONFLICTED"
    STALE = "STALE"
    REJECTED = "REJECTED"


class ReplanTrigger(StrEnum):
    """What prompted a replan decision (03 "Replanning")."""

    NEW_EVIDENCE = "NEW_EVIDENCE"
    CONFLICT = "CONFLICT"
    CALL_RESULT = "CALL_RESULT"
    CRITIC_FAIL = "CRITIC_FAIL"
    CONSTRAINT_CHANGE = "CONSTRAINT_CHANGE"
    STRATEGY_IMPOSSIBLE = "STRATEGY_IMPOSSIBLE"


class ReplanAction(StrEnum):
    """The closed set of actions a replan decision may take. The model
    proposes one; code validates and applies it."""

    RERUN_AGENT = "RERUN_AGENT"
    CREATE_SPECIALIST = "CREATE_SPECIALIST"
    STOP_AGENT = "STOP_AGENT"
    PRUNE_STRATEGY = "PRUNE_STRATEGY"
    REVIVE_STRATEGY = "REVIVE_STRATEGY"
    RESEARCH_PASS = "RESEARCH_PASS"
    CALL_ROUND = "CALL_ROUND"
    PROCEED_TO_OPTIMIZATION = "PROCEED_TO_OPTIMIZATION"


class Freshness(StrEnum):
    FRESH = "FRESH"
    AGING = "AGING"
    STALE = "STALE"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ApprovalSubjectType(StrEnum):
    CALL_INTENT = "CALL_INTENT"
    PLAN_OPTION = "PLAN_OPTION"


class CallAuthorizationState(StrEnum):
    """Authorization state of a CallIntent. Only APPROVED may ever dial."""

    NOT_REQUESTED = "NOT_REQUESTED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    BLOCKED = "BLOCKED"


class CallPattern(StrEnum):
    ONE_SHOT = "ONE_SHOT"
    FAN_OUT = "FAN_OUT"
    CASCADE = "CASCADE"
    NEGOTIATION_ROUND = "NEGOTIATION_ROUND"
    CLARIFICATION = "CLARIFICATION"
    VERIFICATION = "VERIFICATION"
    FOLLOW_UP = "FOLLOW_UP"
    ESCALATION = "ESCALATION"
    HUMAN_GATE = "HUMAN_GATE"


# --- CALL-E enums: exact vendor values -------------------------------------


class CallStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class RecipientStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class AttemptStatus(StrEnum):
    QUEUED = "queued"
    DIALING = "dialing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class WebhookEventType(StrEnum):
    CALL_COMPLETED = "call.completed"
    CALL_FAILED = "call.failed"
    CALL_RESULT_VALIDATION_FAILED = "call.result_validation_failed"


# --- Activity and scheduling ------------------------------------------------


class ActivityEventType(StrEnum):
    MISSION_STATUS_CHANGED = "MISSION_STATUS_CHANGED"
    CLARIFICATION = "CLARIFICATION"
    STRATEGY_UPDATE = "STRATEGY_UPDATE"
    AGENT_CREATED = "AGENT_CREATED"
    AGENT_STATUS_CHANGED = "AGENT_STATUS_CHANGED"
    AGENT_REQUEST = "AGENT_REQUEST"
    RESEARCH_EVENT = "RESEARCH_EVENT"
    CALL_EVENT = "CALL_EVENT"
    APPROVAL_EVENT = "APPROVAL_EVENT"
    EVIDENCE_UPDATE = "EVIDENCE_UPDATE"
    OPTIMIZER_UPDATE = "OPTIMIZER_UPDATE"
    REVIEW_RESULT = "REVIEW_RESULT"
    SCHEDULER_EVENT = "SCHEDULER_EVENT"
    MISSION_COMPLETED = "MISSION_COMPLETED"
    SYSTEM = "SYSTEM"


class ScheduledJobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    SKIPPED = "SKIPPED"
    CANCELED = "CANCELED"
    FAILED = "FAILED"
