# Agentic Performance Characterization & Investigation

**Presenter:** Andrew Theurer
**Department:** Performance & Scale
**Date:** June 2026

---

## Slide 1: Agentic AI Is Critical — But SDLC Isn't Our Primary Mission

> "We'll create capacity through evolving roles: Letting AI handle execution
> so our people can solve problems, ensure system output is accurate,
> and drive innovation."
> — CEO

- The company's agentic AI investment is primarily focused on **SDLC**
  (Software Development Life Cycle)
- As Performance & Scale engineers, we write software — but our core mission
  is **performance characterization and investigation**
- The SDLC agentic wave doesn't directly address the work that consumes
  most of our time
- We need an agentic strategy purpose-built for **performance engineering**

---

## Slide 2: Two Kinds of Performance Work

| | Continuous Performance Testing (CPT) | Ad-Hoc Performance Work |
|---|---|---|
| **Nature** | Repeatable, regression-focused | Exploratory, investigative |
| **Environment** | Locked down — same HW, controlled SW updates | Variable — different HW, OS versions, configs |
| **Automation** | Traditional CI/CD pipelines work well | Too many unknowns for rigid automation |
| **AI opportunity** | Result analysis, anomaly detection | **End-to-end orchestration** |
| **Examples** | Nightly regression suite on fixed cluster | "Compare RHEL 9 vs 10 forwarding on CX7" |

- CPT is well-served by traditional automation — the environment is predictable
- **Ad-hoc work never goes away** — new hardware, new kernels, customer escalations,
  architecture evaluations
- This is where engineers spend days on manual setup, execution, and analysis

---

## Slide 3: The Ad-Hoc Performance Gap — Before and After

### Today: Engineer in the Loop

```
Day 1    Reserve/provision hardware
         Install benchmark tooling
         Configure network, storage, OS tuning
Day 2    Write/adapt run configuration
         Execute benchmark runs
         Babysit execution, re-run on failures
Day 3    Collect results, analyze data
         Write up findings, share with stakeholders
```

**3+ days of engineer time per investigation**

Even with benchmark automation frameworks and AI coding assistants
(Claude Code), the engineer is still the orchestrator — manually connecting
the pieces, making decisions at every step, and context-switching between tools.

### With Agentic Performance

```
Minute 0     Engineer submits a one-line request
Minutes 1-5  Agents triage, plan, and negotiate resources
Minutes 5-30 Agents provision, configure, and execute
Minute 30+   Engineer receives a structured analysis with verdict
```

**Engineer time: one prompt + review of results**

---

## Slide 4: Current Tools Aren't Enough

| Tool | What it does well | The gap |
|---|---|---|
| **Benchmark harnesses** | Execute benchmarks reproducibly | Someone still has to provision, configure, write run-files, interpret results |
| **CI/CD pipelines** | Run the same test repeatedly | Can't handle variable environments, new benchmarks, or investigative workflows |
| **AI coding assistants** (Claude Code) | Help write and debug code interactively | Session-bound — the engineer is still in the loop for every decision; not a reusable, autonomous solution |

We need something that can:

- **Understand intent** — "compare these two things" vs "find the bottleneck"
- **Acquire resources** — reserve hardware, provision it, install tooling
- **Execute** — generate run configurations, launch benchmarks, monitor progress
- **Analyze** — interpret results, form conclusions, produce a report
- **All without a human in the loop**

---

## Slide 5: The Vision — What If You Could Just Ask?

### Low Complexity

> **User:** "Does this NVMe have trouble with 512B writes vs 4K writes?"
>
> **Outcome:** Agents run fio with both block sizes on the target device,
> compare IOPS and latency distributions, and deliver a summary:
> "512B writes show 3.2x higher p99 latency due to write amplification.
> 4K aligns with the device's internal page size and is 2.8x more efficient."

### Medium Complexity

> **User:** "I need baseline memory bandwidth numbers on host X."
>
> **Outcome:** Agents install the appropriate harness, run STREAM,
> produce a baseline report with statistical confidence intervals,
> and store results for future regression comparison.

---

## Slide 6: The Vision — Scaling Up

### High Complexity

> **User:** "Compare RHEL 9 and RHEL 10 on standard uperf tests with a CX7
> adapter, at least 100Gb link speeds."
>
> **Outcome:** Agents reserve two pairs of hosts from the hardware pool,
> provision each pair with the respective OS, configure networking for
> 100Gb+, run a uperf test matrix, and produce a comparative analysis
> with per-message-size throughput and latency charts.

### Expert-Level

> **User:** "Run a scale test in OpenShift — scale namespaces, pods, and
> user-defined-networks from 1 to 200, on a cluster with at least
> 5 worker nodes."
>
> **Outcome:** Agents provision an OpenShift cluster, execute an iterative
> scaling test, monitor control-plane and data-plane metrics at each step,
> identify the scaling knee, and produce a report showing where
> performance degrades and what the limiting factor is.

---

## Slide 7: Multi-Agent — Why Not Just One Big Agent?

Inspiration from projects like **fullsend-ai** shows that multi-agent
architectures work — but performance testing has challenges that go
far beyond what those solutions address:

| Challenge | Why it's hard |
|---|---|
| **Hardware diversity** | Bare metal, VMs, OpenShift, GPUs — each with different provisioning flows |
| **Multiple benchmark harnesses**¹ | Each with its own run-file format, result structure, and installation process |
| **Resource negotiation** | "I need 2 hosts with 25G NICs for 48 hours" — agents must query availability, reserve, and release |
| **Security boundaries** | Agents SSH into production-class hardware — credentials must be scoped and gated |
| **Result interpretation** | Not just pass/fail — statistical analysis, comparison against baselines, root-cause hypotheses |

A single monolithic agent can't hold all of this context. Specialized agents
with clear boundaries can.

---

## Slide 8: The Ticket as Shared Workspace

The user submits a request. A **ticket** becomes the shared workspace where
agents collaborate — not a baton passed down a chain.

```
 User: "Run a 4k random read storage test on host X"
   │
   ▼
┌──────────┐
│  Triage  │  Understands intent, captures hypothesis
└────┬─────┘
     │ posts intent to ticket
     ▼
┌──────────┐     ┌──────────┐
│Benchmark │◄───►│ Resource │  Both read intent, post plans,
│  Agent   │     │  Agent   │  negotiate if they conflict
└────┬─────┘     └────┬─────┘
     │  agreed plan   │
     ▼                ▼
┌──────────────┐
│ Provisioning │  Installs harness, configures hosts
└──────┬───────┘
       ▼
┌──────────────┐
│  Benchmark   │  Generates run-file, executes, monitors
│  Execution   │
└──────┬───────┘
       ▼
┌──────────────┐
│    Review    │  Analyzes results, forms verdict, writes report
└──────┬───────┘
       ▼
   User receives structured analysis
```

Key insight: agents can **negotiate**. If the benchmark agent needs 2 hosts
but the resource agent can only find 1, they resolve it — or escalate to
the user — before wasting time on provisioning.

---

## Slide 9: Agent Architecture — Contracts and Boundaries

### Five Specialized Agents

| Agent | Role | Scoped Tools (MCP) |
|---|---|---|
| **Triage** | Parse user intent, form hypothesis | Ticket read/write |
| **Resource** | Find and reserve hardware (null provider, QUADS, future: cloud) | QUADS API, host inventory |
| **Provisioning** | Install harness, configure hosts via SSH | SSH, SCP, secrets vault |
| **Benchmark** | Generate run-file, execute benchmark, monitor | Harness CLI, run-file schema validator |
| **Review** | Analyze results, compare to baselines, produce verdict | Metric query (CDM), result archive |

### Key Design Principles

- **Stateless agents** — all state lives in the ticket; agents can crash and resume
- **One MCP server per agent** — zero-trust tool boundaries; provisioning can SSH,
  but review cannot
- **Contract-based installs** — public skill definitions + private org config
  (registry URLs, credentials) are composed at runtime
- **Schema guardrails** — run-files validated against the harness schema *before*
  execution; the LLM can fix and retry without wasting a 30-minute benchmark run

---

## Slide 10: Skills — Local, Remote, and Gated

Agents don't hardcode knowledge about specific benchmarks or infrastructure.
Instead, they discover capabilities through **skill providers**.

```
┌─────────────────────────────────────┐
│          Composite Skill Provider   │
│  ┌───────────┐   ┌───────────────┐  │
│  │  Public   │ + │   Private     │  │
│  │  (git)    │   │ (~/.agentic-  │  │
│  │           │   │  perf/private │  │
│  │ harness A │   │  -skills/)    │  │
│  │ harness B │   │              │  │
│  │ ...       │   │ registry URLs │  │
│  │           │   │ install flags │  │
│  │           │   │ vault paths   │  │
│  └───────────┘   └───────────────┘  │
└─────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│         Secrets Provider            │
│  Local vault: ~/.agentic-perf/      │
│  secrets/ (symlinks to real creds)  │
│                                     │
│  Secrets are NEVER in skill config  │
│  or agent prompts — injected at     │
│  runtime by the provisioning agent  │
└─────────────────────────────────────┘
```

- **Public skills** define *what* a harness can do (benchmarks, parameters, roles)
- **Private skills** define *how* your org installs it (internal registry, tokens)
- **Secrets** are resolved at runtime, scoped to the agent that needs them
- Adding a new benchmark harness = adding a new skill provider — no agent code changes

---

## Slide 11: Real Result — FIO on NVMe (End-to-End)

**User prompt:** "Run fio 4k random read test on host-1"

**What happened (autonomously):**

| Step | Agent | Action |
|---|---|---|
| 1 | Triage | Parsed intent: fio benchmark, 4K random read, hypothesis = measure baseline IOPS |
| 2 | Resource | User provided host directly — null provider, no reservation needed |
| 3 | Provisioning | Detected crucible already installed on controller, skipped reinstall |
| 4 | Benchmark | Generated crucible run-file, validated against schema, executed via `crucible run` |
| 5 | Review | Analyzed results — **verdict: hypothesis confirmed** |

- All 5 agents powered by Claude (Vertex AI)
- Real SSH to real hardware, real crucible execution
- Run ID: `403a4761-e46d-4813-bbb6-5377c8a3da05`
- Total engineer time: **one prompt**

---

## Slide 12: Real Result — STREAM Memory Bandwidth via Zathras

**User prompt:** "Run STREAM memory bandwidth test using zathras on host-1"

**What happened (autonomously):**

| Step | Agent | Action |
|---|---|---|
| 1 | Triage | Parsed intent: STREAM benchmark via zathras harness, measure memory bandwidth |
| 2 | Resource | Same host as controller and SUT — single-host topology |
| 3 | Provisioning | Installed zathras from git, handled RHEL-specific package requirements |
| 4 | Benchmark | Generated zathras scenario config, executed `streams` benchmark |
| 5 | Review | Produced detailed analysis with baseline metrics |

**Results delivered autonomously:**

| Metric | Value |
|---|---|
| Memory Bandwidth (mean) | **9.42 GB/s** (single-thread) |
| Range | 9.1 – 9.7 GB/s |
| Coefficient of Variation | ~2.7% (stable) |
| Verdict | **Baseline established** — suitable for regression tracking |

This was a **different harness** (zathras, not crucible) — the same agents
adapted because benchmark knowledge comes from skill providers, not hardcoded logic.

---

## Slide 13: What's Next

### Near-term (in progress)
- **Collaborative negotiation** — benchmark and resource agents plan concurrently
  and resolve conflicts before provisioning begins
- **QUADS integration** — automated hardware reservation from the Scale Lab pool
- **Absent-suite mode** — when no harness covers the requested benchmark, the
  benchmark agent learns and automates it on the fly

### Medium-term
- **Multi-environment tests** — bare metal + OpenShift in the same investigation
- **Comparison workflows** — "run A vs B" with statistical rigor built in
- **Real Jira integration** — replace the local state store with Jira tickets
  for visibility and collaboration
- **Containerized agents** — run in Podman for isolation and portability

### Long-term vision
- **Cross-team reuse** — any perf team can add their harness as a skill provider
- **Continuous learning** — agents build institutional knowledge about hardware
  baselines, known regressions, and tuning profiles

---

## Slide 14: Summary

**The problem:** Ad-hoc performance work is manual, time-consuming, and
resistant to traditional automation because every investigation is different.

**The approach:** Multiple specialized AI agents that collaborate via a shared
ticket, with pluggable skills for different benchmark harnesses and
infrastructure providers.

**The proof:** Two end-to-end demonstrations — different benchmarks, different
harnesses — fully autonomous from prompt to analysis.

**The ask:** This is how we apply the company's agentic vision to what we
actually do every day. We're not replacing engineers — we're letting AI handle
the execution so our people can focus on the problems that matter.

---

## Appendix A: Agent Negotiation Deep Dive

### The Planning Phase

During the `planning` state, Benchmark and Resource agents run concurrently:

- **Benchmark agent** reads triage intent, selects a harness and benchmark,
  posts requirements (host count, roles, OS constraints)
- **Resource agent** reads triage intent, identifies available providers
  (user-provided hosts, QUADS, cloud), posts what it can supply
- If plans are compatible → proceed to provisioning
- If conflict → agents adjust plans or escalate to user

### Conflict Example

```
User: "Run a network forwarding performance test"

Benchmark Agent: "This benchmark requires bare metal with SR-IOV capable
                  NICs, minimum 2 hosts (client + DUT), 25G+ link speed"

Resource Agent:  "User didn't provide hosts. The hardware pool has 3 hosts
                  available with 25G NICs but only for 24 hours."

Resolution:      Plans are compatible — proceed with reservation.
                 Benchmark agent confirms 24hr window is sufficient.
```

---

## Appendix B: LLM Integration and Guardrails

- **Modular LLM provider** — Claude (Anthropic API or Vertex) is default,
  but the provider interface is swappable
- **Schema validation** — run-files are validated against the harness's own
  schema before execution
- **Structured output** — every agent uses a `submit_result` tool pattern
  to ensure responses are machine-parseable
- **Secrets isolation** — credentials are never in prompts or skill configs;
  injected at runtime by a dedicated secrets provider
- **PID lock** — only one orchestrator instance can run at a time

---

## Appendix C: Extensibility — Adding a New Harness

Adding a new benchmark harness requires:

1. A Python class implementing the `SkillProvider` interface
2. A public skill definition (benchmarks, parameters, roles)
3. Optionally, a private skill config (org-specific install details)

No changes to agent prompts, the orchestrator, or the state machine.

When no existing harness covers a requested benchmark, the system can
fall back to **absent-suite mode** — the benchmark agent learns and
automates the unknown benchmark on the fly.

---

### Footnotes

¹ Benchmark harnesses in the Perf & Scale ecosystem include:
arcaflow, benchmark-runner, cluster-buster, crucible, forge, fournos,
k8s-netperf, kube-burner, and zathras. Each is a potential skill provider
in agentic-perf.
