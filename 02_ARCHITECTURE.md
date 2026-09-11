# 02 — Technical Architecture

## Design stance

Practical architecture, no enterprise ceremony. Three rules govern every decision below.

1. **The model proposes, code decides.** LLM output is always a validated structured artifact. Mission state transitions, constraint checks and scoring are deterministic application code.
2. **Structured contracts, not group chat.** Agents never free-chat with each other. They exchange validated artifacts through the Orchestrator.
3. **Nothing is real until it has provenance.** Every consequential fact traces to a research artifact or a call run.

## Stack

**Backend / agent runtime — Python 3.11+**
FastAPI, Pydantic v2, SQLAlchemy 2.x (async) with SQLite locally and PostgreSQL-compatible schema, `pytest` + `pytest-asyncio`, `ruff`, `mypy`.

Python owns mission orchestration, agent execution, the research abstraction, CALL-E integration, evidence processing, optimization, policy enforcement, scheduling and persistence.

**Frontend — Next.js (App Router), React, TypeScript, Tailwind CSS.**
The frontend holds no LLM, CALL-E or search credentials. It reads REST endpoints and subscribes to a server-sent event stream.

**Reasoning provider — Gemini, behind an `LLMProvider` interface.**
Initial implementation `GeminiProvider` using the official Google GenAI Python SDK (`google-genai`, `from google import genai`). Model comes from `GEMINI_MODEL`; no model id is hardwired in code. Both auth paths are supported: `GEMINI_API_KEY`, or Vertex AI via `GOOGLE_GENAI_USE_VERTEXAI=true` with Application Default Credentials. Every provider call requests a response conforming to a Pydantic schema and validates the result before use; a schema violation is a recoverable agent error, never silently accepted text.

## Repository layout

```text
/
├── .claude/agents/{coder.md,reviewer-tester.md}
├── apps/web/                     # Next.js workspace UI
├── backend/callswarm/
│   ├── api/                      # FastAPI routes + SSE stream
│   ├── orchestrator/             # Main Orchestrator, mission state machine
│   ├── agents/                   # agent factory, runner, contracts
│   ├── strategies/               # strategy generation + pruning
│   ├── research/                 # ResearchProvider abstraction + providers
│   ├── calls/                    # CallExecutionProvider, intents, scoring, patterns
│   ├── evidence/                 # Reality Graph, claim normalization, conflicts
│   ├── optimizer/                # constraint enforcement + plan assembly
│   ├── approvals/                # authority policy, approval records
│   ├── scheduler/                # persisted jobs (follow-ups, retries)
│   ├── persistence/              # SQLAlchemy models, repositories, migrations
│   ├── models/                   # Pydantic domain models (shared contracts)
│   ├── llm/                      # LLMProvider + GeminiProvider
│   ├── config/                   # typed settings, capability report
│   └── events/                   # ActivityEvent bus
├── scenarios/{anniversary,pc-build,professional-services,web-leads,simple}/
├── tests/
└── anchor docs, STATUS.md, CLAUDE.md, README.md, .env.example, .gitignore
```

No folder exists without code in it.

## Core data models

All models are Pydantic and persisted through SQLAlchemy repositories.

```text
Mission
- id, user_goal, status, created_at, updated_at
- authority_policy, call_budget
- hard_constraints, soft_preferences, priority_weights

MissionSpec              validated representation of what the user actually wants

StrategyCandidate
- id, title, description, assumptions, benefits, drawbacks
- required_information, expected_dependencies, status

AgentSpec
- id, mission_id, name, role, objective, why_needed
- allowed_tools, required_inputs, dependencies
- expected_output_schema, stop_conditions, risk_level

AgentRun
- agent_id, status, started_at, completed_at
- activity_summary, output_artifact, error

ResearchArtifact
- source, source_type, retrieved_at, entity_ref
- extracted_claims, provenance

CandidateEntity          generic: vendor, venue, caterer, seller, firm, prospect
- id, mission_id, kind, display_name, attributes, contact (masked), source_refs

InformationGap
- question, affected_decision, importance
- current_confidence, possible_resolution_methods

CallIntent
- recipient, purpose, information_gaps, expected_decision_impact
- priority_score, call_pattern, authorization_state, call_goal, result_schema

CallRun
- calle_call_id, status, recipient_masked, started_at, completed_at
- structured_result, recipient_results[], transcript_reference, evidence, confidence
- is_simulated
  NOTE: the CALL-E spec's own `provider_call_id` is attempt-level telephony data, not the
  call task id. Our field is named `calle_call_id` to prevent that mis-mapping.

RecipientResult          one entry per recipient of a fan-out call
- recipient_ref, phone_masked, status (RecipientStatus), structured_result, summary

AgentRequest             a specialist's request that a new specialist be created
- requesting_agent_id, proposed_role, justification, required_inputs, status

SuppressionEntry         do-not-contact record; mission-independent and never cascade-deleted
- phone_hash, reason, source, scope, created_at

EvidenceClaim
- subject, predicate, value, source_type, source_reference
- timestamp, freshness, evidence_status, conflicts

  source_type  WEB | PHONE | USER | DERIVED | FIXTURE | SIMULATED
  FIXTURE marks data from FixtureResearchProvider; SIMULATED marks any result produced by
  FakeCallProvider. Both propagate to every derived PlanOption and are rendered as a
  persistent badge in the UI. A simulated claim can never be displayed as a real one.

PlanOption
- name, components, total_cost, hard_constraints_passed, soft_score
- uncertainties, evidence_summary, tradeoffs

Approval                 PENDING | APPROVED | REJECTED | EXPIRED
ActivityEvent            high-level, user-visible execution event
```

`CandidateEntity` is deliberately generic — `kind` plus an open `attributes` map. No domain schema is baked in.

## Runtime agent model

A runtime agent is: **LLM provider + generated role instruction + bounded tool set + validated output schema.**

Agents are data (`AgentSpec` rows), not Python classes. The agent factory turns an `AgentSpec` into a runnable unit; the runner executes it with only its `allowed_tools` and validates the result against `expected_output_schema`.

Lifecycle states:

```text
CREATED → WAITING → READY → WORKING → { BLOCKED, WAITING_FOR_DEPENDENCY,
WAITING_FOR_CALL, REVIEWING } → COMPLETE | STOPPED | FAILED
```

Only the Main Orchestrator may spawn, pause, stop, replace, merge or restart an agent. A specialist may *request* that another specialist is created; it cannot create one itself in V1.

## Mission state machine

Deterministic, implemented in `orchestrator/state_machine.py`. The model proposes a next action; code validates the proposal against allowed transitions and applies it.

```text
MISSION_CREATED → GOAL_UNDERSTANDING → [CLARIFICATION_REQUIRED → CLARIFICATION_COMPLETE]
→ MISSION_SPEC_READY → STRATEGY_DISCOVERY_RUNNING → STRATEGY_SET_READY
→ SWARM_DESIGN_RUNNING → SWARM_READY
→ RESEARCH_RUNNING → RESEARCH_REVIEW_RUNNING → INFORMATION_GAPS_READY
→ CALL_SELECTION_RUNNING → CALL_PLAN_READY
→ CALL_AUTHORIZATION_PENDING → CALL_AUTHORIZED
→ CALL_EXECUTION_RUNNING → CALL_RESULT_RECEIVED → EVIDENCE_UPDATE_RUNNING
→ REPLAN_DECISION_RUNNING → [NEGOTIATION_OR_FOLLOWUP_RUNNING → back to CALL_SELECTION_RUNNING]
→ OPTIMIZATION_RUNNING → REVIEW_RUNNING → { REVIEW_FAILED → REPLAN_DECISION_RUNNING,
  REVIEW_PASSED → PLAN_OPTIONS_READY }
→ USER_DECISION_PENDING → [MISSION_REVISION_RUNNING → back into the loop]
→ COMPLETE | BLOCKED | CANCELED
```

`FINAL_EXECUTION_APPROVAL_PENDING` and `FINAL_EXECUTION_RUNNING` are **deferred past V1 and are not present in the V1 transition table**. V1 ends at user acceptance of a plan option: CallSwarm never books, buys or commits. Leaving an execution state reachable but unspecified would be an open hole for side effects, so it is not reachable at all until ticket CS-046 defines exactly what it may do.

## Research layer

`ResearchProvider` abstraction with two implementations for V1: a `FixtureResearchProvider` (clearly labelled test data, default in tests) and a live provider selected by `RESEARCH_PROVIDER`. Live research uses an officially supported search-grounding or search-API path verified against current documentation before implementation. Prohibited scraping is not performed. Fixture data is never rendered as live research — every artifact carries `source_type` and the UI displays it.

Pipeline: mission → strategy information requirements → research queries → raw candidates → entity normalization → public-information analysis → hard-constraint filter → shortlist → information gaps.

## Call layer

`CallExecutionProvider` interface: plan call, authorize/confirm plan, execute, poll status, fetch structured result, fetch transcript/activity where available, fetch failure detail, reconcile ambiguous state.

Implementations: `CalleProvider` (real) and `FakeCallProvider` (default; loudly labelled; used by all tests). See `04_CALL_E_INTEGRATION.md` for the verified CALL-E surface.

Call value is computed by deterministic code in `calls/scoring.py` from structured factors the model may estimate individually (mission impact, uncertainty, time sensitivity, expected value, strategy-changing potential, redundancy with existing evidence, call cost). The model never emits the final score. Any intent failing a safety or authorization check is blocked regardless of score.

## Evidence layer — the Reality Graph

Every claim is stored with subject, predicate, value, source and timestamp. Statuses: `UNKNOWN`, `WEB_SUPPORTED`, `PHONE_SUPPORTED`, `MULTI_SOURCE_SUPPORTED`, `CONFLICTED`, `STALE`, `REJECTED`.

Conflicts are never silently resolved — both claims are kept and surfaced. Phone evidence represents *what was stated on the call*, not verified objective truth; confidence rises only through consistency, independent sources, written confirmation or a clarification call.

## Optimizer

Hard constraints are enforced in code (budget, date, dietary requirement, PSU/GPU compatibility, delivery deadline). Soft preferences use weighted scoring with weights from the `MissionSpec`; application code computes totals. The optimizer compares *complete effective plans*, not per-category minima — a costlier venue that includes decoration, parking and staff can win.

Claimed results are worded as "best evaluated solution under the researched candidate set and current evidence", never as a global optimum.

## Backend ↔ frontend

REST/JSON for commands (create mission, answer clarification, change constraints, approve calls, approve execution). Server-Sent Events at `/api/missions/{id}/events` for live activity: agent created, agent status changed, research event, call event, evidence update, strategy update, optimizer update, review result, mission completion.

Every emitted event is a real `ActivityEvent` persisted by the runtime. There is no synthetic UI activity.

The no-chain-of-thought guard is **not** limited to the event emitter. Model-authored free text also reaches the client through plain REST — `StrategyCandidate.description` and `assumptions`, `AgentRun.activity_summary`, `AgentRun.output_artifact`, critic findings. A shared response sanitizer is therefore applied to every API response and to the persistence of `output_artifact`, not only to `ActivityEvent` payloads.

## Persistence and scheduling

SQLite via `sqlite+aiosqlite` for local development; schema stays PostgreSQL-compatible. Persisted: missions, constraints, strategies, agent specs, agent requests, agent states, research artifacts, candidates, call intents, call runs, recipient results, evidence, plan options, approvals, activity events, suppression entries and scheduled jobs.

Deleting a mission cascades to its children — **except `SuppressionEntry`, which is deliberately mission-independent**: an opt-out must outlive the mission that recorded it.

The scheduler persists jobs (follow-up calls, retries, deadline checks) to the database — never in-memory only. A scheduled call re-checks authorization, quiet hours, mission status, recipient suppression, call budget and cancellation at fire time. Canceling a mission blocks all its future scheduled calls.

## Authentication

Not a blocker for V1. A clearly labelled local demo session is used until real auth is configured; the architecture leaves room for Firebase Authentication with Google Sign-In. No fake provider buttons are ever shown.
