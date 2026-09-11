"""Tools a generated agent may call, and their Phase 2 implementations.

The set of names is fixed by ``ALLOWED_TOOLS`` in the factory; the runner
grants an agent exactly ``AgentSpec.allowed_tools``. In this phase:

* ``evidence.read`` / ``evidence.write_claim`` work against the repositories
  (agent-written claims are always ``DERIVED`` — an agent cannot mint a PHONE
  or WEB claim);
* ``research.*`` return an explicit ``NOT_AVAILABLE_IN_THIS_PHASE`` result
  (CS-020 replaces them); nothing touches the network;
* ``calls.request_intent`` persists a ``CallIntent`` in ``PENDING``
  authorization and never executes anything;
* ``orchestrator.request_agent`` persists an ``AgentRequest``; only the
  Orchestrator may act on it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from callswarm.events import ActivityEventEmitter
from callswarm.models import (
    ActivityEvent,
    ActivityEventType,
    AgentRequest,
    AgentSpec,
    CallAuthorizationState,
    CallIntent,
    CallRecipient,
    EvidenceClaim,
    EvidenceStatus,
    JsonValue,
    SourceType,
)
from callswarm.persistence import (
    AgentRequestRepository,
    CallIntentRepository,
    Database,
    EvidenceClaimRepository,
)

NOT_AVAILABLE = "NOT_AVAILABLE_IN_THIS_PHASE"


@dataclass(frozen=True)
class ToolContext:
    mission_id: str
    agent: AgentSpec
    run_id: str
    database: Database
    emitter: ActivityEventEmitter


class Tool(Protocol):
    name: str
    description: str

    async def __call__(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]: ...


class ToolArgumentError(ValueError):
    """The agent supplied arguments the tool cannot accept."""


def _parse(model: type[BaseModel], arguments: dict[str, Any]) -> Any:
    try:
        return model.model_validate(arguments)
    except ValidationError as exc:
        messages = [
            f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', 'invalid')}"
            for e in exc.errors(include_url=False, include_input=False)
        ]
        raise ToolArgumentError("; ".join(messages)) from None


# --- research (stubbed) ------------------------------------------------------------


class _NotAvailableTool:
    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description

    async def __call__(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": NOT_AVAILABLE,
            "tool": self.name,
            "detail": "Research tools are not available yet; record the gap instead.",
        }


# --- evidence -----------------------------------------------------------------------


class _ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str | None = None
    predicate: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class EvidenceReadTool:
    name = "evidence.read"
    description = (
        "Read evidence claims for this mission. Arguments: subject (optional), predicate "
        "(optional), limit (optional). Returns claims with their source type and status."
    )

    async def __call__(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        args = _parse(_ReadArgs, arguments)
        async with context.database.session() as session:
            claims = await EvidenceClaimRepository(session).list_by_mission(context.mission_id)
        selected = [
            c
            for c in claims
            if (args.subject is None or c.subject.lower() == args.subject.lower())
            and (args.predicate is None or c.predicate.lower() == args.predicate.lower())
        ][: args.limit]
        return {
            "claims": [
                {
                    "id": c.id,
                    "subject": c.subject,
                    "predicate": c.predicate,
                    "value": c.value,
                    "source_type": c.source_type.value,
                    "evidence_status": c.evidence_status.value,
                    "freshness": c.freshness.value,
                }
                for c in selected
            ]
        }


class _WriteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    value: JsonValue = None
    source_reference: str = ""
    entity_id: str | None = None


class EvidenceWriteClaimTool:
    name = "evidence.write_claim"
    description = (
        "Record a derived claim. Arguments: subject, predicate, value, source_reference "
        "(what it was derived from). Claims written by agents are always marked DERIVED."
    )

    async def __call__(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        args = _parse(_WriteArgs, arguments)
        claim = EvidenceClaim(
            mission_id=context.mission_id,
            subject=args.subject,
            predicate=args.predicate,
            value=args.value,
            source_type=SourceType.DERIVED,
            source_reference=f"agent:{context.agent.id};run:{context.run_id};{args.source_reference}",
            evidence_status=EvidenceStatus.UNKNOWN,
            entity_id=args.entity_id,
        )
        async with context.database.session() as session:
            stored = await EvidenceClaimRepository(session).add(claim)
        return {"claim_id": stored.id, "source_type": stored.source_type.value}


# --- calls ----------------------------------------------------------------------------


class _IntentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient_phone_e164: str = Field(pattern=r"^\+[1-9]\d{6,14}$")
    recipient_entity_id: str | None = None
    purpose: str = Field(min_length=1)
    call_goal: str = Field(min_length=1)
    expected_decision_impact: str = ""
    information_gaps: list[str] = Field(default_factory=list)


class CallRequestIntentTool:
    name = "calls.request_intent"
    description = (
        "Request that a call be considered. Arguments: recipient_phone_e164, purpose, "
        "call_goal, expected_decision_impact, information_gaps. This never places a call: it "
        "records an intent that requires authorization. Your run pauses until a result exists."
    )

    async def __call__(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        args = _parse(_IntentArgs, arguments)
        intent = CallIntent(
            mission_id=context.mission_id,
            recipients=[
                CallRecipient(
                    phone_e164=args.recipient_phone_e164, entity_id=args.recipient_entity_id
                )
            ],
            purpose=args.purpose,
            call_goal=args.call_goal,
            expected_decision_impact=args.expected_decision_impact,
            information_gaps=list(args.information_gaps),
            authorization_state=CallAuthorizationState.PENDING,
        )
        async with context.database.session() as session:
            stored = await CallIntentRepository(session).add(intent)
        await context.emitter.emit(
            ActivityEvent(
                mission_id=context.mission_id,
                event_type=ActivityEventType.CALL_EVENT,
                summary=f"{context.agent.name} requested a call intent: {args.purpose}",
                agent_id=context.agent.id,
                payload={
                    "call_intent_id": stored.id,
                    "authorization_state": stored.authorization_state.value,
                    "recipient": args.recipient_phone_e164,  # masked by the emitter
                },
            )
        )
        return {
            "call_intent_id": stored.id,
            "authorization_state": stored.authorization_state.value,
        }


# --- orchestrator -----------------------------------------------------------------------


class _RequestAgentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposed_role: str = Field(min_length=1)
    justification: str = Field(min_length=1)
    required_inputs: list[str] = Field(default_factory=list)


class OrchestratorRequestAgentTool:
    name = "orchestrator.request_agent"
    description = (
        "Ask the Orchestrator to consider creating another specialist. Arguments: "
        "proposed_role, justification, required_inputs. You cannot create agents; the "
        "Orchestrator decides."
    )

    async def __call__(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        args = _parse(_RequestAgentArgs, arguments)
        request = AgentRequest(
            mission_id=context.mission_id,
            requesting_agent_id=context.agent.id,
            proposed_role=args.proposed_role,
            justification=args.justification,
            required_inputs=list(args.required_inputs),
        )
        async with context.database.session() as session:
            stored = await AgentRequestRepository(session).add(request)
        await context.emitter.emit(
            ActivityEvent(
                mission_id=context.mission_id,
                event_type=ActivityEventType.AGENT_REQUEST,
                summary=f"{context.agent.name} requested a new specialist: {args.proposed_role}",
                agent_id=context.agent.id,
                payload={"request_id": stored.id, "status": stored.status.value},
            )
        )
        return {"request_id": stored.id, "status": stored.status.value}


def default_tools() -> dict[str, Tool]:
    tools: list[Tool] = [
        _NotAvailableTool(
            "research.search",
            "Search public sources. Arguments: query. (Not available in this phase.)",
        ),
        _NotAvailableTool(
            "research.fetch_public_page",
            "Fetch a public page. Arguments: url. (Not available in this phase.)",
        ),
        EvidenceReadTool(),
        EvidenceWriteClaimTool(),
        CallRequestIntentTool(),
        OrchestratorRequestAgentTool(),
    ]
    return {tool.name: tool for tool in tools}


def describe_tools(names: list[str], registry: Mapping[str, Tool]) -> list[str]:
    lines: list[str] = []
    for name in names:
        tool = registry.get(name)
        lines.append(f"- {name}: {tool.description if tool else 'no description'}")
    return lines
