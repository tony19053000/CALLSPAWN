# STATUS — CallSwarm

_Last updated: 2026-09-11 (Phase 4a complete)_

## Overall completion

**50%** — Phases 0–3 and 4a complete (17 of 36 active tickets, all reviewer-PASSed). Foundation, dynamic-swarm core, research layer, and the call-planning and authorization core: gated provider base, fake provider, CALL-E-constrained result schemas, deterministic call-value scoring and selection, approval service and gates.

## Current phase

Phase 4a complete → Phase 4b (real CALL-E provider, patterns, webhook).

## Current ticket

`CS-032 — CALL-E provider implementation` (PENDING).

## Completed

- Repository initialized, committed and pushed to `origin/main` (`https://github.com/tony19053000/CALLSPAWN.git`).
- Development subagents created: `.claude/agents/coder.md`, `.claude/agents/reviewer-tester.md`.
- Anchor documents written: `01_PRD.md`, `02_ARCHITECTURE.md`, `03_SWARM_ORCHESTRATION.md`, `04_CALL_E_INTEGRATION.md`, `05_SECURITY_SAFETY.md`, `06_FRONTEND_SPEC.md`, `07_FEATURE_TICKETS.md`.
- `CLAUDE.md`, `STATUS.md`, `README.md`, `.env.example`, `.gitignore`.
- CALL-E integration surface verified against live official sources; OpenAPI contract vendored at `docs/vendor/calle.openapi.yaml`.

- **Phase 1 (CS-001…CS-005) — reviewer PASS 2026-09-11.** `backend/` package: `config/` typed settings + `/health` capability report; `models/` full domain layer with exact CALL-E enums; `persistence/` 17 async SQLAlchemy tables, repositories, explicit cascade excluding `suppression_entries`; `llm/` `LLMProvider`, `GeminiProvider`, `FakeLLMProvider`, `untrusted_block`; `events/` persisted emitter + SSE with `Last-Event-ID` replay; `sanitize.py` applied at emitter, JSON response hook and artifact persistence. 89 tests, ruff clean, mypy strict clean.

- **Phase 2 (CS-010…CS-014) — reviewer PASS 2026-09-11** after one FAIL (intake merge path did not fail closed; fixed). `orchestrator/state_machine.py` explicit transition table validated against persisted status, `mission_transitions` audit table; `orchestrator/intake.py` + `api/missions.py` create/answer/get with importance-threshold clarification, assumptions, narrowing-only authority clamp, fail-closed to BLOCKED; `strategies/` architect with distinct-axis + Jaccard diversity, prune/revive with persisted reasons; `agents/factory.py` code-side tool allow-list, reserved names, prohibited purposes, overlap merge, DAG/cycle checks, complexity-scored cap with one reduce request then leaf truncation; `agents/runner.py` + `orchestrator/graph.py` bounded-concurrency DAG runner with full lifecycle, one retry, BLOCKED dependents, no spawning, stub tools. Static domain-noun gate verified by the reviewer to fail when a noun is injected. 182 tests.

- **Phase 3 (CS-020…CS-022) — reviewer PASS 2026-09-11** after two FAILs (SSRF: no private-address refusal; then DNS-rebinding TOCTOU between check and connect). `research/provider.py` protocol with provenance required at the model level; `research/fixture.py` (`source_type=FIXTURE` as a fixed class attribute, no override); `research/live.py` `GeminiGroundedResearchProvider` against verified `google-genai` 2.23.0 types with honest snippet labelling; `research/fetch.py` `PublicPageFetcher` with scheme/userinfo refusal, robots.txt, timeout, size cap, content-type restriction, per-hop redirect checks, and a once-per-hop resolve-validate-pin guard (URL host rewritten to the validated IP, `Host` preserved, `sni_hostname` extension for TLS); `research/service.py` provider selection with loud fixture fallback surfaced in `/health` and as a blocker event; `research/pipeline.py` normalize/dedup/hard-constraint filter; `research/gaps.py` gap engine and knowledge table. Research stubs in `agents/tools.py` replaced with real, fenced tool calls. 246 tests.

- **Phase 4a (CS-030, CS-031, CS-033, CS-034) — reviewer PASS 2026-09-11** with two medium non-blocking findings fixed before commit. `calls/provider.py` protocol + `GatedCallProvider` whose `@final` `execute` reloads intent/approval/mission from the DB by id and runs the gate before the abstract `_execute_authorized`, with an `__init_subclass__` guard against override; `calls/fake.py` default provider, `is_simulated` class constant, real status sequence, scripted outcomes incl. `None` and validation-failed; `CALL_PROVIDER=calle` raises at startup, never falls back; `calls/schema.py` CALL-E-subset validator (enums must contain `unknown`, booleans rejected, required-but-omittable rejected, reserved names rejected) with one guided retry; `calls/scoring.py` pure `compute_priority` + `select_calls` with a reason for every rejection; `calls/strategy.py` intents per callable candidate, code-validated patterns, `raw_phone` → gap not intent; `approvals/` service + `POST .../decision` with a required typed body (422 on anything else); `calls/gates.py` ordered gates each emitting a number-free refusal event; `calls/regions.py` quiet hours by region; `CallService.execute_call` idempotent on repeat. 357 tests.

## In progress

Nothing.

## Pending

19 active tickets: CS-032, CS-035, CS-036, CS-040 … CS-064 (excluding CS-046, deferred).

## Blockers

1. ~~No git remote configured.~~ **Resolved 2026-09-11** — remote `origin` set to `https://github.com/tony19053000/CALLSPAWN.git`, branch `main` pushed and tracking.
2. **No credentials present for the backend paths.** `GEMINI_API_KEY` and `CALLE_API_KEY` are unset, so live LLM calls, live research and backend-initiated live calls cannot run yet. Fake providers cover all development and testing. The CALL-E **CLI** is separately authenticated via browser OAuth and is usable now.
3. **CALL-E skill not installed.** `npx -y skills add https://github.com/CALLE-AI/call-e-integrations --skill calle -g` was blocked by the auto-mode permission classifier (remote package install at global scope). The CLI it depends on is installed and authenticated, so nothing is blocked functionally; the skill needs an explicit approval or a manual run by the user.

## CALL-E state

- **Integration chosen:** Developer API over HTTP from the Python backend (`CalleProvider`), with `calle-ai` SDK usage where it maps cleanly. MCP (`plan_call` / `run_call` / `get_call_run`) documented as the alternate path.
- **Contract verified:** 2026-09-11, against the official integrations repo, docs site and OpenAPI spec `0.7.0`. Snapshot vendored.
- **Auth state:** CLI authenticated. `@call-e/cli` installed globally on 2026-09-11; `calle auth status` reports `usable: true`, token cached at `~/.calle-mcp/cli/.../token.json`, expiry `2029-06-05T02:50:29Z`. `calle mcp tools` confirms `plan_call`, `run_call`, `get_call_run`. No `CALLE_API_KEY` yet for the backend HTTP path — see Blockers.
- **Live calls:** DISABLED (`CALLE_LIVE_CALLS_ENABLED=false`, `CALL_PROVIDER=fake`).
- **Real test status:** not yet attempted (ticket CS-062).

## AI state

- Provider: Gemini via the official unified Google GenAI SDK (`google-genai`, `from google import genai`), verified current on 2026-09-11.
- Model: from `GEMINI_MODEL` (`.env.example` default `gemini-3.5-flash`, unverified against a live model listing — CS-004 adds a startup availability check via `client.models.list()` and the default is confirmed then); never hardcoded in application code.
- Auth: supports `GEMINI_API_KEY` or Vertex AI with Application Default Credentials. Neither configured yet.

## Research state

`RESEARCH_PROVIDER=fixture|gemini_grounded` (`live` alias). Live provider is `GeminiGroundedResearchProvider` using the SDK's Google Search tool; grounding chunks expose URI + title only, so snippets are model answer text attributed via `grounding_supports` and labelled as such. Not yet exercised against the network (no credentials). `/health` reports `research_provider_effective` and `research_fallback_reason`.

## Tests

| Suite | State |
| --- | --- |
| Unit + integration (backend) | 357 passed — `backend/.venv/bin/python -m pytest` |
| End-to-end | not created (CS-060) |
| Lint (ruff) | All checks passed |
| Typecheck (mypy, strict) | Success: no issues found in 85 source files |
| Build (frontend) | not created (CS-050) |

Test suite blocks all socket connects via `tests/conftest.py`; runs with `CALL_PROVIDER=fake`, `CALLE_LIVE_CALLS_ENABLED=false`.

## Security state

Default-off call posture and secret handling are specified in `05_SECURITY_SAFETY.md` and encoded in `.env.example` defaults. `.gitignore` excludes `.env`, databases, logs and transcripts. No secrets, no real phone numbers and no PII are in the repository.

## Latest git commit

`6ceb28b docs: establish CallSwarm Phase 0 foundation` — pushed to `origin/main`.

## Context state log

### 2026-09-11 — Phase 0: documentation foundation

**Built.** Empty workspace initialized as a git repository. Two development subagents. Seven anchor documents plus `CLAUDE.md`, `STATUS.md`, `README.md`, `.env.example`, `.gitignore`, and a vendored CALL-E OpenAPI snapshot.

**Verified, not assumed.** CALL-E surfaces were checked against live official sources rather than prior assumptions: Python SDK `calle-ai` (`from calle import CalleClient`), API base `https://api.heycall-e.com` with `POST /v1/calls`, `GET /v1/calls/{call_id}`, `GET /v1/calls/{call_id}/events`, bearer auth via `CALLE_API_KEY`, `Idempotency-Key` header, MCP at `https://seleven-mcp-sg.airudder.com/mcp/openagent_oauth` exposing `plan_call` / `run_call` / `get_call_run`. Exact enums captured: `CallStatus queued|in_progress|completed|failed|canceled`; `RecipientStatus pending|in_progress|completed|failed|skipped`; `AttemptStatus queued|dialing|in_progress|completed|failed|canceled`; webhooks `call.completed|call.failed|call.result_validation_failed`. The `result_schema` feature restrictions (no `$ref`, `oneOf`, `anyOf`, `allOf`, recursion, or `additionalProperties: true`) are now a hard validator requirement in CS-033. A reusable-Goal API exists and is explicitly out of V1 scope. The unified `google-genai` SDK was confirmed current; the older `google-generativeai` is deprecated.

**Architecture decisions.** The Developer API, not MCP, is the primary CALL-E path, because a headless long-running orchestrator needs direct control of idempotency, polling and reconciliation, and the MCP endpoint is OAuth-interactive. Runtime agents are data (`AgentSpec` rows) executed by a generic runner, not Python classes — this is what makes the swarm genuinely dynamic and makes hardcoded domain agents structurally impossible. The model proposes; deterministic code owns state transitions, hard constraints and call priority scores. Only the Main Orchestrator mutates agent lifecycle; specialists emit an `AgentRequest` artifact instead of spawning.

**Must not be changed accidentally.** Default-off call posture; the dual gate (env switch plus `APPROVED` record); phone masking in the event emitter; the untrusted-input wrapper in the LLM provider; deterministic scoring in `calls/scoring.py`.

**Hackathon facts (verified 2026-09-11).** Submission deadline 2026-09-14 23:45 SGT. Required: a functional app using CALL-E's API, SDK, Skill or MCP; a pull request to `https://github.com/CALLE-AI/awesome-phone-call-agents` with the PR URL in the Devpost form; a public demo video under three minutes; the CALL-E account email. Judging is on Real World Impact, Quality of the Idea, Technical Implementation, and Product Experience & Demo, equally weighted. New accounts receive 20 free calls — live-call budget is scarce and is reserved for the demo, not the test loop. The user has directed that the plan is executed in full regardless of the deadline; scope is not reduced.

**Documentation review gate — FAIL then remediated.** The reviewer agent reviewed all eleven documents against the vendored OpenAPI spec and returned FAIL with 31 findings. All were addressed before any code was written. The ones that changed the design, not just the wording:

- *Reconciliation was fiction.* The docs claimed recovery from an ambiguous create via `GET /v1/calls/{call_id}` "or a metadata lookup". The API exposes no list or search endpoint, and after an ambiguous create we may hold no call id at all. Reconciliation is now defined as **identical replay under the same deterministic `Idempotency-Key`** — the only recovery the contract actually supports.
- *There is no remote cancel.* `cancel(call_id)` was specified for a path the API does not have. Replaced with `cancel_local`, which cancels only an unexecuted plan or an unfired job. An in-flight call cannot be recalled, and no UI control may imply it can.
- *The webhook was an evidence-injection hole.* The spec declares `POST /calle/webhook` unauthenticated, and its payload carries a full `CallTask` with `structured_result` — anyone reaching the URL could inject fabricated call evidence. No ticket implemented the receiver at all. Added CS-036: shared secret, `CALL-E-Event-Id` dedup persisted before side effects, correlation to a locally created run, and re-read of authoritative state before writing evidence.
- *Real dialing would have landed before the approval gate.* CS-032 depended only on CS-031, so a working `CalleProvider.execute` would have existed in the tree before CS-034 created any approval check. CS-032 now depends on CS-034 and enforces both gates **inside the provider itself**, independent of the approvals module.
- *Simulated results were indistinguishable from real evidence.* `EvidenceClaim.source_type` had no enumerated values, so a `FakeCallProvider` result became an ordinary `PHONE_SUPPORTED` claim — and all five offline scenarios run on fake calls. `source_type` is now `WEB | PHONE | USER | DERIVED | FIXTURE | SIMULATED`, propagating into every derived plan option and surfacing as a non-dismissible UI badge.
- *The generalization gate was gameable.* CS-061 did not say which LLM provider it runs under; against a scripted `FakeLLMProvider` a fully hardcoded agent factory would have passed every differentiation assertion. CS-061 now runs against a real provider or recorded cassettes, adds a sixth mission with no fixture directory, scans string literals and prompt templates for domain nouns, and ships a deliberately-hardcoded negative fixture the suite must fail.
- *`allow_booking_without_approval` was an invitation.* A `MissionSpec` field whose `true` value would contradict two hard rules. Deleted; an authority policy may only narrow what CallSwarm can do.
- *An undefined execution state was reachable.* `FINAL_EXECUTION_*` had no implementing ticket. Removed from the V1 transition table and recorded as deferred ticket CS-046. V1 ends at user acceptance; CallSwarm never books or buys.
- *The chain-of-thought guard covered only SSE.* Strategy descriptions, activity summaries, output artifacts and critic findings reach the client over plain REST. The guard is now a shared response sanitizer over every API response and over artifact persistence.
- *Missing entities.* `SuppressionEntry` was enforced by three tickets but modelled nowhere — and it must survive mission deletion, so it is explicitly excluded from the cascade. `AgentRequest`, `RecipientResult` and the `config/` package were likewise referenced but absent. `CallRun.provider_call_id` was renamed `calle_call_id`, because the spec's `provider_call_id` is attempt-level telephony data and would have been mis-mapped.
- *The worked example broke its own rule.* A required `quoted_price_inr` described as omittable would have voided the entire extraction under `additionalProperties: false`. Optional numerics are now paired with a required status enum, and CS-033's validator enforces it.
- *Smaller corrections.* Ticket dependency ordering (CS-010→CS-011, CS-032→CS-033, CS-060→CS-035/041/042/045); event pagination; `CALL_ALLOWED_RECIPIENTS` defined as a hard allow-list where empty means allow none; webhook env vars added; the approval endpoint given a typed decision body so a malformed POST cannot default to approval; `EXPIRED` added to the reviewer checklist; the base URL flagged as a placeholder in the vendored spec; the framework-versus-generated agent boundary stated explicitly so CS-013 and CS-061 are judgeable; commit authority assigned to the lead session.

**Remote.** `origin` = `https://github.com/tony19053000/CALLSPAWN.git`, supplied by the user after the foundation commit. Branch `main`.

**Review-gate economics (user directive, 2026-09-11).** The review loop was consuming too many tokens. Three changes, none of which weaken the gate: the `reviewer-tester` agent is pinned to Sonnet (`model: sonnet`) and escalates to Opus only for CS-032, CS-034, CS-036, CS-045, CS-061 and phase-boundary or documentation gates; the reviewer now works from `git diff` plus the ticket rather than re-reading the tree; and the product's own tests use recorded cassettes under `tests/cassettes/` for the one suite (CS-061) that needs a real model. The coder stays on Opus — implementation depth is worth paying for; re-verification breadth is not.

### 2026-09-11 — Phase 1: backend foundation (CS-001…CS-005)

**Built.** `backend/` Python 3.12 package, venv at `backend/.venv` (created with `uv` because Ubuntu's python lacks `ensurepip`). Pinned: fastapi 0.141.1, pydantic 2.13.5, sqlalchemy 2.0.52, google-genai 2.23.0, sse-starlette 3.4.11. Config, domain models, persistence, LLM provider, event bus, sanitizer, 89 tests.

**Decisions.** An unavailable Gemini model logs a STARTUP ERROR, reports `llm_model_status: "unavailable"` in `/health`, and makes generation raise `LLMModelUnavailable` — the process stays up so judges can see the error, rather than crashing. Reviewer judged this acceptable. All `inputs` to `generate_structured` are untrusted and wrapped; only `instruction` is trusted. Cascade delete is explicit code (`MISSION_SCOPED_TABLES`) so the `SuppressionEntry` exclusion is visible and tested, with `ondelete=CASCADE` FKs as backup. `AuthorityPolicy.calls_allowed` defaults `False`, `max_call_count` defaults `0` — missions opt in. Phone masking keeps country code + last 3 digits; requires leading `+`, so years and prices are untouched. `REASONING_LEAK_MARKERS` added as a setting; empty value falls back to defaults so a copied `.env` cannot disable the guard. `ScheduledJob` table created now so CS-045 has a home and the cascade covers it.

**Review.** First gate run on Sonnet per the cost policy. PASS, no findings. Reviewer independently ran `/health` with real-looking secrets and a distinctive DB filename and confirmed nothing leaked.

**Must not be changed accidentally.** `MISSION_SCOPED_TABLES` exclusion of `suppression_entries`; the three sanitizer choke points; `FakeLLMProvider` queue-only design (no branching on inputs); subscribe-before-replay ordering in `api/events.py`.

**Still unverified.** `gemini-3.5-flash` default against a live listing — no credentials yet.

### 2026-09-11 — Phase 2: mission intake and swarm design (CS-010…CS-014)

**Built.** The dynamic-swarm core. Nothing domain-specific exists in `agents/`, `strategies/` or `orchestrator/`, including string literals and prompt text; a static test enforces it and the reviewer confirmed it fails when a domain noun is injected.

**Decisions.** Authority policy is a structured request field, never inferred from goal text; the model may only narrow it (`clamp_authority`). State transitions validate against the *persisted* status so a forged in-memory status cannot skip a state; every authorization-skipping pair is asserted illegal. Transitions added beyond the `02` diagram, each grounded in `03` prose and reviewed: `CALL_PLAN_READY → REPLAN_DECISION_RUNNING`, `CALL_AUTHORIZATION_PENDING → REPLAN_DECISION_RUNNING`, `REPLAN_DECISION_RUNNING → {RESEARCH_RUNNING, SWARM_DESIGN_RUNNING, CALL_SELECTION_RUNNING}`, `MISSION_REVISION_RUNNING` re-entry points, `BLOCKED`/`CANCELED` from any non-terminal state, `PLAN_OPTIONS_READY → COMPLETE`. Intake fails closed: any `LLMError` during create or merge moves the mission to `BLOCKED` with a plain blocker and the API returns 502/503. Complexity score = hard constraints + distinct categories + strategies, mapped to caps 3/5/7/9; the cap is told to the model up front, one reduce request, then leaf truncation — the cap cannot be exceeded by any model output. Prohibited-purpose matching is keyword-based on name/role/objective/owns; its limits are known and the Critic (CS-044) is the second line. `AgentSpec` gained `owns` and `does_not_control`. Runner tests use a test-local name-routed provider because concurrent agents consume a shared queue nondeterministically; `FakeLLMProvider` itself stays queue-only.

**Review.** Sonnet, diff-scoped. One FAIL: `answer_questions` lacked the fail-closed wrapper that `create_mission` had. Fixed, two regression tests added, retest PASS.

**Must not be changed accidentally.** `TRANSITIONS` table and its illegal-pair assertions; `clamp_authority`; the static domain-noun test in `tests/test_factory.py`; the runner's rule that `orchestrator.request_agent` writes an `AgentRequest` and never spawns.

**Not yet wired.** No mission-level driver ties intake → strategies → factory → runner in one call; that is the Orchestrator loop, built as later phases supply research, calls and evidence.

### 2026-09-11 — Phase 3: research and information gaps (CS-020…CS-022)

**Built.** Research providers, the page fetcher, normalization, constraint filtering, and the gap engine. Only `scenarios/simple/research.json` exists as a fixture (domain-neutral); the four domain scenarios belong to CS-060.

**Decisions.** Fixture provenance is a fixed class attribute with no override parameter, so a fixture claim cannot be stamped `WEB` by any code path; dedup unions `source_types` per entity and never mutates a claim's `source_type`. Phone rule: only exact E.164 goes to `contact.phone_e164`; everything else lands verbatim in `attributes.raw_phone` — never reformatted, per the CALL-E server's own instruction. Absent attributes never exclude a candidate; they become `unverified_constraint_keys` and gaps. The knowledge table is computed from claims, not merged attributes, so dedup's first-value-wins attribute merge cannot hide a conflict. `identify_gaps` takes claims explicitly. Grounding is not combinable with JSON-schema output in the documented path, so the search call is plain text with extraction in code. Official docs now mark `generate_content` "Legacy" in favour of the Interactions API; stayed on `generate_content` for consistency with `GeminiProvider`, extraction isolated in one function for later migration.

**Review.** Sonnet, diff-scoped. FAIL 1: fetcher had no private-address refusal — a prompt-injected page could direct an agent to fetch `http://169.254.169.254/latest/meta-data/`. FAIL 2 after the first fix: the guard validated DNS answers but httpx re-resolved at connect time, leaving a rebinding window. Final design resolves once per hop, pins the validated IP into the URL, preserves `Host` and SNI (`httpcore` `_async/connection.py:107,151` confirmed), and the reviewer verified decimal/octal/hex/short IPv4 forms and trailing-dot names cannot bypass it. Retest PASS.

**Must not be changed accidentally.** `PublicPageFetcher.pin()` and the rule that every request in a hop uses the pinned target; `FixtureResearchProvider.source_type` as a class constant; the injection test proving an agent that read a hostile page still cannot create a `CallIntent`.

**Known limits, stated.** SSRF guard does not cover a compromised public host or an externally configured proxy. Live grounding untested against the network.

### 2026-09-11 — Phase 4a: call planning and authorization core (CS-030, CS-031, CS-033, CS-034)

**Built.** The authorization core. The reviewer actively attempted every bypass listed in the ticket — hand-built `AuthorizedPlan` against PENDING/REJECTED/expired/mismatched/nonexistent approvals, lowercase and extra-field decision bodies, empty allow-list under live semantics, direct `provider.execute` without approval — and none dialed.

**Decisions.** Schemas are generated only for *selected* intents, so rejected calls spend no model tokens; a selected intent whose schema fails is rejected with that reason. The fake provider still requires an `APPROVED` approval (so the full flow rehearses offline) but not the env switch or the allow-list — those are live-only gates. `APPROVED` past `expires_at` is treated as expired by both the gate and `authorize`. An in-provider gate refusal after `CALL_EXECUTION_RUNNING` moves the mission to `BLOCKED` — the pre-check makes this a race-only path; CS-032 may refine it for transient network errors. `ApprovalService.request` is not called automatically by `CallStrategy.plan`; the Orchestrator loop will. Structured result fields become `PHONE_SUPPORTED` claims (`"unknown"` values skipped) with `source_type` `SIMULATED` or `PHONE`; `summary`/`evidence[]` become `UNKNOWN`-status low-confidence claims. Quiet hours for a multi-zone country refuse if *any* zone is in quiet hours.

**Review.** Sonnet, diff-scoped, PASS. Two medium findings fixed before commit: `{"type": ["boolean","null"]}` crashed the validator with `TypeError` instead of a clean rejection; and `execute` was documented final but not enforced — now `@final` plus an `__init_subclass__` runtime guard — with a second `execute_call` on the same intent previously hitting an uncaught duplicate-PK error, now an idempotent no-op returning the existing run. A late test flake was wall-clock quiet hours (the default `SG` test region entered 21:00–09:00 during the session); `conftest.py` now picks a zone currently in daytime.

**Must not be changed accidentally.** `GatedCallProvider.execute` finality and its DB-reload of intent/approval/mission; gate order in `calls/gates.py`; "empty allow-list = allow none when live"; `CALL_PROVIDER=calle` hard failure without an implementation.

**Next.** Phase 4b — CS-032 `CalleProvider` against the Developer API (re-confirm base URL against live docs first), CS-035 call patterns, CS-036 webhook receiver. Review on Opus per the escalation rule.
