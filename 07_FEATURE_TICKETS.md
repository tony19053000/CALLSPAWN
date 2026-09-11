# 07 — Feature Tickets

Every ticket is an executable unit. `[CODER]` implements; `[REVIEWER / TESTER]` returns PASS or FAIL; only a PASS advances `STATUS.md`.

Status values: `PENDING`, `IN_PROGRESS`, `REVIEW`, `DONE`, `BLOCKED`.

---

## Phase 1 — Foundation

### CS-001 — Backend skeleton and configuration
**Purpose** A runnable FastAPI app with typed settings and a health endpoint.
**Modules** `backend/callswarm/{api,config}`, `pyproject.toml`.
**Depends on** —
**Implementation** Python 3.11+ project with `pyproject.toml` (fastapi, uvicorn, pydantic v2, pydantic-settings, sqlalchemy[asyncio], aiosqlite, httpx, google-genai, pytest, pytest-asyncio, ruff, mypy). `Settings` loads every variable in `.env.example` with safe defaults; `CALLE_LIVE_CALLS_ENABLED` defaults `False` and `CALL_PROVIDER` defaults `fake`. `GET /health` returns app status plus a capability report (llm configured, call provider, live calls enabled, research provider) with **no secret values**.
**Acceptance** `uvicorn` starts; `/health` returns the capability report; missing optional keys degrade rather than crash; ruff and mypy pass.
**Tests** Settings defaults; health payload contains no secrets.
**Safety** Establishes the default-off call posture.
**Demo** Startup check for judges.
**Status** DONE

### CS-002 — Domain models
**Purpose** The shared contract layer every other module depends on.
**Modules** `backend/callswarm/models/`.
**Depends on** CS-001
**Implementation** Pydantic v2 models for `Mission`, `MissionSpec`, `AuthorityPolicy`, `CallBudget`, `StrategyCandidate`, `AgentSpec`, `AgentRequest`, `AgentRun`, `ResearchArtifact`, `CandidateEntity`, `InformationGap`, `CallIntent`, `CallRun`, `RecipientResult`, `EvidenceClaim`, `PlanOption`, `Approval`, `SuppressionEntry`, `ActivityEvent`, plus the state and status enums from `02_ARCHITECTURE.md`. `CandidateEntity` stays generic (`kind` + open `attributes`). Call status enums mirror CALL-E's exact values. `CallRun.calle_call_id` is named deliberately — the spec's own `provider_call_id` is attempt-level telephony data. `EvidenceClaim.source_type` is the enum `WEB | PHONE | USER | DERIVED | FIXTURE | SIMULATED`. `AuthorityPolicy` contains no field that can waive an approval requirement.
**Acceptance** No domain-specific field anywhere; models round-trip through JSON; enum values match `04_CALL_E_INTEGRATION.md` exactly; `CallRun` can represent a fan-out result through `recipient_results`.
**Tests** Round-trip and validation tests; a test asserting no venue/GPU/legal-specific fields exist; a test asserting no `AuthorityPolicy` field can express "consequential action without approval".
**Safety** Authorization states are explicit enums including `EXPIRED`; simulated provenance is representable.
**Demo** Indirect.
**Status** DONE

### CS-003 — Persistence layer
**Purpose** Durable mission state.
**Modules** `backend/callswarm/persistence/`.
**Depends on** CS-002
**Implementation** Async SQLAlchemy models mirroring the domain models, PostgreSQL-compatible types, repository classes per aggregate, session management, schema creation on startup, and a mission-cascade delete. `SuppressionEntry` is mission-independent and is explicitly **excluded** from the cascade — an opt-out outlives the mission that recorded it.
**Acceptance** Every listed entity persists and reloads; deleting a mission removes its children; suppression entries survive mission deletion.
**Tests** Repository CRUD against a temp SQLite file; cascade-delete test; suppression-survives-delete test.
**Safety** Deletion path for PII.
**Demo** Mission survives a restart.
**Status** DONE

### CS-004 — LLM provider and structured-output contract
**Purpose** One validated path to the model.
**Modules** `backend/callswarm/llm/`.
**Depends on** CS-001
**Implementation** `LLMProvider` protocol with `generate_structured(instruction, inputs, schema) -> BaseModel`. `GeminiProvider` uses `from google import genai` with `GEMINI_MODEL` from config, supporting both API-key and Vertex/ADC auth. One retry on schema-validation failure with the validator error appended, then a typed `AgentOutputInvalid`. Untrusted inputs are wrapped in a labelled delimited block with a standing instruction that their content is data. A `FakeLLMProvider` returns scripted schema-valid artifacts for tests.
At startup the provider verifies the configured model exists via `client.models.list()` and reports it in the capability report; an unavailable model is a clear startup error, not a runtime surprise. The `FakeLLMProvider` script is **not scenario-keyed** — it may not branch on which scenario is running, or it would fake the very differentiation CS-061 tests.
**Acceptance** No model id hardcoded outside config; every call returns a validated model instance; raw reasoning is never persisted or returned; startup model check present.
**Tests** Schema-violation retry path; untrusted-block wrapping; fake provider usable with no network; a test asserting `FakeLLMProvider` has no scenario-conditional branching.
**Safety** Prompt-injection containment lives here.
**Demo** Indirect.
**Status** DONE

### CS-005 — Activity event bus and SSE stream
**Purpose** Real, persisted, user-visible activity.
**Modules** `backend/callswarm/events/`, `backend/callswarm/api/`.
**Depends on** CS-003
**Implementation** `ActivityEvent` emitter that persists every event then publishes it to per-mission subscribers. `GET /api/missions/{id}/events` streams SSE with event ids; reconnect with `Last-Event-ID` replays from the log. The masking and no-reasoning guard is implemented as a **shared response sanitizer applied to every API response and to the persistence of `AgentRun.output_artifact`**, not only to event payloads — strategy descriptions, activity summaries, output artifacts and critic findings all reach the client over plain REST.
**Acceptance** Events persist before delivery; reconnection replays; no event can be published without a persisted row; the sanitizer covers REST responses as well as SSE.
**Tests** Replay after disconnect; masking guard on both SSE and REST; ordering; an artifact containing reasoning-like text is sanitized before persistence.
**Safety** Enforces the no-chain-of-thought and masking rules at the choke point.
**Demo** The live swarm panel depends on this.
**Status** DONE

---

## Phase 2 — Mission intake and swarm design

### CS-010 — Mission intake and clarification loop
**Purpose** Turn natural language into a validated `MissionSpec`.
**Modules** `orchestrator/intake.py`, `api/missions.py`.
**Depends on** CS-004, CS-005, CS-011
**Implementation** `POST /api/missions` accepts a free-text goal. The Orchestrator extracts a draft `MissionSpec` and a list of clarification questions, each carrying the decision it unblocks and an importance score; only questions above the threshold are asked. `POST /api/missions/{id}/answers` merges answers and re-validates. Unanswered low-importance questions become recorded assumptions.
**Acceptance** A vague goal produces questions; a fully specified goal produces none; answers update the spec without losing prior state.
**Tests** Both paths; assumption recording; state transitions `MISSION_CREATED → … → MISSION_SPEC_READY`.
**Safety** The authority policy is captured here, defaulting to calls not allowed until set.
**Demo** Opening beat of the demo.
**Status** DONE

### CS-011 — Mission state machine
**Purpose** Deterministic control flow.
**Modules** `orchestrator/state_machine.py`.
**Depends on** CS-003
**Implementation** Explicit transition table from `02_ARCHITECTURE.md`. The model proposes a next action; code validates it against the allowed set and applies it, persisting every transition with its trigger. Invalid proposals raise and are logged, never applied.
**Acceptance** Every transition is table-driven; an illegal proposal cannot mutate state.
**Tests** Full happy path; rejection of illegal transitions; replan loop-backs.
**Safety** Prevents a model from skipping an authorization state.
**Demo** Drives the mission timeline.
**Status** DONE

### CS-012 — Strategy generation and pruning
**Purpose** Solution strategies before specialists.
**Modules** `strategies/`.
**Depends on** CS-010, CS-011
**Implementation** Generate 3–5 `StrategyCandidate`s that differ in objective or assumption, each with assumptions, benefits, drawbacks, required information and expected dependencies. A structural check rejects near-duplicates. Strategies can be pruned by research and revived by later evidence, with the reason persisted.
**Acceptance** Strategies differ substantively; the PC mission yields sourcing-model strategies, not component lists; prune and revive both work.
**Tests** Diversity check; revive-on-evidence test; scenario snapshots.
**Safety** —
**Demo** Central to the "it reasons about approach" beat.
**Status** DONE

### CS-013 — Dynamic agent factory
**Purpose** Generate the swarm from the mission.
**Modules** `agents/factory.py`.
**Depends on** CS-012
**Implementation** From the `MissionSpec` and surviving strategies, generate `AgentSpec`s, each answering why it exists, what it owns, what evidence it needs, its tools, its output schema, what it does not control and its stop conditions. Reject overlapping specs unless justified; cap agent count by a complexity score derived from constraint count, category count and strategy count. Tool grants are validated against an allow-list; a spec requesting an unlisted tool is rejected.
Framework components (Orchestrator, Strategy Architect, Call Strategy, Evidence Engine, Optimizer, Critic) are code and are never generated; every domain role is generated.
**Acceptance** Different scenarios produce materially different specs; a simple mission produces a small swarm; no hardcoded domain names exist in this module or in any prompt template it uses.
**Tests** Cross-scenario differentiation; complexity-to-count relationship; tool allow-list enforcement; a grep-style test for domain nouns in framework code.
**Safety** Tool bounding and policy check on every spec.
**Demo** The swarm assembling live is the signature moment.
**Status** DONE

### CS-014 — Agent runner and dependency graph
**Purpose** Execute the generated swarm.
**Modules** `agents/runner.py`, `orchestrator/graph.py`.
**Depends on** CS-013
**Implementation** Build a DAG from `AgentSpec.dependencies`; run ready agents concurrently with bounded concurrency; validate each output against `expected_output_schema`; drive lifecycle states; emit activity events on every transition; retry once on invalid output then mark `FAILED`. Only the Orchestrator mutates agent lifecycle; a specialist may emit an `AgentRequest` artifact instead of spawning.
**Acceptance** Independent agents run in parallel; dependents wait; a failing agent does not stall the mission; no agent can spawn another.
**Tests** Parallelism; dependency ordering; failure isolation; spawn-attempt rejection.
**Safety** Enforces the spawn-authority boundary.
**Demo** Live status changes in the left panel.
**Status** DONE

---

## Phase 3 — Research and information gaps

### CS-020 — Research provider abstraction
**Purpose** One pluggable research surface with provenance.
**Modules** `research/`.
**Depends on** CS-004
**Implementation** `ResearchProvider` protocol; `FixtureResearchProvider` (default; every artifact stamped `source_type=fixture`) and a live provider chosen by `RESEARCH_PROVIDER`, implemented against an officially supported search-grounding or search API verified at implementation time. No prohibited scraping. All retrieved text flows through the untrusted-input wrapper.
**Acceptance** Fixture output can never be rendered as live; provenance is mandatory on every artifact; missing credentials degrade to fixtures with a recorded blocker.
**Tests** Provenance required; fixture labelling surfaces through the API; injection text in a page does not alter behavior.
**Safety** Untrusted-input boundary.
**Demo** The research beat.
**Status** DONE

### CS-021 — Candidate normalization and constraint filtering
**Purpose** Raw results into comparable entities.
**Modules** `research/pipeline.py`.
**Depends on** CS-020, CS-002
**Implementation** Normalize raw results into `CandidateEntity` rows with deduplication, then apply code-enforced hard constraints to produce a shortlist. Each filter decision records the constraint and the value that failed.
**Acceptance** Pipeline is domain-agnostic; every exclusion has a stated reason; dedup works across sources.
**Tests** Domain-agnostic run over two unrelated fixture sets; filter-reason completeness.
**Safety** —
**Demo** "100 found → 30 relevant → 12 qualifying".
**Status** DONE

### CS-022 — Information-gap engine
**Purpose** Separate what is known from what must be asked.
**Modules** `research/gaps.py`.
**Depends on** CS-021
**Implementation** For each shortlisted candidate, classify the decision-relevant attributes as `KNOWN`, `UNKNOWN` or `CONFLICTED`, each linked to the decision it affects, its importance and current confidence, and its possible resolution methods.
**Acceptance** Gaps reference real decisions; a fully known candidate produces no gaps; conflicts are detected rather than averaged.
**Tests** Classification over fixtures; conflict detection; no-gap case.
**Safety** —
**Demo** The known/unknown table.
**Status** DONE

---

## Phase 4 — Calls

### CS-030 — Call value scoring and selection
**Purpose** Decide which calls are worth making.
**Modules** `calls/scoring.py`, `calls/strategy.py`.
**Depends on** CS-022
**Implementation** The model estimates individual structured factors (mission impact, uncertainty, time sensitivity, expected value, strategy-changing potential, redundancy, call cost); **deterministic code computes the final priority**. Selection respects the mission call budget and records, for every rejected intent, why it was rejected. The model never emits a final score.
**Acceptance** Scores are reproducible from factors; rejections carry reasons; budget is never exceeded; a mission needing one call selects one.
**Tests** Determinism given fixed factors; budget cap; the "9 possible, 3 selected" scenario.
**Safety** Blocked intents stay blocked regardless of score.
**Demo** The differentiating beat: the swarm declining to call.
**Status** DONE

### CS-031 — Call provider abstraction and fake provider
**Purpose** A testable call surface that cannot dial by accident.
**Modules** `calls/provider.py`, `calls/fake.py`.
**Depends on** CS-002
**Implementation** `CallExecutionProvider` with `plan_call`, `authorize`, `execute`, `get_status`, `get_events` (paginated), `get_result`, `reconcile` (identical replay under the same idempotency key) and `cancel_local`. There is **no remote cancel** — the API exposes none, so `cancel_local` only cancels an unexecuted plan or an unfired scheduled job. `FakeCallProvider` is the default, is loudly labelled, emits the real status sequence, performs no network I/O, and stamps every resulting claim `source_type=SIMULATED`.
**Acceptance** Fake is the default everywhere; no test can reach the network; every fake result is marked `SIMULATED` end to end, through derived plan options to the UI badge.
**Tests** Default-provider assertion; a network-blocking test over the whole suite; a test that a plan option derived from a fake call is flagged simulated.
**Safety** The core no-accidental-call control.
**Demo** Enables offline rehearsal.
**Status** DONE

### CS-032 — CALL-E provider implementation
**Purpose** Real calls through the verified API.
**Modules** `calls/calle.py`.
**Depends on** CS-031, CS-033, CS-034
**Implementation** Implement against `POST /v1/calls`, `GET /v1/calls/{id}`, `GET /v1/calls/{id}/events` with bearer auth, a deterministic `Idempotency-Key` from `call_intent_id`, explicit `recipients[]` in E.164, generated `result_schema`, and `metadata` carrying mission and intent ids. Map CALL-E status values directly. `structured_result: null` is an explicit unresolved outcome. An ambiguous create is reconciled via status lookup, never retried blindly. Numbers masked in every log line.
Recipient-level results from a fan-out call are mapped into `CallRun.recipient_results`. `get_events` follows `next_cursor` rather than truncating. The base URL is re-confirmed against live documentation before implementation, since the vendored snapshot calls it a placeholder.
**Acceptance** Matches the vendored OpenAPI contract; schema validated before send and result validated after; masking verified; **`CalleProvider.execute` itself raises unless `CALLE_LIVE_CALLS_ENABLED` is true, `CALL_PROVIDER=calle`, the recipient passes the allow-list and suppression checks, and an `APPROVED` `Approval` exists for that intent** — a last line of defence independent of the approvals module.
**Tests** Against a recorded-response mock of the spec; idempotency; replay-based reconciliation; null-result handling; execute raises for approvals in `PENDING`, `REJECTED` and `EXPIRED`; execute raises when the env switch is off; event pagination.
**Safety** Idempotency, masking, no blind retry, in-provider gate enforcement.
**Demo** The real call.
**Status** DONE

### CS-033 — Result schema generator and validator
**Purpose** Per-call structured extraction that respects CALL-E's constraints.
**Modules** `calls/schema.py`.
**Depends on** CS-030
**Implementation** Generate a call-specific JSON Schema per intent using only supported features (`type`, `properties`, `required`, `enum`, nested objects, simple `array.items`, `description`, `additionalProperties: false`); reject `$ref`, `oneOf`, `anyOf`, `allOf`, recursion and `additionalProperties: true`. Prefer string enums with an `unknown` value; never use reserved recipient field names. Validate before send and validate the returned result before it becomes evidence.
The validator also rejects any **required non-enum field whose description implies it may be omitted** — under `additionalProperties: false` a missing required field voids the entire extraction. Optional numeric answers must be paired with a required status enum.
**Acceptance** Generated schemas always pass the constraint validator; an unsupported feature is rejected with a clear error; `unknown` is always representable; no required field can be legitimately absent.
**Tests** Constraint validator over generated and hand-written schemas; reserved-name rejection; required-but-omittable rejection.
**Safety** Prevents silently invalid extraction.
**Demo** The requested-fields list in the call panel.
**Status** DONE

### CS-034 — Authorization and approval gates
**Purpose** Nothing dials without explicit permission.
**Modules** `approvals/`, `api/approvals.py`.
**Depends on** CS-031, CS-011
**Implementation** Approval records with `PENDING`/`APPROVED`/`REJECTED`/`EXPIRED`; `POST /api/missions/{id}/approvals/{approval_id}/decision` with a required typed body `{"decision": "APPROVED" | "REJECTED"}` — approve and reject are never distinguishable only by path, and a malformed or empty body is a 422, never a default approval. Execution requires both the environment switch and an `APPROVED` record for that intent; quiet hours, the `CALL_ALLOWED_RECIPIENTS` allow-list (empty means allow none), the suppression deny-list and the budget are re-checked at execute time. Conversational phrasing never authorizes.
**Acceptance** Execution without approval raises; expired, pending and rejected approvals do not execute; the suppression list blocks even an approved intent; an empty allow-list blocks every live call; an empty decision body is rejected.
**Tests** Each gate independently and combined; a test proving chat text cannot authorize; a test proving a malformed decision body cannot approve.
**Safety** The central safety ticket.
**Demo** The human-in-the-loop moment.
**Status** DONE

### CS-035 — Call patterns
**Purpose** Structured multi-call workflows.
**Modules** `calls/patterns.py`.
**Depends on** CS-032, CS-034
**Implementation** Implement one-shot, fan-out, cascade, negotiation round, clarification, verification, follow-up, escalation and human gate as composable pattern handlers the Call Strategy agent selects per mission. No mission is forced through every pattern.
**Acceptance** A mission uses only the patterns it selects; a negotiation round demonstrably carries the prior quote into the next call task.
**Tests** Each pattern against the fake provider; the second-round negotiation path end to end.
**Safety** Every pattern passes through the same gates.
**Demo** The negotiation beat.
**Status** DONE

### CS-036 — Terminal webhook receiver
**Purpose** Receive CALL-E terminal events safely.
**Modules** `api/webhooks.py`, `calls/`, `evidence/`.
**Depends on** CS-032, CS-040
**Implementation** `POST /calle/webhook` receiving `call.completed`, `call.failed` and `call.result_validation_failed`. The CALL-E spec declares this path unauthenticated, and its payload carries a full `CallTask` including `structured_result` — so an unguarded receiver is a direct evidence-injection path. Protect it with the `CALLE_WEBHOOK_SECRET` shared secret (unguessable path token, constant-time comparison; upgrade to signature verification if CALL-E publishes one). Read the required `CALL-E-Event-Id` header and **persist it before any side effect** so duplicate deliveries are ignored. Reject any payload whose `data.id` does not match a `CallRun` this instance created, or whose `data.metadata.mission_id` and `call_intent_id` do not correlate. Treat an accepted payload as a notification only, then re-read authoritative state via `GET /v1/calls/{call_id}` before writing evidence.
**Acceptance** A forged payload for an unknown call id is rejected; a replayed event id produces no second side effect; a payload with a bad secret is rejected; evidence is written only from the re-read authoritative state.
**Tests** Forged payload; replayed event id; wrong secret; mismatched metadata; happy path.
**Safety** Closes the evidence-injection hole on an unauthenticated public endpoint.
**Demo** Real-time call completion in the UI.
**Status** DONE

---

## Phase 5 — Evidence, optimization, review

### CS-040 — Reality Graph and evidence engine
**Purpose** Traceable facts.
**Modules** `evidence/`.
**Depends on** CS-020, CS-032
**Implementation** Normalize research and call outputs into `EvidenceClaim`s with subject, predicate, value, source, timestamp and freshness; statuses `UNKNOWN`, `WEB_SUPPORTED`, `PHONE_SUPPORTED`, `MULTI_SOURCE_SUPPORTED`, `CONFLICTED`, `STALE`, `REJECTED`. Conflicts are stored, not resolved. Notify the Orchestrator when a conflict can change a decision.
Claims inherit `FIXTURE` or `SIMULATED` from their producing provider, and that marker propagates into every derived `PlanOption`.
**Acceptance** Every plan figure traces to at least one claim; conflicting claims both persist; phone claims never receive a verified-truth status; simulated provenance survives every derivation step.
**Tests** Conflict retention; staleness; provenance completeness on a full mission run; simulated-marker propagation into plan options.
**Safety** Prevents laundering a phone statement into fact.
**Demo** The evidence panel.
**Status** PENDING

### CS-041 — Replanning engine
**Purpose** React to reality.
**Modules** `orchestrator/replan.py`.
**Depends on** CS-040, CS-014
**Implementation** On new evidence, a critic FAIL or a constraint change, decide among rerunning an agent, creating a specialist, stopping an obsolete agent, pruning or reviving a strategy, another research pass, another call round, or proceeding. Persist the decision and its trigger.
**Acceptance** A bundle-discount call result creates a new specialist and changes the active plan; an impossible strategy stops its agents with a stated reason.
**Tests** The bundle scenario; the stop-with-reason scenario; loop-guard against infinite replanning.
**Safety** Replans still respect the call budget.
**Demo** The "it changed its mind for a reason" beat.
**Status** PENDING

### CS-042 — Incremental constraint updates
**Purpose** Change constraints without losing evidence.
**Modules** `orchestrator/revision.py`, `api/missions.py`.
**Depends on** CS-041
**Implementation** `POST /api/missions/{id}/constraints` updates the spec, marks a component `LOCKED` where requested, and marks only dependent derived artifacts stale via the artifact dependency graph. Research artifacts, call evidence and verified vendor facts are preserved. Only owning agents rerun.
**Acceptance** "Under ₹3.2 lakh but keep photography" preserves all evidence, locks photography and reruns only the affected agents.
**Tests** Evidence-preservation assertion; selective staleness; locked component untouched.
**Safety** No re-dialing of already-answered questions, which also protects the call budget.
**Demo** The late-constraint-change beat.
**Status** PENDING

### CS-043 — Global optimizer
**Purpose** Best complete solution, not cheapest parts.
**Modules** `optimizer/`.
**Depends on** CS-040
**Implementation** Code-enforced hard constraints (budget, date, dietary, compatibility, deadline) and weighted soft scoring with weights from the `MissionSpec`; assemble 2–3 complete `PlanOption`s comparing effective totals including inclusions, shipping, assembly and warranty complexity. Savings are computed from two sourced figures or not claimed. Wording is "best evaluated solution under the researched candidate set".
**Acceptance** A costlier inclusive option can win; no option violates a hard constraint; every figure traces to evidence.
**Tests** The inclusive-venue case; constraint enforcement; savings provenance.
**Safety** No fabricated savings.
**Demo** The final options screen.
**Status** PENDING

### CS-044 — Critic agent
**Purpose** Challenge the plan before the user sees it.
**Modules** `agents/critic.py`.
**Depends on** CS-043
**Implementation** Check hard constraints, unsupported claims, unresolved high-impact unknowns, contradictory evidence, missing dependencies, price inconsistencies, unrealistic assumptions, stale information, misused call results, candidate-selection bias, whether another call would materially help, whether the call budget was respected, and whether the plan actually solves the stated goal. Return PASS or FAIL with exact findings and a suggested next action.
**Acceptance** A seeded flaw is caught; a FAIL routes back into replanning; a clean plan passes without invented objections.
**Tests** Seeded-flaw detection for each check class; no-false-fail on a clean plan.
**Safety** Last line against overclaiming.
**Demo** The self-review beat.
**Status** PENDING

### CS-045 — Persisted scheduler
**Purpose** Reliable follow-ups.
**Modules** `scheduler/`.
**Depends on** CS-034, CS-003
**Implementation** Database-backed jobs with due time, mission link and type; a worker that re-checks authorization, quiet hours, mission status, suppression, budget and cancellation at fire time. No in-memory-only scheduling for anything consequential.
Canceling a mission cancels its unfired jobs; an already-executed call cannot be recalled, and the scheduler must not claim otherwise.
**Acceptance** Jobs survive a restart; canceling a mission cancels its unfired jobs; a job whose gate fails is recorded as skipped with a reason.
**Tests** Restart survival; cancellation; each gate at fire time.
**Safety** Time-shifted calls get the same gates as immediate ones.
**Demo** The follow-up beat.
**Status** PENDING

### CS-046 — Final execution (DEFERRED, not in V1)
**Purpose** Bound what "executing" a plan would mean, before any code can do it.
**Status** DEFERRED — explicitly out of V1 scope.
The states `FINAL_EXECUTION_APPROVAL_PENDING` and `FINAL_EXECUTION_RUNNING` are absent from the V1 transition table. V1 ends at user acceptance of a plan option: CallSwarm never books, buys or commits on the user's behalf. A specified-but-unimplemented execution state that the state machine could legally enter would be an open hole for side effects, so it is unreachable rather than merely unimplemented. Any future work here must first enumerate the exact permitted actions, their approval records and their reversibility.

---

## Phase 6 — Frontend

### CS-050 — Web app shell and SSE client
**Modules** `apps/web/`. **Depends on** CS-005, CS-010.
Next.js App Router + TypeScript + Tailwind; three-panel workspace; typed API client; SSE hook with `Last-Event-ID` reconnect and replay. No credentials client-side.
**Acceptance** Reconnect replays missed events; refresh loses no history. **Tests** SSE reconnect; no secret in the bundle. **Status** PENDING

### CS-051 — Live swarm panel and mission feed
**Modules** `apps/web/`. **Depends on** CS-050, CS-014.
Agent cards driven entirely by real `AgentSpec`/`AgentRun` data, including stopped agents with reasons; framework components and generated specialists are visually distinguished; center feed carries major mission events only.
**Acceptance** Every card maps to a backend spec; no synthetic states. **Tests** A test asserting the card list equals the API agent list. **Status** PENDING

### CS-052 — Inspector, call UI and evidence browser
**Modules** `apps/web/`. **Depends on** CS-051, CS-040.
Agent inspector, call panel with masked number and live fields, Reality Graph browser showing sources, conflicts and freshness.
**Acceptance** Numbers masked; conflicts shown unresolved; no control implying live-call steering or remote call cancellation; `FIXTURE`/`SIMULATED` claims and any plan option derived from them carry a persistent non-dismissible SIMULATED badge. **Tests** Masking; conflict rendering; simulated badge present on every derived option. **Status** PENDING

### CS-053 — Swarm graph
**Modules** `apps/web/`. **Depends on** CS-051.
Real dependency graph (verify `@xyflow/react` before adding). Nodes and edges reflect actual dependencies and artifact flow; state changes are driven by runtime events.
**Acceptance** No decorative edges; node states match backend states. **Tests** Graph built from API data only. **Status** PENDING

### CS-054 — Mission view, approvals and final plan UI
**Modules** `apps/web/`. **Depends on** CS-034, CS-043.
Mission constraints and permissions view, explicit approval controls, constraint-revision control, final options with components, evidence, trade-offs and uncertainty.
**Acceptance** Approval requires an explicit action; every displayed figure links to its evidence. **Tests** Approval flow; evidence linkage. **Status** PENDING

---

## Phase 7 — Proof and submission

### CS-060 — Scenario harness
**Modules** `scenarios/`, `tests/`. **Depends on** CS-035, CS-041, CS-042, CS-044, CS-045.
Five scenarios — anniversary, PC build, professional services, web leads, simple task — each with fixture research data and a fake-call script, runnable end to end offline.
No scenario fixture may supply an `AgentSpec` list — fixtures provide research data and call scripts only, never the swarm. **Acceptance** All five complete without network access; each exercises replanning and at least one evidence-driven second-round call where its mission calls for it. **Tests** End-to-end per scenario. **Status** PENDING

### CS-061 — Generalization tests
**Modules** `tests/`. **Depends on** CS-060.
Assert semantically that the anniversary mission creates event-relevant specialists, the PC mission creates compatibility and vendor-risk specialists, the professional-services mission creates neither GPU nor venue agents, the simple mission stays small, and changing constraints changes the swarm. Validate by capability semantics, not by exact generated names.

**Cost control.** These are the only tests permitted to touch a real model, and they run from **recorded cassettes** under `tests/cassettes/` by default: capture once with an explicit `--record` flag, replay thereafter, never re-record automatically or in CI. Recording uses the cheapest model that still exercises real generation (a Flash-class `GEMINI_MODEL`). A cassette contains no secrets and no real phone numbers.

These tests **do not run against `FakeLLMProvider`**. A scripted fake would satisfy every differentiation assertion while the factory stayed hardcoded, which is the largest available hole in the gate. They run against a real `GeminiProvider` or against recorded real-model cassettes, and they include a **sixth mission that has no fixture directory in `scenarios/`**, whose swarm must still be valid, distinct and capability-appropriate.

The static domain-noun check scans `orchestrator/`, `agents/`, `strategies/`, `research/`, `calls/`, `evidence/` and `optimizer/` — **including every string literal and every prompt template file**, since a hardcoded list hides just as well in a prompt as in a list. It also asserts no scenario fixture supplies an `AgentSpec` list.

**Tests** The assertions above, plus a negative fixture: a deliberately hardcoded factory variant that the same suite runs against and **must fail**. A gate that has never been seen to fail is not a gate.
**Acceptance** The hardcoded variant fails; the real factory passes; the unseen sixth mission produces an appropriate swarm. **Status** PENDING

### CS-062 — Live CALL-E verification
**Depends on** CS-032, CS-034, CS-036.
One authorized live call to a consenting number, recording the real call id, status progression and structured result in `STATUS.md`. Free-call budget is limited; run it deliberately, not repeatedly.
**Acceptance** Recorded written consent from the recipient; the recipient present in `CALL_ALLOWED_RECIPIENTS`; an `APPROVED` `Approval` whose id is quoted in `STATUS.md`; quiet-hours and suppression checks passing; `CALLE_LIVE_CALLS_ENABLED=true` set only for the duration of the run and **reverted to `false` in the same commit**; the result reaching the Reality Graph through the normal path with `source_type=PHONE`. **Status** PENDING

### CS-063 — Documentation, README and judge instructions
**Depends on** CS-060.
README with setup, environment, offline scenario run, live-call instructions and explicit safety notes; judge-facing quickstart; architecture summary.
**Acceptance** A clean clone reaches a running offline scenario by following the README alone. **Status** PENDING

### CS-064 — Hackathon submission package
**Depends on** CS-062, CS-063.
Re-verify the current official rules, open the required pull request to `CALLE-AI/awesome-phone-call-agents` following its README placement instructions, record the PR URL, prepare the Devpost description, a demo video under three minutes, judge testing instructions, the CALL-E account email and a working deployment or test build.
**Acceptance** Every rule verified against the live rules page at submission time; nothing fabricated. **Status** PENDING
