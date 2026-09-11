# 05 — Security and Safety

CallSwarm acts in the physical world: it calls real people and can influence real money. These rules are product requirements, not suggestions. The `reviewer-tester` agent fails any change that weakens one.

## Credentials

All secrets live in environment variables, loaded server-side only: `GEMINI_API_KEY`, `CALLE_API_KEY`, `SEARCH_API_KEY`, `DATABASE_URL`. The Next.js frontend holds none of them; only `NEXT_PUBLIC_API_BASE_URL` is exposed. `.env` is git-ignored, `.env.example` carries empty placeholders. Secrets never appear in logs, activity events, API responses or error messages. A secret accidentally committed is treated as compromised and rotated, not merely removed.

## Phone numbers and PII

Numbers are stored in E.164 form in the database and are transmitted only to CALL-E. Everywhere else — logs, `ActivityEvent`s, SSE payloads, UI, exports, test fixtures — they appear masked. Fixtures and the repository contain no real phone numbers; documentation uses placeholders such as `<E164_PHONE>`. Transcripts may contain third-party personal information: they are stored against the mission, are never used to enrich unrelated missions, and are deletable with the mission.

## Authorization model

Mission `authority_policy` fields: research allowed, calls allowed, maximum call count, negotiation allowed, scheduled follow-up allowed, confirmation calls allowed.

The policy can only **narrow** what CallSwarm may do. There is deliberately no field that grants consequential action without approval — no `allow_booking_without_approval` or equivalent may be added, because a coder would then be invited to honour `true`. A consequential action always requires an `APPROVED` record, and a test asserts that no code path can perform one without it regardless of policy contents.

Authorization state is an explicit record: `PENDING`, `APPROVED`, `REJECTED`, `EXPIRED`. It is never inferred from conversational phrasing. "Sure, go ahead" in chat does not authorize a booking; the user must act on the approval control for that specific action. High-impact actions must not depend on inferred approval.

## Real calls

Live calling is off by default (`CALLE_LIVE_CALLS_ENABLED=false`, `CALL_PROVIDER=fake`). Dialing requires both the environment switch and an `APPROVED` approval for that intent. Enforced additionally: per-mission call budget, quiet-hours window evaluated in the recipient's region, a persisted do-not-contact list checked at execute time, deterministic idempotency keys, and reconciliation instead of blind retry. Canceling a mission cancels its scheduled and not-yet-executed calls. It **cannot recall a call already handed to CALL-E** — the API exposes no cancel path — and no UI control may imply that it can.

## Automated outreach

A publicly listed phone number is not consent to be called by an automated system. Production outbound outreach must obey the user's authorization, CALL-E's terms, applicable local law, calling-hour rules, AI-disclosure requirements, opt-out and do-not-call state, and jurisdiction-specific constraints. CallSwarm discloses that the call is made by an AI assistant on behalf of the named user or organization wherever disclosure is required, and honors an opt-out immediately by writing the recipient to the suppression list.

No deceptive outreach. No impersonation of a human or of another organization. No spam. Hackathon testing dials only consenting, authorized test recipients — never businesses discovered by research.

## High-risk boundaries

CallSwarm V1 does not autonomously provide medical diagnosis or legal advice, trade financial assets, request passwords, OTPs or PINs, contact emergency services, make high-stakes eligibility decisions, deceive recipients, impersonate humans, perform debt collection, conduct political persuasion, or make irreversible financial commitments without explicit approval.

Finding a law firm is allowed; giving legal advice is not. Finding a doctor is allowed; diagnosing is not. These boundaries are enforced by a policy check on every generated `AgentSpec` and every `CallIntent` before execution, not by prompt wording alone.

## Web research is untrusted input

Retrieved pages, reviews and listings are **data, never instructions**. Site text may not override system rules, authorize a call, expand an agent's tool set, reveal secrets, modify mission policy, force agent creation or trigger any side effect. Research content is passed to the model inside a clearly delimited, explicitly labelled untrusted block, and any instruction-like content found in it is ignored and may be logged as a prompt-injection attempt.

## Call results are untrusted input too

Whoever answers a phone may say anything. A structured extraction does not make a statement true. Phone evidence records *what was stated on the call*, not objective fact, and is stored as `PHONE_SUPPORTED` — never as verified truth. Confidence rises only through independent sources, written confirmation, internal consistency or a clarification call. A quoted price is a claim until it is confirmed in writing, and the UI says so.

## Fabrication controls

No hardcoded demo outputs. No fixture data presented as live research. Claims originating from `FixtureResearchProvider` or `FakeCallProvider` carry `source_type` `FIXTURE` or `SIMULATED`, which propagates into every derived plan option and is rendered as a persistent badge — the five offline scenarios run on simulated calls, so this marker is what keeps a rehearsal from looking like a real result. No synthetic agent cards or activity lines — every one traces to a persisted `AgentSpec` and `ActivityEvent`. No fabricated savings: a saving is computed from two compared, sourced figures or it is not claimed. No "verified" label without a corroborating source. Optimizer results are worded as the best evaluated solution under the researched candidate set, never as a global optimum.

## No chain-of-thought exposure

Private model reasoning, raw reasoning tokens and internal prompt contents are never returned by the API or shown in the UI. Enforcement is a shared response sanitizer applied to every API response and to the persistence of `AgentRun.output_artifact` — not only to activity events, since strategy descriptions, activity summaries, output artifacts and critic findings all reach the client over plain REST. Agents publish concise, factual activity summaries and their validated output artifacts.

## Data retention and deletion

Deleting a mission deletes its candidates, research artifacts, call runs, transcript references, evidence and scheduled jobs. The CALL-E API supports a no-store header on Goal endpoints; if an equivalent control applies to a call path we use, it is honored for missions marked sensitive.

## Dependency and supply-chain hygiene

Dependencies are pinned. New dependencies require justification in the ticket. No package is added merely to satisfy a single small helper.
