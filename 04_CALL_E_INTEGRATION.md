# 04 — CALL-E Integration

**Verified on 2026-09-11** against the official integrations repository, the hackathon rules page and the published OpenAPI contract (`CALL-E Developer API`, version `0.7.0`). A vendored copy of the spec is at `docs/vendor/calle.openapi.yaml`. Re-verify before any change to this layer; do not code from memory.

Sources: `https://github.com/CALLE-AI/call-e-integrations`, `https://docs.heycall-e.com/`, `https://docs.heycall-e.com/openapi/calle.openapi.yaml`, `https://call-e.devpost.com/rules`.

## Available surfaces

| Surface | Detail |
| --- | --- |
| Python SDK | `pip install calle-ai` → `from calle import CalleClient`; `client.calls.create_and_wait(task=..., result_schema=...)` |
| TypeScript SDK | `pnpm add @call-e/calle` |
| Developer API | Base `https://api.heycall-e.com`, `Authorization: Bearer $CALLE_API_KEY` |
| MCP | Streamable HTTP at `https://seleven-mcp-sg.airudder.com/mcp/openagent_oauth`, OAuth, tools `plan_call`, `run_call`, `get_call_run` (plus `track_ui_events`, telemetry only) |
| CLI | `npm install -g @call-e/cli` → `calle auth login`, `calle call plan|start|run|status|recover`, `calle mcp tools|call` |

### Installed locally on 2026-09-11

`@call-e/cli` is installed globally and authenticated; `calle auth status` reports `usable: true` with the token valid to 2029-06-05. `calle mcp tools` confirms the live tool list.

Verified tool parameters (from the live MCP server, not from documentation):

```text
plan_call     goal, to_phones, region, language, scheduled_at, plan_id,
              user_input, ttl_seconds, retry_confirmation_action
run_call      plan_id, confirm_token, ttl_seconds
get_call_run  run_id, cursor, limit
```

`plan_call` returns `plan_id`, `confirm_token` and `ready_to_run`; `run_call` consumes the first two. `get_call_run` is paginated by `cursor`/`limit`, matching the pagination rule already recorded for the events endpoint.

Two details worth keeping: the server explicitly instructs clients **not to guess a region or reformat an ambiguous phone number** — pass the user's raw message through `user_input` instead. CallSwarm follows this: a candidate phone number that is not unambiguously E.164 is an information gap, not a guess. And `calle call recover` exists precisely to resolve an uncertain `run_call` submission, which independently corroborates the reconciliation-not-retry rule below.

## Chosen integration path

**Primary: the Developer API over HTTP from the Python backend**, wrapped by `CalleProvider`, with the `calle-ai` SDK used where it cleanly maps.

Rationale: CallSwarm's orchestrator is a long-running server process that must create calls under its own authorization gates, poll or receive webhooks, and reconcile ambiguous state. That needs direct control over idempotency keys, status polling and error handling. The MCP path targets interactive agent environments (Claude Code, Codex, Cursor) and carries an OAuth flow that does not fit a headless server.

**Secondary: MCP is retained as a documented alternate path** and is exercised in the developer-facing skill/plugin usage described in the README, satisfying the hackathon's "API or SDK or MCP" requirement through the API/SDK route.

## Endpoints used

| Method | Path | Use |
| --- | --- | --- |
| `POST` | `/v1/calls` | Create a call task. Header `Idempotency-Key` (≤255 chars) makes retries safe — reusing a key with the same request returns the original call instead of dialing again. |
| `GET` | `/v1/calls/{call_id}` | Read status, structured result, summary, evidence, recipients, transcript references. |
| `GET` | `/v1/calls/{call_id}/events` | List developer events for live activity display. |
| `POST` | `/calle/webhook` (our endpoint) | Receive terminal webhooks: `call.completed`, `call.failed`, `call.result_validation_failed`. |

The vendored spec labels `https://api.heycall-e.com` a *placeholder* developer API base URL, so the base URL is re-confirmed against live documentation before CS-032 rather than trusted from the snapshot. `GET /v1/calls/{call_id}/events` is paginated (`cursor`, `limit`, default 50, max 100, response `next_cursor`); our provider must page rather than truncate.

A reusable-Goal API also exists (`/v1/goals`, `/v1/goals/{goal_id}/runs`). It is **out of scope for V1** and noted here only so it is not rediscovered as new.

## Request contract — `POST /v1/calls`

Required: `task` (natural-language instruction containing the goal, context the voice agent needs, and exactly what to collect).

Optional and used by CallSwarm:

- `recipients[]` — each `{ phones: ["+E164"], locale, region }`. `phones` must match `^\+[1-9]\d{6,14}$`. CallSwarm always passes recipients explicitly rather than embedding numbers in prose, so the authorization gate has a structured target to check.
- `result_schema` — JSON Schema for the task-level structured result.
- `recipient_result_schema` — per-recipient result, for fan-out calls.
- `metadata` — CallSwarm writes `{ mission_id, call_intent_id }` for reconciliation.
- `webhook_url` — our terminal webhook endpoint.

### `result_schema` constraints (from the spec, enforced by our validator before send)

Supported: `type`, `properties`, `required`, `enum`, nested `object`, simple `array.items`, `description`, `additionalProperties: false`.
**Unsupported: `$ref`, `oneOf`, `anyOf`, `allOf`, recursive schemas, complex `format` validation, `additionalProperties: true`.**

Field `description` values are passed to the extraction model and guide extraction; hard validation comes from `type`, `required`, `enum` and `additionalProperties`. Per the vendor's guidance, CallSwarm's schema generator prefers string enums over booleans for business decisions and **always includes an `unknown` value**, because "the call did not establish this" must be representable — that distinction is the difference between evidence and invention.

Reserved field names that must never be used as custom fields: `summary`, `status`, `transcript`, `call_id`, and timing fields. The spec states this constraint on `recipient_result_schema`; CallSwarm applies it to both schemas deliberately, and CS-033 records that the rule is intentionally broader than the vendor requires.

The Call Strategy agent generates a **call-specific** schema per intent. There is no universal result schema. Every generated schema is validated against the constraints above before the call is created, and every returned result is validated again before it becomes evidence.

A required field may never be one whose description implies it can be omitted: under `additionalProperties: false`, a missing required field invalidates the whole extraction and returns `structured_result: null`. Optional numeric answers are therefore paired with a required status enum, as below. CS-033's validator enforces this rule.

Example generated schema for a venue availability and pricing call:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["date_available", "quoted_price_status", "jain_option_available"],
  "properties": {
    "date_available": {
      "type": "string",
      "enum": ["yes", "no", "unknown"],
      "description": "Whether the venue is free on the requested date. Use unknown if staff could not confirm."
    },
    "quoted_price_status": {
      "type": "string",
      "enum": ["quoted", "refused", "unknown"],
      "description": "Whether staff gave a concrete total price. Use refused if they declined to quote over the phone, unknown if the call did not establish it."
    },
    "quoted_price_inr": {
      "type": "number",
      "description": "Total quoted package price in INR for the stated guest count. Present only when quoted_price_status is quoted."
    },
    "jain_option_available": {
      "type": "string",
      "enum": ["yes", "no", "unknown"],
      "description": "Whether Jain-compliant catering can be provided."
    },
    "decoration_included": {
      "type": "string",
      "enum": ["yes", "no", "unknown"],
      "description": "Whether decoration is included in the quoted price."
    },
    "evidence_summary": {
      "type": "string",
      "description": "One sentence quoting what staff actually said to support the answers above."
    }
  }
}
```

## Response contract — `CallTask`

Key fields consumed by CallSwarm: `id`, `status`, `task`, `recipients[]`, `structured_result`, `summary`, `task_completed`, `completion_confidence` (`{score 0–1, label}`), `evidence[]`, `metadata`, `failure_code`, `failure_message`, `created_at`, `completed_at`.

`structured_result` is `null` when CALL-E could not produce a schema-valid result, or when no schema was supplied. `null` is handled as an explicit outcome — the information gap stays `UNKNOWN` — never as a failure to paper over.

### Status enums (exact)

```text
CallStatus       queued | in_progress | completed | failed | canceled
RecipientStatus  pending | in_progress | completed | failed | skipped
AttemptStatus    queued | dialing | in_progress | completed | failed | canceled
```

`in_progress` includes post-call result finalization; terminal states publish only once the post-call outcome is available. Our `CallRun` mirrors these values rather than inventing parallel names.

### Webhook events (exact)

```text
call.completed | call.failed | call.result_validation_failed
```

The webhook's top-level `id` identifies the *event*; `data.id` is the call id. `call.result_validation_failed` is treated as "the call happened but produced no schema-valid result" — the transcript and summary may still be used as low-confidence evidence, clearly labelled as such.

## Execution model

CallSwarm does **not** inject instructions into a conversation that is already running. The official contract exposes no such capability, and claiming it would be false.

```text
CallSwarm reasons
   ↓  builds one complete, bounded call objective + result schema
CALL-E conducts the phone conversation, adapting naturally within that goal
   ↓  call reaches a terminal state
CallSwarm receives the structured result
   ↓  result becomes evidence in the Reality Graph
CallSwarm reasons again; the next call may be entirely different
```

This is sufficient for negotiation rounds, follow-ups, comparison, clarification, verification, escalation and multi-party workflows — the swarm reasons *between* call runs.

## Call patterns

Supported by the architecture and chosen dynamically by the Call Strategy agent — never all forced onto one mission:

**One-shot inquiry** · one recipient, one bounded goal.
**Fan-out** · several independent candidates, e.g. collecting quotes; maps onto `recipients[]` with `recipient_result_schema`.
**Cascade** · call the next candidate only when the previous one fails a condition.
**Negotiation round** · a prior quote becomes context in a later call task.
**Clarification** · resolve a contradiction or a missing detail.
**Verification** · independently confirm a high-impact claim.
**Follow-up** · a later scheduled call based on a stated commitment.
**Escalation** · move to a higher-level contact only where permitted.
**Human gate** · pause before a consequential next stage.

## Provider abstraction

`CallExecutionProvider` (in `backend/callswarm/calls/provider.py`):

```text
plan_call(intent)                     -> CallPlan       validate schema, resolve recipients, estimate cost
authorize(plan, approval)             -> AuthorizedPlan refuses without an APPROVED Approval record
execute(plan)                         -> CallRun        POST /v1/calls with a deterministic Idempotency-Key
get_status(calle_call_id)             -> CallRun
get_events(calle_call_id, cursor, limit=50) -> EventPage  paginated; follows next_cursor
get_result(calle_call_id)             -> structured result + summary + evidence + confidence
reconcile(intent)                     -> CallRun        idempotent replay; see below
cancel_local(intent)                  -> CallIntent     cancels a not-yet-executed plan or scheduled job only
```

**Reconciliation is replay, not retry.** The API exposes no list-or-search endpoint and no metadata query, so after an ambiguous create we may hold no `call_id` at all. The only recovery the contract supports is re-sending `POST /v1/calls` with a **byte-identical body and the identical deterministic `Idempotency-Key`**; CALL-E returns the original call task rather than creating a second one. The returned `CallTask.id` is then authoritative. No other recovery path is claimed.

**There is no remote cancel.** The API exposes no cancel or delete path for a call task; `CallStatus: canceled` is a state CALL-E may reach on its own. `cancel_local` therefore only cancels a plan that has not been executed, or a scheduled job that has not fired. **An in-flight CALL-E call cannot be recalled**, and no document, ticket or UI control may imply otherwise.

Implementations: `CalleProvider` (real) and `FakeCallProvider` (**default**). The fake provider returns clearly marked synthetic results, emits the same status sequence, and is used by every automated test. It never touches the network.

## Safety gates — both are required to dial

1. `CALLE_LIVE_CALLS_ENABLED=true` **and** `CALL_PROVIDER=calle` with a valid `CALLE_API_KEY`.
2. An `Approval` record in state `APPROVED` for that specific `CallIntent`, under a mission `authority_policy` that permits calls.

Both gates are checked in `approvals/` **and re-checked inside `CalleProvider.execute` itself**, as a last line of defence — a caller that bypasses the approvals module still cannot dial. `CALL_ALLOWED_RECIPIENTS` is an additional hard allow-list evaluated on top of the suppression deny-list: when `CALLE_LIVE_CALLS_ENABLED=true`, **an empty allow-list permits no recipient at all.** It exists so live testing cannot escape a known set of consenting numbers.

Configured credentials are not permission to dial. Neither is a public phone number. Additional enforcement:

- Per-mission call budget (`CALL_MAX_PER_MISSION`) checked at plan time and again at execute time.
- Quiet-hours window, evaluated in the recipient's region.
- Recipient suppression / do-not-contact list, checked at execute time including for scheduled jobs.
- Every create call carries a deterministic `Idempotency-Key` derived from `call_intent_id`. An ambiguous create is **recovered by identical replay under the same key, never by a fresh retry.**
- Phone numbers are masked (`+91 ••••• ••210`) in all logs, activity events and ordinary UI. Full numbers live only in the database and in the request to CALL-E.
- No real phone number is committed to the repository or to fixtures.

## Webhook receiver

`POST /calle/webhook` on our side is declared `security: []` in the vendored spec — unauthenticated by construction — and its payload carries a full `CallTask` including `structured_result` that would otherwise flow straight into the Reality Graph. Anyone who can reach the public URL could inject fabricated call evidence. The receiver (ticket CS-036) therefore:

- accepts only requests carrying the shared secret configured in `CALLE_WEBHOOK_SECRET` (unguessable path token plus constant-time comparison, upgraded to signature verification if CALL-E publishes one);
- reads the required `CALL-E-Event-Id` header and **persists it before performing any side effect**, so duplicate deliveries are ignored safely;
- rejects any payload whose `data.id` does not match a `CallRun` this instance created, or whose `data.metadata.mission_id` and `call_intent_id` do not correlate to that run;
- treats an accepted payload as a *notification*, then re-reads the authoritative state via `GET /v1/calls/{call_id}` before writing evidence.

`call.result_validation_failed` is handled as "the call happened but produced no schema-valid result".

## Testing policy

Default test mode is `CALL_PROVIDER=fake`, `CALLE_LIVE_CALLS_ENABLED=false`. A test that would place a real call fails the suite by design — the reviewer agent checks for this explicitly. Live calls during development or demo use only numbers the operator owns or has consent to call. Twenty free calls ship with a new account, so live-call budget is scarce and is spent on the demo, not on the test loop.
