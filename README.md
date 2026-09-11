# CallSwarm

**CallSwarm turns a high-level real-world goal into a dynamically generated team of AI specialists that research, reason, selectively call real people, verify what they hear, negotiate, replan and optimize the best complete outcome.**

You describe an outcome. CallSwarm designs the work.

## Why it exists

Modern AI agents are strong inside digital systems, but many real decisions still depend on things that only exist offline: today's availability, an unpublished price, whether a bundle discount is possible, whether a firm takes new clients, whether the part is actually in stock.

A normal assistant can search and summarize, but it cannot close a gap that needs a phone conversation. A simple phone agent can dial, but someone still has to decide whom to call, why, what to ask, how many calls are justified, and what each answer changes.

CallSwarm does that deciding. It makes phone calls a *strategic resource*, not a feature.

## How it works

1. **Understand** — the Main Orchestrator interprets the goal and asks only the questions that would change the plan.
2. **Strategize** — it generates competing solution strategies *before* designing any agents. The task determines the swarm; the swarm never determines the task.
3. **Design the swarm** — mission-specific specialists are generated as data, each with a stated purpose, bounded tools and a validated output schema.
4. **Research** — everything available digitally is gathered first, with provenance, so no call is wasted on a published fact.
5. **Decide what is worth a call** — deterministic value-of-information scoring ranks candidate calls and rejects the rest, with reasons. Nine possible calls may become three.
6. **Call** — CALL-E conducts each call as a self-contained phone mission and returns a structured result.
7. **Absorb reality** — results become traceable evidence. Conflicts are kept as conflicts. A phone statement is recorded as *what was said*, not as proven fact.
8. **Replan** — a surprising quote can spawn a new specialist, revive a discarded strategy or retire an obsolete agent.
9. **Optimize and critique** — complete solutions are assembled under code-enforced constraints, then challenged by a Critic before a human ever sees them.
10. **Stay in your control** — consequential actions require explicit approval, and changing a constraint later preserves everything already learned.

## What it is not

Not a chatbot with a Call button. Not a fixed set of hardcoded agents. Not a bulk dialer. Not a fake swarm animation. It does not call every candidate it finds.

## Safety posture

Live calling is **off by default**. Dialing requires both an explicit environment switch and an approved authorization record for that specific call. A publicly listed phone number is not consent. Phone numbers are masked everywhere except the database and the call request. Web pages and call transcripts are treated as untrusted data, never as instructions. Details in [05_SECURITY_SAFETY.md](05_SECURITY_SAFETY.md).

## Stack

Python 3.11+ · FastAPI · Pydantic v2 · SQLAlchemy · Gemini via the unified `google-genai` SDK · CALL-E Developer API · Next.js · TypeScript · Tailwind CSS.

## Status

Under active development. See [STATUS.md](STATUS.md) for the current phase, and [07_FEATURE_TICKETS.md](07_FEATURE_TICKETS.md) for the work breakdown.

## Documentation

| Document | Contents |
| --- | --- |
| [01_PRD.md](01_PRD.md) | Product requirements, user journey, reference missions, MVP scope |
| [02_ARCHITECTURE.md](02_ARCHITECTURE.md) | Stack, data models, runtime agent model, mission state machine |
| [03_SWARM_ORCHESTRATION.md](03_SWARM_ORCHESTRATION.md) | Orchestrator, strategy generation, dynamic agents, replanning |
| [04_CALL_E_INTEGRATION.md](04_CALL_E_INTEGRATION.md) | Verified CALL-E surfaces, contracts, call patterns, safety gates |
| [05_SECURITY_SAFETY.md](05_SECURITY_SAFETY.md) | Credentials, authorization, outreach rules, untrusted input |
| [06_FRONTEND_SPEC.md](06_FRONTEND_SPEC.md) | Workspace UI, swarm graph, evidence browser, call UI |
| [07_FEATURE_TICKETS.md](07_FEATURE_TICKETS.md) | Executable ticket breakdown |
| [CLAUDE.md](CLAUDE.md) | Development process and non-negotiable rules |

## License

To be determined before public release.
