# 06 — Frontend Specification

CallSwarm should feel like a premium AI operations workspace: calm, dense, factual. Not a chat toy, not a dashboard of decorative motion. Every pixel that implies activity must be backed by a real runtime event.

Original CallSwarm identity — no pixel-copying of another product. Desktop is primary; dark theme is primary, with light mode supported where practical. Layout stays usable down to a narrow window.

## Three-panel workspace

### Left — Live Swarm

Shows **only agents that actually exist** for the current mission, sourced from persisted `AgentSpec` and `AgentRun` rows.

Each card: agent name, one-line role, status dot, current activity line, dependency/waiting state, completion state.

```text
● Orchestrator          Evaluating new venue quote
● Research Agent        42 venues analyzed
● Venue Analyst         Comparing 5 finalists
● Budget Optimizer      Recalculating
● Call Strategy         Selecting second-round calls
● Negotiator            Waiting for pricing evidence
○ Reviewer              Waiting
```

Framework components (Orchestrator, Research, Call Strategy, Optimizer, Critic) and mission-generated specialists are visually distinguished, so a viewer can see which part of the swarm the mission actually produced. Stopped agents remain visible with the reason they were stopped — an honest swarm shrinks as well as grows. Clicking a card opens it in the inspector.

### Center — Mission feed

Where the user talks to the Orchestrator: the initial goal, clarification answers, constraint changes, approvals.

```text
Plan my parents' anniversary under ₹4 lakh.
Food matters more than decoration.
Do not call more than five venues.
Keep this photographer but reduce total cost.
```

The feed also carries major mission events only: mission understood, clarification needed, swarm created, research complete, calls selected, call result received, strategy changed, review failed or passed, options ready. Low-level agent chatter stays in the left panel and inspector — the center never floods.

### Right — Agent and evidence inspector

Contextual detail for whatever is selected.

```text
RESEARCH AGENT
Status      Working
Objective   Find viable event venues.
Progress    47 discovered · 19 analyzed · 7 shortlisted
Action      Reviewing package inclusions
```

```text
CALL STRATEGY
Possible calls   9
Selected         3
Avoided          6
Reason           Low expected decision impact
```

```text
CALL-E
Status      IN CALL
Recipient   Royal Garden  ·  +91 ••••• ••210
Purpose     selected-date availability · package price · Jain food · decoration inclusion
Elapsed     01:18
```

```text
BUDGET OPTIMIZER
Budget           ₹4,00,000
Preferred plan   ₹3,54,500
Remaining        ₹45,500
Open uncertainty DJ availability
```

## Top-level views

`Chat` · `Swarm` · `Mission` · `Evidence`

**Mission** shows the goal, constraints, permissions, call budget, priorities and the active strategy, each editable where the backend supports revision.

**Evidence** is the Reality Graph browser.

## Swarm graph

A real dependency graph, not decoration. Candidate library: `@xyflow/react` — verify the current package and version before adding it.

Nodes: Orchestrator, specialist agents, research artifacts, call intents, call results, evidence, optimizer, reviewer. Edges represent real dependencies and real artifact flow. No animated links that mean nothing. When evidence arrives, the dependent nodes visibly change state because the runtime actually transitioned them.

## Agent visibility rule

Every visible agent corresponds to a real runtime `AgentSpec`. Every activity line comes from a real `ActivityEvent`. `Analyzing…` is never rendered unless the runtime emitted that state. This rule is tested, not trusted.

## Call UI

While a call is active: recipient display name, masked number, call goal, the fields being collected, status, elapsed time, current high-level activity, and transcript or event activity **only where CALL-E actually provides it**. On completion: the structured result, the summary, the confidence label and the evidence items. Nothing in this panel implies a capability CALL-E does not expose — there is no "steer the live call" control and no "cancel this call" control, because neither is a real capability. A call already handed to CALL-E cannot be recalled, and the UI says so plainly.

## Reality Graph UI

Every important fact is inspectable down to its sources.

```text
Venue price   ₹105,000
  ✓ CALL #12 — quoted ₹115,000
  ✓ CALL #19 — negotiated ₹105,000
  Status  PHONE SUPPORTED   Freshness  today
```

```text
Warranty
  Website  2 years
  Phone    3 years
  ⚠ CONFLICTED
```

Conflicts are displayed, never auto-resolved. Web-sourced and phone-sourced facts are visually distinct.

Any claim whose `source_type` is `FIXTURE` or `SIMULATED` carries a persistent, non-dismissible **SIMULATED** badge, and so does every plan option derived from one. Offline scenario runs must be unmistakable as rehearsals at a glance.

## Final plan UI

Two or three meaningful options, named to fit the mission (`Lowest Cost`, `Best Overall`, `Premium` are defaults, not fixed labels).

Each option shows total cost, components, selected candidates, the evidence behind each figure, trade-offs, unresolved uncertainty, which facts are phone-verified versus web-researched, which user constraints it satisfies, and why it is recommended. Savings are computed from compared sourced figures — never invented.

## Real-time transport

REST for commands; Server-Sent Events on `/api/missions/{id}/events` for live activity. The client reconnects with a last-event id and replays missed events from the persisted event log, so a refresh never loses mission history.

## Authentication

A clearly labelled local demo session until real auth is configured; Firebase Authentication with Google Sign-In is the intended path. No fake provider buttons are ever rendered.
