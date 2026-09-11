"""CS-014: agent runner over the dependency graph."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import BaseModel

from callswarm.agents.output_schema import SchemaError, validate_output, validate_schema
from callswarm.agents.runner import (
    AgentRunner,
    AgentTurn,
    ToolNotPermitted,
    build_role_instruction,
)
from callswarm.agents.tools import default_tools
from callswarm.config.settings import Settings
from callswarm.events import ActivityEventEmitter
from callswarm.llm import FakeLLMProvider
from callswarm.llm.provider import TModel
from callswarm.models import (
    ActivityEvent,
    ActivityEventType,
    AgentSpec,
    AgentState,
    CallAuthorizationState,
    Mission,
    SourceType,
)
from callswarm.persistence import (
    ActivityEventRepository,
    AgentRequestRepository,
    AgentRunRepository,
    AgentSpecRepository,
    CallIntentRepository,
    Database,
    EvidenceClaimRepository,
)
from callswarm.sanitize import Sanitizer

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "finding": {"type": "string"},
        "status": {"type": "string", "enum": ["done", "unknown"]},
    },
    "required": ["finding", "status"],
    "additionalProperties": False,
}
GOOD = {"finding": "ok", "status": "done"}
TEST_PHONE = "+15550000123"

_NAME_RE = re.compile(r"^You are (.+?), a specialist")


class RoutingProvider:
    """Test double: a scripted queue per agent name, keyed off the role instruction.

    Records the maximum number of concurrent in-flight calls. Unlike the
    FakeLLMProvider it must branch on the agent, because concurrent agents
    consume responses in a nondeterministic order.
    """

    def __init__(self, scripts: Mapping[str, list[dict[str, Any]]], delay: float = 0.05) -> None:
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.started: dict[str, float] = {}
        self.finished: dict[str, float] = {}

    async def generate_structured(
        self, instruction: str, inputs: Mapping[str, str], schema: type[TModel]
    ) -> TModel:
        match = _NAME_RE.match(instruction)
        assert match, instruction[:80]
        name = match.group(1)
        self.calls.append((name, dict(inputs)))
        loop = asyncio.get_running_loop()
        self.started.setdefault(name, loop.time())
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.active -= 1
        self.finished[name] = loop.time()
        return schema.model_validate(self.scripts[name].pop(0))


def spec(
    mission_id: str, name: str, *, deps: list[str] | None = None, tools: list[str] | None = None
) -> AgentSpec:
    return AgentSpec(
        mission_id=mission_id,
        name=name,
        role="worker",
        objective=f"{name} objective",
        why_needed="needed",
        owns=f"{name} ownership",
        dependencies=deps or [],
        allowed_tools=tools if tools is not None else ["evidence.read"],
        expected_output_schema=SCHEMA,
        does_not_control=["the final decision"],
        stop_conditions=["output delivered"],
    )


async def persist(database: Database, *specs: AgentSpec) -> list[AgentSpec]:
    async with database.session() as s:
        return [await AgentSpecRepository(s).add(x) for x in specs]


def make_runner(
    database: Database, emitter: ActivityEventEmitter, llm: Any, settings: Settings
) -> AgentRunner:
    return AgentRunner(database, emitter, llm, settings, default_tools(), Sanitizer())


async def events_for(database: Database, mission_id: str) -> list[ActivityEvent]:
    async with database.session() as s:
        return await ActivityEventRepository(s).list_by_mission(mission_id)


# --- schema validator ------------------------------------------------------------


def test_schema_subset_validator() -> None:
    validate_schema(SCHEMA)
    for bad in (
        {"type": "string"},
        {"type": "object", "properties": {}},  # missing additionalProperties: false
        {"type": "object", "properties": {"a": {"$ref": "#"}}, "additionalProperties": False},
        {"type": "object", "properties": {"a": {"oneOf": []}}, "additionalProperties": False},
        {"type": "object", "properties": {"a": {"type": "array"}}, "additionalProperties": False},
        {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["b"],
            "additionalProperties": False,
        },
    ):
        with pytest.raises(SchemaError):
            validate_schema(bad)
    assert validate_output(GOOD, SCHEMA) == []
    errors = validate_output({"finding": 1, "status": "maybe", "extra": True}, SCHEMA)
    assert len(errors) == 3
    nested = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"n": {"type": "integer"}},
                    "required": ["n"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }
    validate_schema(nested)
    assert validate_output({"items": [{"n": 1}, {"n": True}]}, nested) == [
        "$.items[1].n: expected integer, got bool"
    ]


def test_role_instruction_is_built_from_spec_fields_only() -> None:
    s = spec("m", "Probe", tools=["evidence.read", "calls.request_intent"])
    text = build_role_instruction(s, default_tools())
    for fragment in (
        "You are Probe",
        s.objective,
        s.why_needed,
        s.owns,
        "the final decision",
        "output delivered",
        "- evidence.read:",
        "- calls.request_intent:",
    ):
        assert fragment in text
    assert "evidence.write_claim" not in text
    assert "untrusted data" in text and "Do not include reasoning" in text


# --- concurrency and ordering ---------------------------------------------------------


async def test_independent_agents_run_concurrently_and_dependents_wait(
    database: Database, emitter: ActivityEventEmitter, settings: Settings, mission: Mission
) -> None:
    a, b, c = await persist(
        database, spec(mission.id, "A"), spec(mission.id, "B"), spec(mission.id, "C")
    )
    c = c.model_copy(update={"dependencies": [a.id, b.id]})
    async with database.session() as s:
        c = await AgentSpecRepository(s).update(c)
    llm = RoutingProvider(
        {
            "A": [{"summary": "did A", "output": {"finding": "from A", "status": "done"}}],
            "B": [{"summary": "did B", "output": GOOD}],
            "C": [{"summary": "did C", "output": GOOD}],
        }
    )
    result = await make_runner(database, emitter, llm, settings).run_swarm(mission, [a, b, c])
    assert {k: v.status for k, v in result.runs.items()} == dict.fromkeys(
        [a.id, b.id, c.id], AgentState.COMPLETE
    )
    assert llm.max_active == 2  # A and B overlapped
    assert llm.started["C"] >= max(llm.finished["A"], llm.finished["B"])
    c_inputs = next(inputs for name, inputs in llm.calls if name == "C")
    assert '"from A"' in c_inputs["upstream artifact from A"]
    assert "upstream artifact from B" in c_inputs and "mission" in c_inputs
    run_c = result.runs[c.id]
    assert run_c.started_at is not None and run_c.completed_at is not None
    assert run_c.output_artifact == GOOD


async def test_concurrency_is_bounded_by_setting(
    database: Database, emitter: ActivityEventEmitter, mission: Mission, tmp_path: Any
) -> None:
    one = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'one.db'}",
        agent_concurrency=1,
    )
    specs = await persist(database, *(spec(mission.id, n) for n in ("A", "B", "C")))
    llm = RoutingProvider({n: [{"output": GOOD}] for n in ("A", "B", "C")})
    await make_runner(database, emitter, llm, one).run_swarm(mission, specs)
    assert llm.max_active == 1


# --- failure isolation -------------------------------------------------------------------


async def test_invalid_output_is_retried_once_then_failed_and_siblings_complete(
    database: Database, emitter: ActivityEventEmitter, settings: Settings, mission: Mission
) -> None:
    bad, good, child = await persist(
        database, spec(mission.id, "Bad"), spec(mission.id, "Good"), spec(mission.id, "Child")
    )
    async with database.session() as s:
        child = await AgentSpecRepository(s).update(
            child.model_copy(update={"dependencies": [bad.id]})
        )
    llm = RoutingProvider(
        {
            "Bad": [{"output": {"finding": "x", "status": "maybe"}}, {"output": {"finding": 3}}],
            "Good": [{"output": GOOD}],
        }
    )
    result = await make_runner(database, emitter, llm, settings).run_swarm(
        mission, [bad, good, child]
    )
    assert result.runs[bad.id].status is AgentState.FAILED
    assert "status" in (result.runs[bad.id].error or "")
    assert result.runs[good.id].status is AgentState.COMPLETE
    assert result.runs[child.id].status is AgentState.BLOCKED
    assert "Bad is FAILED" in result.runs[child.id].activity_summary
    bad_calls = [inputs for name, inputs in llm.calls if name == "Bad"]
    assert len(bad_calls) == 2
    assert "not one of" in bad_calls[1]["validation errors from your previous output"]
    async with database.session() as s:
        stored = await AgentSpecRepository(s).get(bad.id)
    assert stored is not None and stored.state is AgentState.FAILED


async def test_tool_outside_grant_fails_only_that_agent(
    database: Database, emitter: ActivityEventEmitter, settings: Settings, mission: Mission
) -> None:
    rogue, honest = await persist(
        database, spec(mission.id, "Rogue", tools=["evidence.read"]), spec(mission.id, "Honest")
    )
    llm = RoutingProvider(
        {
            "Rogue": [
                {
                    "tool_calls": [
                        {
                            "tool": "evidence.write_claim",
                            "arguments": {"subject": "s", "predicate": "p"},
                        }
                    ]
                }
            ],
            "Honest": [{"output": GOOD}],
        }
    )
    result = await make_runner(database, emitter, llm, settings).run_swarm(mission, [rogue, honest])
    assert result.runs[rogue.id].status is AgentState.FAILED
    assert result.runs[rogue.id].error == "ToolNotPermitted: evidence.write_claim"
    assert result.runs[honest.id].status is AgentState.COMPLETE
    async with database.session() as s:
        assert await EvidenceClaimRepository(s).list_by_mission(mission.id) == []
    with pytest.raises(ToolNotPermitted):
        raise ToolNotPermitted("x", "y")


async def test_unknown_or_bad_arguments_do_not_crash_the_agent(
    database: Database, emitter: ActivityEventEmitter, settings: Settings, mission: Mission
) -> None:
    (a,) = await persist(
        database, spec(mission.id, "A", tools=["research.search", "evidence.read"])
    )
    llm = RoutingProvider(
        {
            "A": [
                {
                    "tool_calls": [
                        {"tool": "research.search", "arguments": {"query": "q"}},
                        {"tool": "evidence.read", "arguments": {"bogus": 1}},
                    ]
                },
                {"output": GOOD},
            ]
        }
    )
    result = await make_runner(database, emitter, llm, settings).run_swarm(mission, [a])
    assert result.runs[a.id].status is AgentState.COMPLETE
    second = llm.calls[1][1]["tool results"]
    # No research service is wired into this runner, so the research tool is absent.
    assert "TOOL_UNAVAILABLE" in second and "INVALID_ARGUMENTS" in second


# --- tools with side effects ---------------------------------------------------------


async def test_request_agent_records_request_and_never_spawns(
    database: Database, emitter: ActivityEventEmitter, settings: Settings, mission: Mission
) -> None:
    (a,) = await persist(database, spec(mission.id, "A", tools=["orchestrator.request_agent"]))
    llm = RoutingProvider(
        {
            "A": [
                {
                    "summary": "asked for help",
                    "tool_calls": [
                        {
                            "tool": "orchestrator.request_agent",
                            "arguments": {"proposed_role": "helper", "justification": "gap"},
                        }
                    ],
                },
                {"output": GOOD},
            ]
        }
    )
    result = await make_runner(database, emitter, llm, settings).run_swarm(mission, [a])
    assert result.runs[a.id].status is AgentState.COMPLETE
    async with database.session() as s:
        requests = await AgentRequestRepository(s).list_by_mission(mission.id)
        runs = await AgentRunRepository(s).list_by_mission(mission.id)
        specs = await AgentSpecRepository(s).list_by_mission(mission.id)
    assert len(requests) == 1 and requests[0].requesting_agent_id == a.id
    assert requests[0].proposed_role == "helper" and requests[0].status.value == "PENDING"
    assert len(runs) == 1 and len(specs) == 1
    events = await events_for(database, mission.id)
    assert any(e.event_type is ActivityEventType.AGENT_REQUEST for e in events)


async def test_call_intent_request_pauses_agent_and_never_executes(
    database: Database, emitter: ActivityEventEmitter, settings: Settings, mission: Mission
) -> None:
    caller, dependent = await persist(
        database,
        spec(mission.id, "Caller", tools=["calls.request_intent"]),
        spec(mission.id, "Dep"),
    )
    async with database.session() as s:
        dependent = await AgentSpecRepository(s).update(
            dependent.model_copy(update={"dependencies": [caller.id]})
        )
    llm = RoutingProvider(
        {
            "Caller": [
                {
                    "summary": "needs a phone confirmation",
                    "tool_calls": [
                        {
                            "tool": "calls.request_intent",
                            "arguments": {
                                "recipient_phone_e164": TEST_PHONE,
                                "purpose": "confirm availability",
                                "call_goal": "ask whether the item is in stock",
                            },
                        }
                    ],
                }
            ]
        }
    )
    result = await make_runner(database, emitter, llm, settings).run_swarm(
        mission, [caller, dependent]
    )
    run = result.runs[caller.id]
    assert run.status is AgentState.WAITING_FOR_CALL
    assert run.call_intent_id is not None
    assert result.runs[dependent.id].status is AgentState.WAITING_FOR_DEPENDENCY
    async with database.session() as s:
        intent = await CallIntentRepository(s).get(run.call_intent_id)
    assert intent is not None
    assert intent.authorization_state is CallAuthorizationState.PENDING
    assert intent.recipients[0].phone_e164 == TEST_PHONE
    events = await events_for(database, mission.id)
    call_events = [e for e in events if e.event_type is ActivityEventType.CALL_EVENT]
    assert len(call_events) == 1
    assert call_events[0].payload["recipient"] == "+1 ••••• ••123"  # masked at the emitter
    assert not any(TEST_PHONE in e.summary or TEST_PHONE in str(e.payload) for e in events)


async def test_evidence_tools_write_derived_claims_only(
    database: Database, emitter: ActivityEventEmitter, settings: Settings, mission: Mission
) -> None:
    (a,) = await persist(
        database, spec(mission.id, "A", tools=["evidence.read", "evidence.write_claim"])
    )
    llm = RoutingProvider(
        {
            "A": [
                {
                    "tool_calls": [
                        {
                            "tool": "evidence.write_claim",
                            "arguments": {"subject": "thing", "predicate": "price", "value": 12},
                        },
                        {"tool": "evidence.read", "arguments": {"subject": "thing"}},
                    ]
                },
                {"output": GOOD},
            ]
        }
    )
    await make_runner(database, emitter, llm, settings).run_swarm(mission, [a])
    async with database.session() as s:
        claims = await EvidenceClaimRepository(s).list_by_mission(mission.id)
    assert len(claims) == 1 and claims[0].source_type is SourceType.DERIVED
    assert f"agent:{a.id}" in claims[0].source_reference
    assert '"source_type": "DERIVED"' in llm.calls[1][1]["tool results"]


# --- orchestrator control ---------------------------------------------------------------


async def test_stop_agent_persists_reason_and_blocks_dependents(
    database: Database, emitter: ActivityEventEmitter, settings: Settings, mission: Mission
) -> None:
    slow, child = await persist(database, spec(mission.id, "Slow"), spec(mission.id, "Child"))
    async with database.session() as s:
        child = await AgentSpecRepository(s).update(
            child.model_copy(update={"dependencies": [slow.id]})
        )
    llm = RoutingProvider({"Slow": [{"output": GOOD}]}, delay=5.0)
    runner = make_runner(database, emitter, llm, settings)
    swarm = asyncio.create_task(runner.run_swarm(mission, [slow, child]))
    for _ in range(100):
        await asyncio.sleep(0.01)
        async with database.session() as s:
            runs = await AgentRunRepository(s).list_by_agent(slow.id)
        if runs and runs[0].status is AgentState.WORKING:
            break
    else:
        pytest.fail("agent never started")
    stopped = await runner.stop_agent(runs[0], "strategy became impossible")
    assert stopped.status is AgentState.STOPPED
    assert stopped.stop_reason == "strategy became impossible"
    result = await asyncio.wait_for(swarm, timeout=2)
    assert result.runs[slow.id].status is AgentState.STOPPED
    assert result.runs[child.id].status is AgentState.BLOCKED
    async with database.session() as s:
        stored_run = await AgentRunRepository(s).get(stopped.id)
        stored_spec = await AgentSpecRepository(s).get(slow.id)
    assert stored_run is not None and stored_run.stop_reason == "strategy became impossible"
    assert stored_spec is not None and stored_spec.state is AgentState.STOPPED
    assert stored_spec.state_reason == "stopped by Orchestrator: strategy became impossible"
    with pytest.raises(ValueError, match="already"):
        await runner.stop_agent(stored_run, "again")


# --- events -----------------------------------------------------------------------------


async def test_every_transition_is_an_event_and_passes_the_sanitizer(
    database: Database, emitter: ActivityEventEmitter, settings: Settings, mission: Mission
) -> None:
    a, b = await persist(database, spec(mission.id, "A"), spec(mission.id, "B"))
    async with database.session() as s:
        b = await AgentSpecRepository(s).update(b.model_copy(update={"dependencies": [a.id]}))
    llm = RoutingProvider(
        {
            "A": [{"summary": f"call {TEST_PHONE} later", "output": GOOD}],
            "B": [{"summary": "<thinking>secret plan</thinking>", "output": GOOD}],
        }
    )
    result = await make_runner(database, emitter, llm, settings).run_swarm(mission, [a, b])
    assert result.runs[a.id].status is AgentState.COMPLETE
    # B's summary carried a reasoning leak: the sanitizer rejects it and the run fails
    # closed instead of the leak being persisted or published.
    assert result.runs[b.id].status is AgentState.FAILED
    assert result.runs[b.id].error == "ReasoningLeakError"
    events = await events_for(database, mission.id)
    sanitizer = Sanitizer(settings.reasoning_leak_markers)
    for event in events:
        sanitizer.sanitize_text(event.summary, context="t")
        sanitizer.sanitize_value(event.payload, context="t")
        assert TEST_PHONE not in event.summary
    status_events = [e for e in events if e.event_type is ActivityEventType.AGENT_STATUS_CHANGED]
    seen = {(e.agent_id, e.payload["status"]) for e in status_events}
    for state in (AgentState.CREATED, AgentState.READY, AgentState.WORKING, AgentState.COMPLETE):
        assert (a.id, state.value) in seen
    for state in (
        AgentState.CREATED,
        AgentState.WAITING_FOR_DEPENDENCY,
        AgentState.READY,
        AgentState.WORKING,
        AgentState.FAILED,
    ):
        assert (b.id, state.value) in seen
    assert any("+1 ••••• ••123" in e.summary for e in status_events)
    assert not any("secret plan" in e.summary for e in events)


def test_agent_turn_schema_rejects_extra_fields() -> None:
    with pytest.raises(ValueError):
        AgentTurn.model_validate({"output": GOOD, "reasoning": "hidden"})
    assert isinstance(AgentTurn(output=GOOD), BaseModel)


async def test_runner_never_uses_fake_provider_branching(fake_llm: FakeLLMProvider) -> None:
    """Documentation guard: FakeLLMProvider stays queue-only; the routing double is test-local."""
    assert not hasattr(fake_llm, "scripts")
