# 01 — Product Requirements Document

## Product

**CallSwarm**

## One-line statement

CallSwarm is an autonomous real-world planning and phone-work system that turns a user's high-level goal into a dynamically generated team of AI specialists that research, reason, selectively call, verify, negotiate, replan and optimize the best complete outcome.

## The problem

AI agents are strong inside digital systems. Many real-world decisions still depend on information that exists only outside structured APIs: current local availability, unpublished prices, bundle discounts, present service capacity, negotiated terms, appointment slots, supplier constraints, intake requirements, business-specific policy, current stock, verbal confirmation, follow-up status.

A normal assistant can search, summarize and recommend, but it cannot close an information gap that requires a phone conversation.

A simple phone agent can dial, but a human or developer must still decide whom to call, why, when, what to ask, how many calls are justified, how each result changes the larger goal, whether a second round is worth it, whether alternatives should be reconsidered, and whether the assembled solution is still the best one.

CallSwarm solves that larger problem. **The user describes an outcome. CallSwarm designs the work.**

## Core principle: strategy-first, not agent-first

CallSwarm must not convert `Build me a PC` directly into `CPU Agent`, `GPU Agent`, `RAM Agent`.

It first reasons about *how the goal could actually be solved* — every part from separate cheapest vendors, selected bundles, one local system builder, a hybrid, a prebuilt comparison, trading a small saving for unified warranty — and only then decides what expertise the mission needs.

The task determines the swarm. The swarm never determines the task.

## What CallSwarm must not be

A chatbot with a Call button. A fixed set of hardcoded agents. A bulk dialer. A single-domain comparison tool. A fake swarm visualization. A pre-scripted demo. An LLM wrapper around CALL-E. A system that calls every candidate it discovers. A system whose agents exist only for visual effect.

## Target users

Primary: individuals handling complex purchases or plans, founders, small businesses, operations and procurement teams, freelancers, event planners, local-business operators — anyone who regularly compares or coordinates offline providers.

Secondary: developers experimenting with autonomous agents, teams building phone-enabled research workflows, agencies doing authorized lead qualification, service businesses coordinating vendors.

## Core user journey

1. The user describes a complex real-world goal in natural language.
2. The Main Orchestrator interprets the goal and identifies critical missing information.
3. It asks only the clarification questions that would change the plan.
4. It produces a structured, validated `MissionSpec`.
5. The Strategy Architect generates several competing solution strategies.
6. The Orchestrator determines what expertise those strategies require and generates a mission-specific swarm. Every created agent becomes visible in the UI.
7. The Research layer investigates everything available digitally, preserving provenance.
8. Domain specialists analyze findings and declare explicitly what is `KNOWN`, `UNKNOWN` and `CONFLICTED`.
9. The Call Strategy agent turns important unknowns into candidate `CallIntent`s; deterministic value-of-information scoring ranks them and rejects the rest.
10. Authorized calls are planned and executed through CALL-E as self-contained phone missions.
11. Structured call results become evidence in the Reality Graph.
12. Affected agents react. The Orchestrator may replan, create a new specialist, stop an obsolete one, change strategy, or open another authorized call round (negotiation, clarification, verification, follow-up).
13. The Optimizer assembles complete solution alternatives under code-enforced hard constraints.
14. The Critic challenges them and returns PASS or FAIL. Failures return into the swarm.
15. Passing options are presented with cost, components, evidence, trade-offs and unresolved uncertainty.
16. The user may approve, change constraints, lock a component or reject a vendor. Existing mission evidence persists; only affected derived work is invalidated and recomputed.
17. Consequential execution requires explicit user authority. There is no policy field that can waive it: an authority policy may only *narrow* what CallSwarm is allowed to do, never remove the approval requirement for a consequential action.
18. CallSwarm completes the mission or clearly reports the unresolved blockers.

## Reference missions (test scenarios, never framework assumptions)

**A — Anniversary.** "Plan my parents' 24th marriage anniversary." Clarify location, date, guests, budget, food preferences, style, priorities, negotiation preference, execution authority. Competing strategies: all-inclusive hotel package; venue plus independent catering; restaurant private dining; inexpensive venue plus premium experience suppliers. Possible dynamic agents: Venue Package Analyst, Catering Analyst, Experience Analyst, Photography Analyst, Budget Optimizer, Negotiation Specialist.

Example `MissionSpec`:

```json
{
  "goal": "Plan parents' 24th marriage anniversary",
  "guest_count": 80,
  "max_budget": 400000,
  "currency": "INR",
  "food_preferences": ["vegetarian", "Jain options"],
  "style": "elegant family celebration",
  "priority_order": ["food", "venue", "photography"],
  "allow_research": true,
  "allow_calls": true,
  "allow_negotiation": true,
  "max_calls": 5
}
```

**B — Gaming PC under ₹1.2 lakh.** No sourcing method may be assumed. Reasoning must weigh compatibility, delivery, warranty, RMA complexity, assembly, support, bundle discounts, stock, deadline, performance, upgrade path and total effective cost. A local vendor ₹3,000 more expensive can be the better solution when assembly is included, delivery is immediate, warranty handling collapses to one counter and a negotiated bundle closes most of the gap. Lowest-price ranking must not replace mission reasoning.

**C — Professional service.** "Find me a suitable law firm for this business matter." Possible agents: Legal-Service Research Specialist, Firm Discovery Specialist, Practice-Area Matcher, Reputation Analyst, Consultation Availability Specialist. Calls ask only intake and logistical questions — does the firm handle this category, are new clients accepted, consultation fee, appointment availability, remote consultation. CallSwarm never provides legal advice.

**D — Authorized business prospecting.** "Find businesses that may genuinely benefit from our web-development services and qualify appropriate leads." This is market discovery, web-presence analysis, lead scoring, selective authorized outreach, qualification and follow-up — not calling every business without a website. A business with no site but strong reviews, heavy foot traffic, no booking system and an active social presence scores high; a business with no standalone site but an excellent third-party booking funnel scores low. A public phone number is not authorization: outreach obeys user authorization, CALL-E terms, local law, calling hours, AI-disclosure requirements and do-not-call state. Hackathon testing uses consenting recipients only.

**E — Simple task.** A mission that justifies a small swarm and a single call. This scenario exists to prove CallSwarm does not always build the same swarm.

## MVP scope (V1 must prove)

Natural-language mission creation; intelligent clarification; structured `MissionSpec`; multiple strategy generation; dynamic agent generation; visible real agent activity; a web research layer; candidate discovery and filtering; information-gap detection; deterministic call-value scoring; CALL-E planning and execution; real structured call results; evidence ingestion; replanning after a call; at least one second-round call driven by prior evidence; whole-mission optimization; a critic gate; final solution alternatives; user constraint modification without a full reset; human approval before consequential actions.

V1 does not claim universal support for arbitrary real-world tasks. The architecture is general; the proof runs through the controlled scenarios above.

## Non-goals for V1

Autonomous medical diagnosis, legal advice, financial trading, credential or OTP collection, emergency services, high-stakes eligibility decisions, deceiving or impersonating humans, debt collection, political persuasion, and irreversible financial commitment without explicit approval.
