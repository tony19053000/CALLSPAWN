"""Incremental constraint updates (CS-042): change the spec, keep the evidence.

A ``ConstraintChange`` is typed and structured — updates keyed by
``(key, operator)``, component locks and unlocks, soft-preference weight
changes. No free text reaches this module; the chat layer translates.

``RevisionService.apply`` updates the ``MissionSpec`` deterministically, then
builds an **artifact dependency graph** from what the artifacts themselves
declare — ``StrategyCandidate.required_information``,
``AgentSpec.required_inputs``/``owns`` and ``PlanOption.constraint_keys`` and
``components`` — and marks stale *only* the artifacts that depend on a key
that changed. Research artifacts, evidence claims, call runs and candidate
facts are never touched: an answered question is not asked again, which also
protects the call budget. Agents owning stale outputs are re-queued
``READY``; an agent that owns a locked component is left alone.

Transitions: ``USER_DECISION_PENDING | PLAN_OPTIONS_READY →
MISSION_REVISION_RUNNING → REPLAN_DECISION_RUNNING``. The replan engine then
decides how to proceed with a ``CONSTRAINT_CHANGE`` trigger.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from pydantic import Field

from callswarm.events import ActivityEventEmitter
from callswarm.evidence.normalize import normalize_key
from callswarm.models import (
    ActivityEvent,
    ActivityEventType,
    AgentRun,
    AgentSpec,
    AgentState,
    ConstraintChange,
    DomainModel,
    HardConstraint,
    Mission,
    MissionSpec,
    MissionStatus,
    PlanOption,
    SoftPreference,
    StrategyCandidate,
    utcnow,
)
from callswarm.orchestrator.state_machine import MissionStateMachine
from callswarm.persistence import (
    AgentRunRepository,
    AgentSpecRepository,
    CallRunRepository,
    Database,
    EvidenceClaimRepository,
    MissionRepository,
    PlanOptionRepository,
    StrategyCandidateRepository,
)

logger = logging.getLogger(__name__)

REVISABLE_STATES: frozenset[MissionStatus] = frozenset(
    {MissionStatus.USER_DECISION_PENDING, MissionStatus.PLAN_OPTIONS_READY}
)

# Agents in these states are not re-queued: STOPPED was an Orchestrator
# decision with a reason; WORKING/READY are already in flight.
NOT_REQUEUED: frozenset[AgentState] = frozenset(
    {AgentState.STOPPED, AgentState.WORKING, AgentState.READY}
)


class RevisionNotAllowedError(Exception):
    def __init__(self, mission_id: str, status: MissionStatus, detail: str) -> None:
        self.mission_id = mission_id
        self.status = status
        self.detail = detail
        super().__init__(f"mission {mission_id}: {detail}")


# --- dependency graph -------------------------------------------------------------------


def key_matches(key: str, text: str) -> bool:
    """``key`` is a dependency of ``text`` when its normalized tokens appear as
    a contiguous run in the text's normalized tokens."""
    needle = [t for t in normalize_key(key).split("_") if t]
    haystack = [t for t in normalize_key(text).split("_") if t]
    if not needle or not haystack:
        return False
    return any(haystack[i : i + len(needle)] == needle for i in range(len(haystack)))


def depends_on(keys: Iterable[str], texts: Iterable[str]) -> list[str]:
    texts = list(texts)
    return [key for key in keys if any(key_matches(key, text) for text in texts)]


class ArtifactDependencyGraph(DomainModel):
    """Which derived artifact depends on which constraint or component key."""

    constraint_keys: list[str] = Field(default_factory=list)
    strategies: dict[str, list[str]] = Field(default_factory=dict)
    agents: dict[str, list[str]] = Field(default_factory=dict)
    plan_options: dict[str, list[str]] = Field(default_factory=dict)
    component_owners: dict[str, list[str]] = Field(
        default_factory=dict, description="component key -> agent ids owning it"
    )

    def dependents_of(self, changed: Iterable[str]) -> tuple[set[str], set[str], set[str]]:
        wanted = {normalize_key(k) for k in changed}

        def hit(deps: list[str]) -> bool:
            return any(normalize_key(d) in wanted for d in deps)

        return (
            {sid for sid, deps in self.strategies.items() if hit(deps)},
            {aid for aid, deps in self.agents.items() if hit(deps)},
            {pid for pid, deps in self.plan_options.items() if hit(deps)},
        )


def build_dependency_graph(
    spec: MissionSpec,
    strategies: Sequence[StrategyCandidate],
    agents: Sequence[AgentSpec],
    plan_options: Sequence[PlanOption],
    *,
    component_keys: Iterable[str] = (),
) -> ArtifactDependencyGraph:
    keys = [c.key for c in spec.hard_constraints] + [p.key for p in spec.soft_preferences]
    hard_keys = [c.key for c in spec.hard_constraints]
    components = {normalize_key(k) for k in component_keys if normalize_key(k)}
    for plan in plan_options:
        components.update(normalize_key(c.name) for c in plan.components if normalize_key(c.name))
    graph = ArtifactDependencyGraph(constraint_keys=keys)
    for strategy in strategies:
        graph.strategies[strategy.id] = depends_on(keys, strategy.required_information)
    for agent in agents:
        graph.agents[agent.id] = depends_on(keys, [*agent.required_inputs, agent.owns])
        for component in components:
            if key_matches(component, agent.owns) or key_matches(component, agent.name):
                graph.component_owners.setdefault(component, []).append(agent.id)
    for plan in plan_options:
        declared = plan.constraint_keys or hard_keys  # unknown → every hard constraint
        graph.plan_options[plan.id] = list(declared) + [
            normalize_key(c.name) for c in plan.components if normalize_key(c.name)
        ]
    return graph


# --- spec update ----------------------------------------------------------------------


def apply_change_to_spec(
    spec: MissionSpec, change: ConstraintChange
) -> tuple[MissionSpec, list[str]]:
    """Deterministic spec update. Returns the new spec and the keys whose
    effective value changed (new constraints, changed values, changed
    preference weights). Locks and unlocks never count as a changed key."""
    changed: list[str] = []
    constraints: dict[tuple[str, str], HardConstraint] = {
        (normalize_key(c.key), c.operator.value): c for c in spec.hard_constraints
    }
    for update in change.updates:
        slot = (normalize_key(update.key), update.operator.value)
        current = constraints.get(slot)
        if current is not None and current.value == update.value:
            continue
        constraints[slot] = HardConstraint(
            key=current.key if current is not None else update.key,
            operator=update.operator,
            value=update.value,
            description=current.description if current is not None else "",
            locked=True,  # stated by the user, not inferred by the model
        )
        changed.append(update.key)
    preferences = list(spec.soft_preferences)
    by_key = {normalize_key(p.key): i for i, p in enumerate(preferences)}
    for key, weight in change.preference_changes.items():
        index = by_key.get(normalize_key(key))
        if index is None:
            preferences.append(SoftPreference(key=key, weight=weight))
            changed.append(key)
        elif preferences[index].weight != weight:
            preferences[index] = preferences[index].model_copy(update={"weight": weight})
            changed.append(key)
    locked = [k for k in spec.locked_components if normalize_key(k) not in _norm(change.unlocks)]
    for key in change.locks:
        if normalize_key(key) and normalize_key(key) not in _norm(locked):
            locked.append(key)
    return (
        spec.model_copy(
            update={
                "hard_constraints": list(constraints.values()),
                "soft_preferences": preferences,
                "locked_components": locked,
            }
        ),
        changed,
    )


def _norm(keys: Iterable[str]) -> set[str]:
    return {normalize_key(k) for k in keys}


# --- service --------------------------------------------------------------------------


class RevisionResult(DomainModel):
    mission_id: str
    status: MissionStatus
    changed_keys: list[str] = Field(default_factory=list)
    locked_components: list[str] = Field(default_factory=list)
    stale_strategy_ids: list[str] = Field(default_factory=list)
    stale_plan_option_ids: list[str] = Field(default_factory=list)
    stale_run_ids: list[str] = Field(default_factory=list)
    requeued_agent_ids: list[str] = Field(default_factory=list)
    untouched_agent_ids: list[str] = Field(default_factory=list)
    preserved_claims: int = 0
    preserved_call_runs: int = 0

    @property
    def stale_artifact_count(self) -> int:
        return (
            len(self.stale_strategy_ids) + len(self.stale_plan_option_ids) + len(self.stale_run_ids)
        )


class RevisionService:
    def __init__(
        self, database: Database, emitter: ActivityEventEmitter, state_machine: MissionStateMachine
    ) -> None:
        self._database = database
        self._emitter = emitter
        self._machine = state_machine

    async def apply(self, mission: Mission, change: ConstraintChange) -> RevisionResult:
        async with self._database.session() as session:
            stored = await MissionRepository(session).get(mission.id)
            if stored is None:
                raise KeyError(f"mission {mission.id!r} not found")
            if stored.status not in REVISABLE_STATES:
                raise RevisionNotAllowedError(
                    stored.id,
                    stored.status,
                    f"mission is {stored.status.value}; constraints can be revised only in "
                    f"{', '.join(sorted(s.value for s in REVISABLE_STATES))}",
                )
            if stored.spec is None:
                raise RevisionNotAllowedError(stored.id, stored.status, "mission has no spec")
            claims_before = len(await EvidenceClaimRepository(session).list_by_mission(stored.id))
            runs_before = len(await CallRunRepository(session).list_by_mission(stored.id))
            strategies = await StrategyCandidateRepository(session).list_by_mission(stored.id)
            agents = await AgentSpecRepository(session).list_by_mission(stored.id)
            runs = await AgentRunRepository(session).list_by_mission(stored.id)
            plans = await PlanOptionRepository(session).list_by_mission(stored.id)

        mission = await self._machine.propose_transition(
            stored, MissionStatus.MISSION_REVISION_RUNNING, "user constraint update"
        )
        new_spec, changed = apply_change_to_spec(stored.spec, change)
        graph = build_dependency_graph(
            new_spec, strategies, agents, plans, component_keys=new_spec.locked_components
        )
        stale_strategies, dependent_agents, stale_plans = graph.dependents_of(changed)
        locked_owner_ids = {
            aid
            for component in _norm(new_spec.locked_components)
            for aid in graph.component_owners.get(component, [])
        }
        reason = "constraint change: " + ", ".join(changed) if changed else "constraint change"
        latest_run = _latest_runs(runs)
        result = RevisionResult(
            mission_id=mission.id,
            status=mission.status,
            changed_keys=changed,
            locked_components=list(new_spec.locked_components),
        )
        now = utcnow()
        async with self._database.session() as session:
            missions = MissionRepository(session)
            await missions.update(
                mission.model_copy(
                    update={
                        "spec": new_spec,
                        "hard_constraints": list(new_spec.hard_constraints),
                        "soft_preferences": list(new_spec.soft_preferences),
                        "updated_at": now,
                    }
                )
            )
            strategy_repo = StrategyCandidateRepository(session)
            for strategy in strategies:
                if strategy.id in stale_strategies and not strategy.stale:
                    await strategy_repo.update(
                        strategy.model_copy(
                            update={"stale": True, "stale_reason": reason, "updated_at": now}
                        )
                    )
                    result.stale_strategy_ids.append(strategy.id)
            plan_repo = PlanOptionRepository(session)
            locked = _norm(new_spec.locked_components)
            for plan in plans:
                update: dict[str, object] = {}
                components = [
                    c.model_copy(update={"locked": normalize_key(c.name) in locked})
                    for c in plan.components
                ]
                if components != plan.components:
                    update["components"] = components
                if plan.id in stale_plans and not plan.stale:
                    update.update({"stale": True, "stale_reason": reason})
                    result.stale_plan_option_ids.append(plan.id)
                if update:
                    await plan_repo.update(plan.model_copy(update=update))
            spec_repo = AgentSpecRepository(session)
            run_repo = AgentRunRepository(session)
            for agent in agents:
                if agent.id not in dependent_agents:
                    result.untouched_agent_ids.append(agent.id)
                    continue
                if agent.id in locked_owner_ids:
                    # Owns a locked component: its output must not change.
                    result.untouched_agent_ids.append(agent.id)
                    continue
                run = latest_run.get(agent.id)
                if run is not None and run.output_artifact is not None and not run.stale:
                    await run_repo.update(
                        run.model_copy(update={"stale": True, "stale_reason": reason})
                    )
                    result.stale_run_ids.append(run.id)
                if agent.state in NOT_REQUEUED:
                    result.untouched_agent_ids.append(agent.id)
                    continue
                await spec_repo.update(
                    agent.model_copy(
                        update={
                            "state": AgentState.READY,
                            "state_reason": f"re-queued: {reason}",
                            "updated_at": now,
                        }
                    )
                )
                result.requeued_agent_ids.append(agent.id)
            claims_after = len(await EvidenceClaimRepository(session).list_by_mission(mission.id))
            runs_after = len(await CallRunRepository(session).list_by_mission(mission.id))
        assert claims_after == claims_before and runs_after == runs_before
        result.preserved_claims = claims_after
        result.preserved_call_runs = runs_after
        for agent_id in result.requeued_agent_ids:
            agent = next(a for a in agents if a.id == agent_id)
            await self._emitter.emit(
                ActivityEvent(
                    mission_id=mission.id,
                    event_type=ActivityEventType.AGENT_STATUS_CHANGED,
                    summary=f"{agent.name} is READY: re-queued after {reason}",
                    agent_id=agent.id,
                    payload={"status": AgentState.READY.value, "reason": reason},
                )
            )
        mission = await self._machine.propose_transition(
            mission, MissionStatus.REPLAN_DECISION_RUNNING, "constraints revised"
        )
        result.status = mission.status
        await self._emitter.emit(
            ActivityEvent(
                mission_id=mission.id,
                event_type=ActivityEventType.SYSTEM,
                summary=(
                    f"constraints revised: {result.stale_artifact_count} derived artifacts marked "
                    f"stale, {len(result.requeued_agent_ids)} agents re-queued, evidence preserved "
                    f"({result.preserved_claims} claims)"
                ),
                payload={
                    "changed_keys": changed,
                    "locked_components": list(new_spec.locked_components),
                    "stale_strategy_ids": result.stale_strategy_ids,
                    "stale_plan_option_ids": result.stale_plan_option_ids,
                    "stale_run_ids": result.stale_run_ids,
                    "requeued_agent_ids": result.requeued_agent_ids,
                    "preserved_claims": result.preserved_claims,
                    "preserved_call_runs": result.preserved_call_runs,
                },
            )
        )
        return result


def _latest_runs(runs: Sequence[AgentRun]) -> dict[str, AgentRun]:
    latest: dict[str, AgentRun] = {}
    for run in runs:
        current = latest.get(run.agent_id)
        if current is None or (run.started_at or utcnow()) >= (current.started_at or utcnow()):
            latest[run.agent_id] = run
    return latest
