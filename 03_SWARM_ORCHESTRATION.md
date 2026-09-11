# 03 — Swarm Orchestration

## The Main Orchestrator

The Main Orchestrator is the brain of CallSwarm, not a router. Its purpose:

> Understand the user's real goal and dynamically design the intelligence required to solve it.

Responsibilities, in order of use:

1. Interpret the user goal.
2. Detect critical missing context.
3. Ask the minimum set of clarification questions that would change the plan.
4. Produce a validated `MissionSpec`.
5. Generate plausible solution strategies **before** generating any specialist.
6. Identify the important trade-offs those strategies imply.
7. Determine what expertise is actually required.
8. Generate `AgentSpec`s — avoiding redundancy and avoiding unnecessary agent count.
9. Assign dependencies and decide what may run in parallel.
10. Start, pause and stop agents; monitor their outputs.
11. Detect new strategies implied by fresh evidence; create a specialist when justified; retire obsolete ones.
12. Update strategy when the user changes constraints, preserving reusable evidence.
13. Coordinate the final review and present options.

**More agents is not a better result.** A simple mission may justify only a Research Specialist, a Call Specialist and a Reviewer. A complex one may justify ten. Agent count follows mission complexity, and the reviewer tests for exactly that.

## Clarification loop

The Orchestrator asks a question only when a missing answer would materially change the strategy set, the constraint check or the call plan. Each question carries the decision it unblocks. Answers are merged into the `MissionSpec` and re-validated. Unanswered non-critical questions become recorded assumptions, surfaced in the final plan.

## Strategy generation

For a sufficiently complex mission, the Strategy Architect produces several `StrategyCandidate`s that differ in **objective or assumption**, not in wording. Typical axes: cost-first, quality-first, simplicity/risk-first.

Example for `Build gaming PC under ₹1.2 lakh`:

1. Cheapest reputable individual suppliers per component.
2. CPU/motherboard/RAM bundle plus GPU sourced separately.
3. One local complete-system vendor.
4. Hybrid local plus online.
5. Prebuilt comparison.

Strategies are pruned by research and revived by evidence. If a call reveals a vendor offering a GPU + CPU + motherboard bundle at ₹8,000 off, the Orchestrator may create a `Bundle Analysis Specialist` and change the active plan. The runtime must support this at any point, not only before research.

The Critic compares surviving strategies. There is no simulated debate for show — each proposal must differ substantively, and rationale is emitted as concise structured text, never as hidden reasoning.

## Framework components versus generated specialists

This line decides what the generalization tests police, so it is explicit.

**Fixed framework components** — Main Orchestrator, Strategy Architect, Call Strategy, Evidence Engine, Optimizer and Critic. These are code, not `AgentSpec` rows. They are mission-independent machinery and are always present.

**Generated specialists** — every domain role, without exception: the venue analyst, the compatibility checker, the practice-area matcher, the web-presence auditor. These exist only as `AgentSpec` rows produced by the agent factory for one mission.

The prohibition on hardcoding, and the CS-061 differentiation and domain-noun checks, apply to the **generated-specialist set**. A framework component appearing in every mission is correct; a domain specialist appearing in every mission is a defect. The UI labels the two groups distinctly.

Where `03` or `06` shows a card list containing Orchestrator, Research, Call Strategy or Reviewer, those are framework components; the Venue Analyst and Negotiator beside them are generated for that mission.

## Dynamic specialist generation

There is no permanent list of domain agents. `Venue Agent`, `GPU Agent` and `Legal Agent` exist only when a mission justifies them.

Every generated `AgentSpec` must answer:

- Why does this agent exist?
- What exact problem does it own?
- What evidence does it need?
- Which tools may it use?
- What output must it return, in what schema?
- What does it *not* control?
- When can it stop?

Specs with overlapping responsibility are rejected and merged unless the overlap is explicitly justified.

## Agent contracts

Agents never message each other freely. Each run receives its `required_inputs` as validated artifacts and returns one artifact matching `expected_output_schema`. The Orchestrator routes artifacts along the dependency graph. This keeps the swarm auditable and makes fake activity structurally impossible.

## Dependency graph and parallelism

The Orchestrator builds a DAG from `AgentSpec.dependencies`. Agents whose inputs are satisfied run concurrently; the rest sit in `WAITING_FOR_DEPENDENCY`. Call-bound agents sit in `WAITING_FOR_CALL` and wake on `CALL_RESULT_RECEIVED`.

## Replanning

Triggers: new evidence that contradicts a plan assumption, a call result that changes cost or availability, a strategy becoming impossible, a critic FAIL, or a user constraint change.

On a trigger the Orchestrator decides among: rerun an existing agent, create a new specialist, stop an obsolete agent, revive or prune a strategy, request another research pass, open another authorized call round, or proceed to optimization.

## User constraint updates

Mission state persists. When the user says "bring it under ₹3.2 lakh but keep photography unchanged", the Orchestrator updates `max_budget = 320000` and marks the photography component `LOCKED`.

Research, call evidence and verified vendor information are **not** discarded. Only derived artifacts that depend on the changed constraint are marked stale, and only the agents that own them rerun. This incremental invalidation is essential and is directly tested.

## Agent termination

An agent is stopped when its strategy becomes impossible, its work becomes redundant, the user changes the mission, a better strategy replaces it, or its output is no longer relevant. Example: a `Prebuilt PC Strategy` agent stops because no evaluated prebuilt meets the performance requirement. The UI shows this honestly, with the reason.

## Termination conditions for the mission

`COMPLETE` when a reviewed plan option is accepted or presented with no remaining high-impact unknown and no pending authorization. `BLOCKED` when a required authorization, credential or piece of evidence cannot be obtained — with the blocker stated plainly. `CANCELED` on user request, which also cancels scheduled jobs.

## Activity events — no chain-of-thought

Transparency means **visible actions, evidence and outputs** — not a private reasoning transcript.

Good:

```text
Research Agent is comparing 12 shortlisted venues.
Budget Agent recalculated the Balanced plan after a new ₹105,000 venue quote.
Call Strategy selected 3 of 9 possible calls; the other 6 were unlikely to change the decision.
```

Never emitted: raw reasoning tokens, step-by-step private model thoughts, internal prompt contents.

## Error recovery

A schema-invalid agent output is retried once with the validation error appended, then the agent is marked `FAILED` and the Orchestrator decides whether to replace it. Provider timeouts are retried with backoff. An ambiguous call creation is **never** blindly retried — it is reconciled through the provider's status endpoint first.
