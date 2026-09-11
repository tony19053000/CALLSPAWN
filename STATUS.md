# STATUS — CallSwarm

_Last updated: 2026-09-11_

## Overall completion

**5%** — Phase 0 (documentation foundation) complete. No application code written yet.

## Current phase

Phase 0 → Phase 1 (Foundation, ticket CS-001).

## Current ticket

`CS-001 — Backend skeleton and configuration` (PENDING).

## Completed

- Repository initialized, committed and pushed to `origin/main` (`https://github.com/tony19053000/CALLSPAWN.git`).
- Development subagents created: `.claude/agents/coder.md`, `.claude/agents/reviewer-tester.md`.
- Anchor documents written: `01_PRD.md`, `02_ARCHITECTURE.md`, `03_SWARM_ORCHESTRATION.md`, `04_CALL_E_INTEGRATION.md`, `05_SECURITY_SAFETY.md`, `06_FRONTEND_SPEC.md`, `07_FEATURE_TICKETS.md`.
- `CLAUDE.md`, `STATUS.md`, `README.md`, `.env.example`, `.gitignore`.
- CALL-E integration surface verified against live official sources; OpenAPI contract vendored at `docs/vendor/calle.openapi.yaml`.

## In progress

Nothing.

## Pending

All 36 active tickets in `07_FEATURE_TICKETS.md` (CS-001 … CS-064). CS-046 is a 37th, explicitly deferred out of V1.

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

`RESEARCH_PROVIDER=fixture`. Live provider selection is deferred to ticket CS-020, where the current official search-grounding options are verified before implementation.

## Tests

| Suite | State |
| --- | --- |
| Unit | not created |
| Integration | not created |
| End-to-end | not created |
| Lint (ruff) | not configured |
| Typecheck (mypy) | not configured |
| Build | not configured |

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

**Next.** CS-001 — backend skeleton and configuration.
