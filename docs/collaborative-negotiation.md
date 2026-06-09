# Collaborative Agent Negotiation — Design Document

## Status: Draft — pending review and refinement

## 1. Current Linear Model and Its Limitations

### Current Pipeline
```
Triage → Resource → Provisioning → Benchmark → Review → Teardown
```

Each agent runs in sequence. Triage decides everything upfront (benchmark suite, resource requirements), passes a baton to Resource, which passes to Provisioning, etc. No agent can push back to a previous one.

### Limitations

**Triage is overloaded with decisions it shouldn't make:**
- Picks the specific benchmark suite (crucible's fio vs uperf) — this is benchmark-agent knowledge
- Looks up resource requirements (min_hosts, roles) — this is derived from the benchmark choice
- If we add a second automation suite, triage needs to know about both

**No cross-agent feedback:**
- If Resource can't satisfy what Triage requested, it can only ask the user — it can't tell Triage "pick a different benchmark that needs fewer hosts"
- If Benchmark discovers the chosen suite doesn't support the target environment, it can't negotiate with Resource for a different environment
- Each agent operates in isolation, reading only what previous agents wrote

**Real-world scenarios that break the linear model:**
- User wants to test on both bare metal (RHEL 10) and OpenShift — Resource needs to source from two providers, Benchmark needs to know if the suite supports both
- User asks for a benchmark crucible doesn't support — Benchmark agent needs to decide between "automate it myself" (absent suite) and "suggest an alternative crucible benchmark"
- QUADS can provide hosts but not in time — Resource needs Benchmark to confirm whether a shorter reservation window works

## 2. The Collaborative Negotiation Model

### Analogy
Three specialists working together, like a real perf testing team:
- **Triage** = the perf tester who understands the user's intent
- **Benchmark** = the automation engineer who knows what suites can do
- **Resource** = the sysadmin who controls hardware

### How It Works

1. **Triage captures intent** — parses the user's request into a structured intent: what they want to test, what their hypothesis is, any constraints (OS, hardware, environment type). Triage does NOT pick a benchmark suite or look up resource requirements. It posts the intent to the ticket.

2. **Benchmark and Resource agents are notified concurrently** — both read Triage's intent and add their assessment to the ticket:
   - Benchmark: "I can do this with crucible's fio (1 host, client role)" or "No automation suite covers this — I'll need to automate it myself" or "This requires trafficgen but the user wants OpenShift which trafficgen doesn't support — suggesting iperf instead"
   - Resource: "User provided hosts directly" or "I can reserve from QUADS — 3 hosts available, 24hr window" or "OpenShift cluster available but needs 2hr provisioning lead time"

3. **Each agent reads the other's updates** — if Benchmark says "need 2 hosts" and Resource says "only 1 available," there's a conflict. The ticket surfaces this.

4. **Conflict resolution** — options:
   - Agent-to-agent: Benchmark adjusts its plan based on what Resource can provide
   - HITL: If agents can't resolve it, the user is asked to arbitrate
   - Priority rules: configurable policies (e.g., "always prefer the benchmark's requirements, ask user if resource can't satisfy")

5. **Convergence** — once Benchmark and Resource agree (both have posted compatible plans), the ticket moves to Provisioning.

### The Ticket as Shared Workspace

The ticket is no longer a baton — it's a whiteboard. Each agent:
- **Reads** all custom fields and comments (including other agents' assessments)
- **Writes** its own assessment to specific custom fields
- **Reacts** to changes from other agents

New custom fields for the collaborative model:
- `intent` — Triage's structured understanding of what the user wants (not tied to any suite)
- `benchmark_plan` — Benchmark agent's proposed plan (suite, benchmark, parameters, requirements)
- `resource_plan` — Resource agent's proposed plan (provider, hosts, availability, constraints)
- `plan_status` — "proposing" | "conflict" | "agreed" | "needs_user_input"

## 3. State Machine Changes

### Current States
```
new → triage_pending → awaiting_hardware → awaiting_provision → 
executing_benchmark → awaiting_review → awaiting_teardown → closed
```

### Proposed States
```
new → triage_pending → planning → awaiting_provision → 
executing_benchmark → awaiting_review → awaiting_teardown → closed
```

The `planning` state replaces `awaiting_hardware`. During `planning`:
- Both Benchmark and Resource agents run (concurrently or in rounds)
- Each posts their plan to the ticket
- If they agree → transition to `awaiting_provision`
- If conflict → either negotiate another round or escalate to `awaiting_customer_guidance`

### Dispatcher Changes
The dispatcher needs to support:
- Multiple agents active on the same ticket simultaneously during `planning`
- Re-dispatching agents when the ticket is updated by another agent (not just on status changes)
- Round-based negotiation with a max-rounds limit to prevent infinite loops

## 4. Test Matrix

### Benchmark Suites
| Suite | Description | Example |
|-------|-------------|---------|
| **Crucible** | Full automation suite, known benchmarks (fio, uperf, trafficgen) | "Run fio on host X" |
| **Agent-automated** | No suite covers it, benchmark agent learns and automates | "Run sysbench CPU test" |

### Resource Providers
| Provider | Description | Example |
|----------|-------------|---------|
| **Null** | User provides hosts + SSH keys directly | "Use nfv-amd-5, root SSH" |
| **QUADS** | Reserve from Scale Lab via self-service API | "I need 2 hosts with 25G NICs for 48 hours" |

### Test Scenarios (2x2 + edge cases)

1. **Crucible + Null** (current working case): "Run fio on nfv-amd-5"
2. **Crucible + QUADS**: "Run uperf network test, reserve 2 hosts from Scale Lab"
3. **Agent-automated + Null**: "Run sysbench CPU benchmark on nfv-amd-5" (absent suite)
4. **Agent-automated + QUADS**: "Run a custom memory benchmark, reserve a host from Scale Lab"
5. **Conflict case**: "Run trafficgen on OpenShift" — Benchmark: "trafficgen needs bare metal," Resource: "user asked for OpenShift" → negotiation needed
6. **Multi-environment**: "Test forwarding between a bare metal RHEL 10 host and an OpenShift pod" — Resource needs to source from two providers

## 5. Implementation Plan (High-Level)

### Phase 1: Refactor Agent Communication
- Add `intent`, `benchmark_plan`, `resource_plan`, `plan_status` custom fields
- Triage outputs intent only (not benchmark resolution)
- Benchmark and Resource agents read intent and produce plans

### Phase 2: Concurrent Planning
- Replace `awaiting_hardware` with `planning` state
- Dispatcher supports multiple agents on one ticket during planning
- Add negotiation logic (read other agent's plan, adjust own plan)

### Phase 3: Add QUADS Resource Provider
- QUADS skill with available/assign/poll/terminate tools
- Resource agent uses QUADS when user doesn't provide hosts directly

### Phase 4: Test the Matrix
- Run all 6 scenarios above
- Verify negotiation works for conflict cases

## 6. Open Questions

- Should there be a max negotiation rounds before escalating to the user?
- How does the benchmark agent discover what environments a suite supports? (This is skill/context we need to add)
- Should the planning phase have a timeout?
- Can we reuse the existing HITL mechanism for agent-to-agent negotiation, or do we need a separate channel?
