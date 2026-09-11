"""Deterministic call priority and selection (CS-030).

The model estimates :class:`CallValueFactors` per candidate intent; **this
module computes the priority**. ``compute_priority`` is a pure function of the
factors and the configured weights, so a score is reproducible from its
factors and the model never emits one.

    priority = clamp01( sum(w_i * positive_factor_i) - w_r * redundancy - w_c * call_cost )

Default weights (``config/settings.py``): mission_impact 0.25, uncertainty
0.15, time_sensitivity 0.10, expected_value 0.20, strategy_change_potential
0.15, evidence_importance 0.15 (positive weights sum to 1.0, so the positive
term lies in [0, 1]); redundancy penalty 0.50, call-cost penalty 0.30.

``select_calls`` then applies, in order and with a recorded reason for every
rejection: hard blocks (policy forbids calls, prohibited purpose, intent
already ``BLOCKED``) regardless of score; redundancy ≥ ``REDUNDANT_THRESHOLD``
("all gaps already resolved"); the minimum priority; and the call budget
after sorting by priority. Ties break on intent id so runs are reproducible.
"""

from __future__ import annotations

from pydantic import Field

from callswarm.config.settings import Settings
from callswarm.models import (
    AuthorityPolicy,
    CallAuthorizationState,
    CallBudget,
    CallIntent,
    CallValueFactors,
    DomainModel,
)

REDUNDANT_THRESHOLD = 0.9
DEFAULT_MIN_PRIORITY = 0.35


class PriorityWeights(DomainModel):
    mission_impact: float = Field(default=0.25, ge=0.0)
    uncertainty: float = Field(default=0.15, ge=0.0)
    time_sensitivity: float = Field(default=0.10, ge=0.0)
    expected_value: float = Field(default=0.20, ge=0.0)
    strategy_change_potential: float = Field(default=0.15, ge=0.0)
    evidence_importance: float = Field(default=0.15, ge=0.0)
    redundancy: float = Field(default=0.50, ge=0.0)
    call_cost: float = Field(default=0.30, ge=0.0)

    @classmethod
    def from_settings(cls, settings: Settings) -> PriorityWeights:
        return cls(
            mission_impact=settings.call_weight_mission_impact,
            uncertainty=settings.call_weight_uncertainty,
            time_sensitivity=settings.call_weight_time_sensitivity,
            expected_value=settings.call_weight_expected_value,
            strategy_change_potential=settings.call_weight_strategy_change_potential,
            evidence_importance=settings.call_weight_evidence_importance,
            redundancy=settings.call_weight_redundancy,
            call_cost=settings.call_weight_call_cost,
        )


def compute_priority(factors: CallValueFactors, weights: PriorityWeights) -> float:
    """Weighted positives minus weighted penalties, clamped to [0, 1]. Pure."""
    positive = (
        weights.mission_impact * factors.mission_impact
        + weights.uncertainty * factors.uncertainty
        + weights.time_sensitivity * factors.time_sensitivity
        + weights.expected_value * factors.expected_value
        + weights.strategy_change_potential * factors.strategy_change_potential
        + weights.evidence_importance * factors.evidence_importance
    )
    penalty = weights.redundancy * factors.redundancy + weights.call_cost * factors.call_cost
    return round(max(0.0, min(1.0, positive - penalty)), 6)


class RejectedIntent(DomainModel):
    intent_id: str
    reason: str = Field(min_length=1)
    priority_score: float | None = None


class CallSelection(DomainModel):
    selected: list[CallIntent] = Field(default_factory=list)
    rejected: list[RejectedIntent] = Field(default_factory=list)

    @property
    def considered(self) -> int:
        return len(self.selected) + len(self.rejected)


def prohibited_purpose(intent: CallIntent, purposes: list[str]) -> str | None:
    haystack = f"{intent.purpose} {intent.call_goal} {intent.expected_decision_impact}".lower()
    for purpose in purposes:
        needle = purpose.strip().lower()
        if needle and needle in haystack:
            return purpose
    return None


def _block_reason(
    intent: CallIntent, policy: AuthorityPolicy, prohibited_purposes: list[str]
) -> str | None:
    if intent.authorization_state is CallAuthorizationState.BLOCKED:
        return "blocked: intent is BLOCKED" + (
            f" ({intent.rejection_reason})" if intent.rejection_reason else ""
        )
    if not policy.calls_allowed:
        return "blocked: mission authority policy does not allow calls"
    purpose = prohibited_purpose(intent, prohibited_purposes)
    if purpose is not None:
        return f"blocked: prohibited purpose ({purpose})"
    return None


def select_calls(
    intents: list[CallIntent],
    budget: CallBudget,
    policy: AuthorityPolicy,
    *,
    weights: PriorityWeights | None = None,
    min_priority: float = DEFAULT_MIN_PRIORITY,
    prohibited_purposes: list[str] | None = None,
    hard_cap: int | None = None,
) -> CallSelection:
    """Score every intent (its ``priority_factors`` must be set) and pick the
    ones worth making. Every intent lands in exactly one of ``selected`` or
    ``rejected``; selected intents carry their ``priority_score``."""
    w = weights or PriorityWeights()
    purposes = prohibited_purposes or []
    cap = min(budget.remaining, policy.max_call_count)
    if hard_cap is not None:
        cap = min(cap, hard_cap)

    rejected: list[RejectedIntent] = []
    scored: list[CallIntent] = []
    for intent in intents:
        block = _block_reason(intent, policy, purposes)
        if intent.priority_factors is None:
            score: float | None = None
        else:
            score = compute_priority(intent.priority_factors, w)
        if block is not None:
            rejected.append(RejectedIntent(intent_id=intent.id, reason=block, priority_score=score))
            continue
        if intent.priority_factors is None or score is None:
            rejected.append(
                RejectedIntent(intent_id=intent.id, reason="no priority factors were estimated")
            )
            continue
        if intent.priority_factors.redundancy >= REDUNDANT_THRESHOLD:
            rejected.append(
                RejectedIntent(
                    intent_id=intent.id,
                    reason=(
                        f"redundant: information already known (redundancy "
                        f"{intent.priority_factors.redundancy:.2f} >= {REDUNDANT_THRESHOLD})"
                    ),
                    priority_score=score,
                )
            )
            continue
        if score < min_priority:
            rejected.append(
                RejectedIntent(
                    intent_id=intent.id,
                    reason=f"below minimum priority ({score:.2f} < {min_priority:.2f})",
                    priority_score=score,
                )
            )
            continue
        scored.append(intent.model_copy(update={"priority_score": score}))

    scored.sort(key=lambda i: (-(i.priority_score or 0.0), i.id))
    selected = scored[:cap]
    for intent in scored[cap:]:
        rejected.append(
            RejectedIntent(
                intent_id=intent.id,
                reason=f"over call budget (cap {cap}; priority {intent.priority_score:.2f})",
                priority_score=intent.priority_score,
            )
        )
    return CallSelection(selected=selected, rejected=rejected)
