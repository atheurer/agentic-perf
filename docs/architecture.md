# Architecture

This document describes the internal architecture of agentic-perf: how the
components fit together, how data flows through the system, and the key
abstractions that enable extensibility.

## System Overview

Agentic-perf has four major subsystems:

```
┌─────────────────────────────────────────────────────────────────────┐
│  CLI (cli.py)                                                       │
│  User submits tickets, watches progress, replies, views transcripts │
└────────────────────────────────┬────────────────────────────────────┘
                                 │ HTTP
┌────────────────────────────────▼────────────────────────────────────┐
│  State Store (FastAPI)                                              │
│  REST API for tickets, transitions, comments, events                │
│  In-memory ticket store + web dashboard                             │
│  Port 8090 (configurable)                                           │
└────────────────────────────────┬────────────────────────────────────┘
                                 │ HTTP (polling)
┌────────────────────────────────▼────────────────────────────────────┐
│  Orchestrator                                                       │
│  Polls state store for non-terminal tickets                         │
│  Dispatches agents based on ticket status                           │
│  One agent at a time per ticket                                     │
└───────┬──────────┬────────────┬──────────┬────────────┬────────────┘
        │          │            │          │            │
   ┌────▼──┐ ┌────▼───┐ ┌─────▼────┐ ┌───▼────┐ ┌────▼───┐
   │Triage │ │Resource│ │Provision │ │Bench-  │ │Review  │
   │Agent  │ │ Agent  │ │  Agent   │ │mark    │ │ Agent  │
   │       │ │        │ │          │ │Agent   │ │        │
   └───────┘ └────────┘ └──────────┘ └────────┘ └────────┘
```

A sixth agent, the **Introspection Agent**, runs as an optional companion
alongside the pipeline agents. It does not participate in the state machine
— it continuously watches the event stream and writes observations to
`custom_fields.introspection` for the web dashboard to display.

All communication between components goes through the state store's REST API.
Agents never talk to each other directly — they read and write the shared
ticket document.

## State Machine

Tickets progress through a defined set of statuses. Each status maps to an
agent that processes the ticket at that stage. Two paths are supported:
ad-hoc test execution (original linear pipeline) and recursive investigation
(iterative loop with convergence).

### Ad-hoc test execution

```
                          ┌──────────────────┐
                          │       new        │
                          └────────┬─────────┘
                                   │
                          ┌────────▼─────────┐
                    ┌─────│  triage_pending   │─────┐
                    │     └────────┬──────────┘     │
                    │              │                 │
                    │     ┌────────▼─────────┐      │
                    │  ┌──│awaiting_hardware  │──┐   │
                    │  │  └────────┬──────────┘  │   │
                    │  │           │              │   │
                    │  │  ┌────────▼─────────┐   │   │
                    │  │  │awaiting_provision │─┐ │   │
                    │  │  └────────┬──────────┘ │ │   │
                    │  │           │             │ │   │
                    │  │  ┌────────▼──────────┐  │ │   │
                    │  │  │executing_benchmark│──┤ │   │  All stages can
                    │  │  └────────┬──────────┘  │ │   │  pause at
                    │  │           │              │ │   │  awaiting_customer_
                    │  │  ┌────────▼─────────┐   │ │   │  guidance for
              rerun─┼──┼──│ awaiting_review  │───┤ │   │  human input
                    │  │  └────────┬──────────┘  │ │   │
                    │  │           │              │ │   │
                    │  │  ┌────────▼──────────┐   │ │   │
                    │  │  │awaiting_teardown  │───┘ │   │
                    │  │  └────────┬──────────┘     │   │
                    │  │           │                 │   │
                    │  │  ┌────────▼─────────┐      │   │
                    │  │  │     closed       │      │   │
                    │  │  └──────────────────┘      │   │
                    │  │                            │   │
                    │  └────────────┬───────────────┘   │
                    │              │                     │
                    │     ┌────────▼───────────────┐     │
                    └─────│awaiting_customer_      │─────┘
                          │guidance                │
                          └────────────────────────┘
```

### Recursive investigation

```
                          ┌──────────────────┐
                          │       new        │
                          └────────┬─────────┘
                                   │
                          ┌────────▼─────────┐
                          │  triage_pending   │─────────────────┐
                          └────────┬──────────┘                 │
                                   │                            │
                          ┌────────▼──────────┐                 │
                     ┌────│gathering_context   │──── closed     │
                     │    └────────┬───────────┘   (dedup)      │
                     │             │                             │
              ┌──────│────┌────────▼──────────────┐             │
              │      │    │planning_investigation  │─────┐      │
              │      │    └────────┬───────────────┘     │      │
              │      │             │                     │      │
              │      │    ┌────────▼─────────┐           │      │
              │      │ ┌──│awaiting_hardware  │          │      │
              │      │ │  └────────┬──────────┘          │      │
              │      │ │           │                     │      │
              │      │ │  ┌────────▼─────────┐           │      │
              │      │ │  │awaiting_provision │──┐       │      │
              │      │ │  └────────┬──────────┘  │       │      │  All stages
              │      │ │           │             │       │      │  can pause at
              │      │ │  ┌────────▼──────────┐  │       │      │  awaiting_
              │      │ │  │executing_benchmark│──┤       │      │  customer_
              │      │ │  └────────┬──────────┘  │       │      │  guidance
              │      │ │           │              │       │      │
  refine──────┼──────┼─┼──┌───────▼────────────┐ │       │      │
  params      │      │ │  │evaluating_         │─┤       │      │
              │      │ │  │convergence         │ │       │      │
  re-flash────┼──────┘ │  └───────┬────────────┘ │       │      │
  hardware    │        │          │              │       │      │
              │        │ ┌────────▼────────────┐  │       │      │
              │        │ │synthesizing_results │──┘       │      │
              │        │ └────────┬────────────┘          │      │
              │        │          │                       │      │
              │        │ ┌────────▼──────────┐            │      │
              │        │ │awaiting_teardown  │────────────┘      │
              │        │ └────────┬──────────┘                   │
              │        │          │                              │
              │        │ ┌────────▼─────────┐                    │
              │        │ │     closed       │                    │
              │        │ └──────────────────┘                    │
              │        │                                         │
              │        └─────────────┬───────────────────────────┘
              │                      │
              │             ┌────────▼───────────────┐
              └─────────────│awaiting_customer_      │
                            │guidance                │
                            └────────────────────────┘
```

### Status-to-Agent Mapping

| Status | Agent | Mode |
|---|---|---|
| `triage_pending` | TriageAgent | — |
| `awaiting_hardware` | ResourceAgent | create |
| `awaiting_provision` | ProvisioningAgent | — |
| `executing_benchmark` | BenchmarkAgent | — |
| `awaiting_review` | ReviewAgent | — |
| `awaiting_teardown` | ResourceAgent | teardown |
| `gathering_context` | GatheringContextAgent | Investigation Record dedup |
| `planning_investigation` | *(stub)* | — |
| `evaluating_convergence` | EvaluateAgent | Convergence assessment |
| `synthesizing_results` | SynthesisAgent | Investigation Record write-back |

Terminal statuses (`closed`, `awaiting_customer_guidance`) do not dispatch
agents. `awaiting_customer_guidance` resumes to the previous status when the
user replies. The `planning_investigation` agent is a stub that auto-advances;
all other investigation loop agents are fully implemented.

### Introspection Agent (Out-of-Band)

The introspection agent is a continuous background observer that runs
alongside the pipeline agents. It does NOT participate in the status-to-agent
mapping or the state machine — it is started by the orchestrator as a
companion task before the first pipeline agent dispatches.

See [Introspection](#introspection-agent) for details.

### Special Transitions

- **Rerun loop:** `awaiting_review` can transition back to `triage_pending`
  for iterative testing.
- **Investigation loop-back:** `evaluating_convergence` can loop back to
  `planning_investigation` (refine parameters) or `awaiting_provision`
  (re-flash tainted hardware).
- **Grounding dedup:** `gathering_context` routes to `retrospective_pending`
  (not directly to `closed`) if a matching Investigation Record is found,
  so the retrospective agent can analyze the dedup-skipped ticket.
- **Abort:** From `awaiting_customer_guidance`, the user can jump directly to
  `awaiting_teardown` to skip remaining work.
- **Execution plan re-benchmark:** `awaiting_review` can transition back to
  `executing_benchmark` when an execution plan has more benchmark steps
  to run.

### Investigation Pipeline

Investigation-mode tickets (those with `anomaly_context` in
`custom_fields`) follow a different path from ad-hoc benchmarks:

```
triage → gathering_context → planning (stub) → provision → benchmark
  → evaluating_convergence → (loop or synthesizing_results)
  → awaiting_teardown → retrospective → closed
```

**Routing is code-enforced, not LLM-inferred.** The triage agent
checks for `anomaly_context` in `custom_fields` after completing
its analysis. If present, it transitions to `gathering_context`
instead of `awaiting_hardware`. The benchmark agent uses the same
pattern — investigation tickets route to `evaluating_convergence`
instead of `awaiting_review`. The `anomaly_context` field is set
by alert seeds, CLI flags, or API calls before triage runs.

#### Gathering Context (Dedup Gate)

The `GatheringContextAgent` queries open Investigation Records for
the same subsystem using LLM-driven semantic matching. If a match
is found (cross-platform, label drift, and magnitude shifts are
handled), the agent appends a `build_history` entry to the matched
record and routes to `retrospective_pending` (skipping the full
investigation). If no match, proceeds to `planning_investigation`.

#### Evaluate Agent (Convergence Assessment)

The `EvaluateAgent` drives the recursive investigation loop with
two-layer evaluation:

1. **Deterministic gates** (code-enforced): `max_iterations`,
   statistical thresholds, info gain stall. If a gate fires, the
   LLM cannot override it.
2. **LLM reasoning** (when no deterministic gate fires): assesses
   Isolation (≥90% confidence), Entropy Stall, Expected Regression
   (when change context available), and Manual Interruption.

On loop-back: appends a new execution plan step with refined
parameters, writes a ledger entry, transitions to
`planning_investigation` or `awaiting_provision`.

On convergence: writes a final ledger entry, transitions to
`synthesizing_results`. Uses `max_iterations=0` — termination
is driven by convergence gates and budget guardrails.

#### Synthesis Agent (Investigation Record Write-Back)

The `SynthesisAgent` produces the Investigation Record when a
convergence gate fires. It:

1. Asks the LLM to produce a comprehensive root cause summary
   from the investigation evidence
2. Collects operational metrics from ticket state and EventBus:
   provision_cycles, wall_clock_mins, hardware_time_mins,
   info_gain_trajectory, stall_events, token/cost data
3. Creates the Investigation Record via the investigation-records
   MCP server with the complete operational context
4. Transitions to `awaiting_teardown`

#### Investigation Ledger

The investigation ledger (`custom_fields.investigation_ledger`)
tracks reasoning history alongside the execution plan. Each entry
references plan steps by index, maintaining clear separation:
the plan handles sequencing (what runs next), the ledger handles
reasoning (what was learned). The ledger is append-only.

### Execution Plans

Tickets can carry a multi-step execution plan in
`custom_fields.execution_plan`. This enables two modes of operation:

#### Predetermined sequences (known at submission time)

When the user requests multiple separate benchmark runs — e.g., "run
crucible uperf with wsize=64, then run crucible again with wsize=16384" —
the triage agent produces an execution plan:

```json
{
  "current_step": 0,
  "run_ids": [],
  "steps": [
    {"id": 0, "agent_type": "benchmark", "status": "in_progress",
     "params": {"label": "wsize-64B", "mv_params": {"wsize": "64"}}},
    {"id": 1, "agent_type": "benchmark", "status": "pending",
     "params": {"label": "wsize-16384B", "mv_params": {"wsize": "16384"}}},
    {"id": 2, "agent_type": "review", "status": "pending", "params": {}}
  ]
}
```

The orchestrator advances through the plan automatically after each
agent completes. The benchmark agent reads step-specific parameters
from the current step. The review agent sees all completed run IDs
for comparison.

**Important:** This is for separate harness invocations, not parameter
sweeps within a single run. Many harnesses (e.g., crucible's mv-params)
can test multiple parameter values in one invocation — the triage agent
should use that capability when appropriate and only create an execution
plan when separate runs are explicitly needed.

**Universal plans:** Every ticket gets an execution plan, even single-
benchmark requests (which get a 1-step plan). This ensures the
investigation ledger always has `plan_steps` to reference, and lets
review agents and users extend any ticket's plan dynamically.

#### Iterative convergence (unknown iteration count)

When the number of iterations is not known upfront — e.g., "keep
refining parameters until throughput stabilizes" — the same plan
mechanism serves as the work ledger. The `evaluating_convergence`
agent (from the recursive investigation loop) can dynamically append
steps to the plan based on results:

- **Not converged:** append a new benchmark step with refined parameters
- **Converged:** append a review/synthesis step and stop

This means the plan grows during execution rather than being fully
specified at submission time. Completed steps remain immutable with
their results, while the convergence agent extends the pending portion.

The user defines convergence criteria on the ticket (see issue #134) —
the evaluating agent reads them to decide whether to loop or stop.
Criteria are stored in `custom_fields.convergence_criteria` and
evaluated in two layers:

1. **Deterministic gates** (checked first, no LLM call):
   - `max_iterations` — hard ceiling (0 = unlimited)
   - `metric` + `threshold_pct` + `consecutive_passes` —
     statistical convergence (metric within N% for M runs)
   - `compare_metric` + `compare_threshold_pct` —
     comparative convergence (delta between last two runs)
   - `min_info_gain` — entropy stall detection

2. **LLM-driven evaluation** (when no deterministic gate fires):
   The evaluate agent reasons about whether more data would change
   the conclusion, using the accumulated iteration results and
   the original hypothesis.

The `ConvergenceCriteria` model and `evaluate_deterministic()` function
live in `providers/convergence.py`. See `IterationResult` for the
per-iteration data structure that feeds the evaluation.

Both modes produce the same artifact: an ordered list of completed steps
with run IDs, parameters, and results — giving the review agent (or
human) a complete record of what ran and why.

#### Investigation Ledger

The investigation ledger (`custom_fields.investigation_ledger`) tracks
reasoning history alongside the execution plan. Each entry references
plan steps by index, maintaining clear separation:

- **Execution plan** — shared mutable, tracks sequencing (what runs
  next). Written by the orchestrator, users (via HITL), review agents,
  and the evaluate agent.
- **Investigation ledger** — append-only, tracks reasoning (what was
  learned). Written only by the evaluate agent. Each entry has:
  `iteration`, `plan_steps`, `hypothesis`, `params_rationale`,
  `conclusion`, `info_gain`, `timestamp`.

The split exists because the plan is edited by multiple writers
including humans via HITL — investigation reasoning belongs in a
separate write-once structure. The `LedgerEntry` model and helpers
live in `providers/ledger.py`.

#### Plan step lifecycle

```
pending → in_progress → completed
                      → failed
```

- **Completed steps** are immutable — results (run_id, benchmark_status)
  are captured and cannot be modified.
- **Pending steps** can be modified, reordered, or deleted by the user
  via HITL (`awaiting_customer_guidance`), or extended by agents.
- **In-progress steps** are locked while the agent is running.

#### Orchestrator plan advancement

After each agent completes, the orchestrator's `_advance_plan()`
function checks whether the completed agent matches the current plan
step. If so, it:

1. Marks the step completed and captures results
2. Appends the run_id to the plan's `run_ids` list
3. Advances `current_step` to the next step
4. Transitions the ticket to the next step's target status

If the agent paused for HITL (pre-run approval, clarification), the
plan is not advanced — the step stays in_progress until the agent
actually finishes its work after the user replies.

If the completed agent is not part of the plan (resource, provisioning),
the plan is not touched.

## Agents

### Agent Base Class

All agents extend `AgentBase` (defined in `agents/base.py`), which provides:

- **LLM loop** — Up to 20 iterations of: send messages to LLM → receive
  response → execute tool calls → append results → repeat. Stops when the
  LLM returns `end_turn` or calls a `submit_*` tool.
- **Tool dispatch** — Routes tool calls to registered handlers by name.
- **State store client** — Methods for reading tickets, transitioning status,
  updating custom fields, and adding comments.
- **Event emission** — Every LLM request, response, tool call, tool result,
  transition, and error is emitted through the EventBus.
- **Human input** — `_request_human_input()` pauses the ticket at
  `awaiting_customer_guidance` with a question for the user.

### Agent Lifecycle

```
1. Orchestrator polls state store, finds ticket in dispatch-eligible status
2. Dispatcher creates the appropriate agent instance
3. Agent reads the ticket document
4. Agent constructs system prompt + initial messages from ticket state
5. Agent enters LLM loop (tool calls ↔ tool results)
6. Agent writes results to ticket custom_fields via submit_* tool
7. Agent transitions ticket to next status
8. Agent exits; orchestrator continues polling
```

### Individual Agents

**Triage Agent** — Parses the user's natural-language request into structured
fields: hypothesis, benchmark suite, host requirements, resource preferences,
and operational directives (harness choice, install behavior, cleanup policy,
endpoint type, pre-run approval).

**Resource Agent** — Acquires hardware through one of three paths:
1. User provided hosts → validate via SSH
2. Triage directives specify a provider → reserve from that provider
3. No hosts specified → auto-select provider (prefers QUADS for perf work)

Runs in two modes: `create` (acquire) and `teardown` (release).

**Provisioning Agent** — Prepares hosts for benchmarks. Checks platform
contracts (OS compatibility, required packages), handles existing harness
installations (reinstall/update/skip per directives), installs the harness,
and optionally deploys K3s for Kubernetes endpoints.

**Benchmark Agent** — Constructs the run configuration by reading harness
documentation, schemas, and example run-files through its tools. The LLM
builds the run-file directly (no template patching), validated against the
harness schema. Handles both remotehosts and Kubernetes endpoint types.

**Review Agent** — Retrieves results from the benchmark harness, analyzes
metrics, and produces a verdict (hypothesis confirmed/refuted/inconclusive)
with key metrics and recommendations. Harness-agnostic: discovers how to
retrieve results through skill providers.

**Introspection Agent** — Continuous passive observer that runs alongside
the pipeline agents without participating in the state machine. Uses a
hybrid architecture: a deterministic poll loop handles anomaly detection
(code enforces invariants), while periodic LLM calls produce human-useful
narrative and the final summary (LLM interprets the signals). The web
dashboard renders observations in a dedicated card below the LLM Usage
section, with LLM-generated narrative entries visually distinguished
from mechanical log entries.

The introspection agent:
- Is started by the orchestrator BEFORE the first pipeline agent to
  avoid missing early events
- Polls the event stream every 5 seconds with incremental reads
- Accumulates full event history for pattern detection
- Detects content-based tool failures (not just `is_error` — also
  non-zero exit codes, `success: false`, `status: failed` in tool
  result JSON)
- Detects consecutive tool failures with similar error output, even
  when the agent changes input flags between retries. Detection is
  per-tool — interleaved diagnostic calls do not reset streaks
- Classifies errors as infrastructure (retrying won't help), transient
  (may resolve), or logic (agent needs a different approach)
- Computes wasted iteration ratio per agent (LLM calls that produced
  only failed tool results)
- Detects retry loops (per-tool, survives interleaved diagnostics)
  and max iteration exhaustion
- Detects tool bypass patterns: agents using generic tools (e.g.,
  `execute_command`) instead of purpose-built tools (e.g.,
  `execute_benchmark`), manual schema exploration via SSH, and
  manual container orchestration via SSH
- Stops automatically when the ticket reaches a terminal status
- Never transitions ticket state or modifies agent behavior (Phase 1)

Detection parameters (error patterns, thresholds, severity escalation)
are loaded from skill files (`skills/introspection/`) with private-
skills overrides (`~/.agentic-perf/private-skills/introspection.json`).
See [Skill Documentation](#skill-documentation) for the pattern.

Enabling introspection:
- Globally: `introspection.enabled = true` in config.json or
  `INTROSPECTION_ENABLED=true` env var
- Per-ticket: `custom_fields.introspection_enabled = true` (overrides
  global setting in either direction)
- Disabled by default

The introspection agent defaults to `claude-haiku-4-5` (lightweight,
cheap) since it makes periodic LLM calls across the full ticket
lifecycle. This can be overridden via `agent_models.introspection`
in config.json. Token usage is tracked per-ticket and per-agent
through the standard EventBus accounting, appearing in the LLM
Usage card alongside pipeline agents.

When the ticket closes, the agent writes a final LLM-driven summary
to `custom_fields.introspection_summary` with verdict, observations,
and actionable recommendations. This persists separately from the
live observation field. Without an LLM provider, it falls back to
deterministic stats.

Future phases will add active monitoring (Phase 2: soft-stop signals
when anomalies are detected) and corralling (Phase 3: guidance
injection to redirect off-track agents).

## Provider System

Providers are the extensibility layer — abstract interfaces with swappable
implementations. Agents interact with providers, never with specific
backends directly.

### LLM Providers

Interface: `LLMProvider` (`providers/llm/base.py`)

| Provider | Backend | Usage |
|---|---|---|
| `ClaudeLLMProvider` | Anthropic direct API or Vertex AI | Production |
| `MockLLMProvider` | Hardcoded responses | Testing |

The LLM provider handles message formatting, tool definitions, and
response parsing. Agents call `llm.complete()` with system prompt,
messages, and tools.

### Resource Providers

Interface: `ResourceProvider` (`providers/resource/base.py`)

| Provider | Type | Registration |
|---|---|---|
| `QuadsResourceProvider` | bare_metal | `~/.agentic-perf/secrets/quads/config.json` |
| `AWSResourceProvider` | cloud | `~/.agentic-perf/secrets/aws/config.json` |
| `PSAPCCResourceProvider` | gpu_cluster | `~/.agentic-perf/secrets/psap-cc/config.json` |
| `JumpstarterResourceProvider` | bare_metal | `~/.agentic-perf/secrets/jumpstarter/config.json` |

Providers are lazy-loaded by `ResourceProviderRegistry` — a provider is
only instantiated when its secrets file exists. The registry maps provider
names to class paths and secret locations.

Each provider implements:
- `check_available(requirements)` — Query what's available
- `reserve(selection, description, duration_hours)` — Create reservation
- `get_reservation_status(reservation_id)` — Poll status
- `terminate(reservation_id)` — Release resources
- `setup_ssh(hosts)` / `cleanup_ssh_keys(hosts)` — SSH key management

#### Jumpstarter

The [Jumpstarter](https://jumpstarter.dev) provider manages lab
devices — physical boards and virtual machines — from a
Jumpstarter controller. Devices are leased via label selectors:

- `board-type=renesas-rcar-s4` — any board of this type (preferred)
- `device=nxp-s32g-vnp-rdb3-03` — a specific board by name
- `target=ride4_sa8775p_sx_r3` — legacy target label

The `board_selector` directive is passed verbatim to the
Jumpstarter lease API. Code guardrails prevent the LLM from
substituting a different selector at reserve time.

Configuration requires both secrets (`~/.agentic-perf/secrets/
jumpstarter/config.json`) and the `jmp` CLI config
(`~/.config/jumpstarter/clients/<name>.yaml`) which handles gRPC
channel creation and TLS. Set `tls.insecure: true` in the CLI
config for internal deployments with self-signed certificates.

The provider uses the `jumpstarter` Python package for the
resource lifecycle (Layer 1). Device-level operations (power
cycling, serial console, firmware) use the Jumpstarter MCP
server (`jmp mcp serve`) attached to agents via
`connect_command()` (Layer 2).

Required Python packages:

- `jumpstarter` — core client and CLI
- `jumpstarter-driver-can` — CAN bus communication
- `jumpstarter-driver-composite` — composite exporter support
- `jumpstarter-driver-flashers` — board-agnostic flash orchestration
- `jumpstarter-driver-gpiod` — GPIO control (R-Car S4, S32G)
- `jumpstarter-driver-http` — HTTP image fetching (flasher dep)
- `jumpstarter-driver-network` — TCP forwarding and SSH tunnels
- `jumpstarter-driver-opendal` — storage backend abstraction
- `jumpstarter-driver-power` — power control base
- `jumpstarter-driver-pyserial` — serial console access
- `jumpstarter-driver-ridesx` — Qualcomm RideSX4 storage/power
- `jumpstarter-driver-shell` — shell command execution
- `jumpstarter-driver-snmp` — SNMP-based external power control
- `jumpstarter-driver-ssh` — SSH wrapper for board access
- `jumpstarter-driver-tftp` — TFTP image serving (flasher dep)
- `jumpstarter-driver-tmt` — TMT test framework integration
- `jumpstarter-driver-uboot` — U-Boot serial interaction
- `jumpstarter-driver-ustreamer` — video streaming
- `jumpstarter-driver-vnc` — VNC remote display

Install all drivers to support the full range of board types:

```bash
pip install jumpstarter \
  jumpstarter-driver-can \
  jumpstarter-driver-composite \
  jumpstarter-driver-flashers \
  jumpstarter-driver-gpiod \
  jumpstarter-driver-http \
  jumpstarter-driver-network \
  jumpstarter-driver-opendal \
  jumpstarter-driver-power \
  jumpstarter-driver-pyserial \
  jumpstarter-driver-ridesx \
  jumpstarter-driver-shell \
  jumpstarter-driver-snmp \
  jumpstarter-driver-ssh \
  jumpstarter-driver-tftp \
  jumpstarter-driver-tmt \
  jumpstarter-driver-uboot \
  jumpstarter-driver-ustreamer \
  jumpstarter-driver-vnc
```

When a ticket uses Jumpstarter hardware (`resource_provider =
"jumpstarter"`), the benchmark and provisioning agents
automatically attach the Jumpstarter MCP server via
`attach_jumpstarter_mcp()`. Tool filtering ensures agents see
device interaction tools (`jmp_run`, `jmp_connect`, `jmp_explore`)
but not lease management tools (`jmp_create_lease`,
`jmp_delete_lease`) which are the resource provider's job.

**Orchestrator lifecycle for Jumpstarter tickets:**

- **Image resolution:** Before the provisioning agent runs, the
  orchestrator fetches `test_images_info.json` from the build
  server and resolves the flash command deterministically. The
  result (including `flash_command`, partition URLs, and the
  orchestrator's SSH public key) is stored in
  `custom_fields.jumpstarter_flash`. Agents never touch the
  image server.
- **Pre-dispatch lease release:** When a ticket enters
  `awaiting_hardware`, any existing Jumpstarter lease is
  released. This handles the case where a user sends a ticket
  back to resource acquisition after a provisioning failure.
- **Lease sweep:** Each poll cycle, the orchestrator checks for
  orphaned leases whose tickets have reached a terminal status.
  This is a failsafe for leases not cleaned up by teardown
  (crashed orchestrators, skipped teardown, manual closure).
  The normal lease release path is the resource agent's
  `terminate()` during teardown.

### Skill Providers

Interface: `SkillProvider` (`providers/skills/base.py`)

Each benchmark harness has a skill provider that describes its capabilities
without requiring the harness to be installed:

| Provider | Harness | Discovery |
|---|---|---|
| `CrucibleSkillProvider` | Crucible | Reads multiplex.json from git repo |
| `ZathrasSkillProvider` | Zathras | Reads tool inventory from git repo |
| `KubeBurnerSkillProvider` | Kube-Burner | Static workload catalog |
| `K8sNetperfSkillProvider` | k8s-netperf | Static workload catalog |
| `BenchmarkRunnerSkillProvider` | Benchmark-Runner | Static workload catalog |
| `ClusterbusterSkillProvider` | Clusterbuster | Static workload catalog |
| `VstormSkillProvider` | Vstorm | Static workload catalog |
| `ArcaflowPluginSkillProvider` | Arcaflow Plugins | Quay.io registry discovery + container schema introspection |

`MultiHarnessSkillProvider` aggregates all configured harnesses into a
single provider. When benchmarks overlap (e.g., both Crucible and Zathras
offer fio), it prefers the default harness (Crucible).

Each provider implements:
- `list_benchmarks()` — Returns `BenchmarkSuite` objects with roles,
  min_hosts, supported params, and endpoint types
- `get_benchmark(name)` — Fetch a single suite by name
- `resolve_benchmark(requirements)` — Match natural-language description
  to a benchmark suite using keyword matching
- `generate_runfile(benchmark, params)` — Produce a run-file template

Additional methods for LLM-driven run-file construction:
- `get_runfile_schema()` — JSON schema for the run-file format
- `get_benchmark_params(benchmark)` — Valid parameters and presets
- `get_example_runfile(benchmark, endpoint_type)` — Reference run-files
- `get_private_config(suite, key)` — Organization-specific config

### Secrets Provider

Interface: `SecretsProvider` (`providers/secrets/base.py`)

`LocalSecretsProvider` reads credentials from JSON files under
`~/.agentic-perf/secrets/`. Secrets are scoped by provider name (e.g.,
`quads/config.json`, `aws/config.json`) and injected only into the
agents that need them.

### Investigation Record Provider

Interface: `InvestigationRecordProvider` (`providers/investigation/base.py`)

Provides cross-investigation memory — agents can check whether a
regression has already been investigated before starting a new
investigation, and persist outcomes for future reference.

Records are **write-once**: all investigation data (root cause,
confidence, operational metrics, change attribution) is set at
creation time and never modified. The only allowed mutations are:
- Appending build history entries (tracking regression across builds)
- Linking a Jira ticket (one-time, only if not already set)
- Closing the record (OPEN → RESOLVED lifecycle transition)

| Provider | Backend | Use Case |
|---|---|---|
| `FileRecordProvider` | JSON files on disk | Default. No external deps. Development and testing. |
| `HorreumRecordProvider` | Horreum REST API | Production use with Horreum as the data store. |
| `CompositeRecordProvider` | One writer + N readers | Migration, federated dedup, local caching. |

#### File backend (default)

Stores each record as a JSON file in a configurable directory
(default: `~/.agentic-perf/investigation-records/`). No external
services required. Queries scan all files and filter in memory —
suitable for small-to-medium record counts.

```json
{
    "investigation_records": {
        "backend": "file",
        "persist_dir": "/path/to/records"
    }
}
```

#### Horreum backend

Stores records as Horreum test runs under a dedicated test type
(`investigation_records`). The test is auto-created on first use
if it doesn't exist. Records are uploaded as schemaless JSON
payloads.

Supports Horreum API keys (`HUSR_*` tokens via `X-Horreum-API-Key`
header) and standard Bearer tokens. TLS verification can be
disabled for instances with internal CA certificates.

```json
{
    "investigation_records": {
        "backend": "horreum",
        "url": "https://horreum.example.com",
        "token": "HUSR_...",
        "tls_verify": false,
        "test_id": 426
    }
}
```

The `test_id` is optional — if omitted, the provider searches for
the test by name and creates it if missing.

#### Composite backend (multi-read)

Routes writes to a single authoritative backend and fans out reads
across multiple backends concurrently. Results are deduplicated by
`investigation_id` — the writer's copy takes precedence.

Use cases:
- **Migration**: old records in files, new records in the primary store
- **Federated dedup**: check multiple teams' record stores before
  starting an investigation
- **Local cache**: write to primary, read from local mirror too

```json
{
    "investigation_records": {
        "backend": "composite",
        "writer": {"backend": "horreum", "url": "..."},
        "readers": [
            {"backend": "horreum", "url": "..."},
            {"backend": "file", "persist_dir": "/old/records"}
        ]
    }
}
```

#### Record Schema Reference

Investigation Records use schema URI
`urn:agentic-perf:investigation-record:v1`. The full JSON Schema
is at
[`providers/investigation/schemas/investigation-record-v1.json`](../providers/investigation/schemas/investigation-record-v1.json),
generated from the Pydantic models in
`providers/investigation/models.py`.

Queryable fields that backends must support filtering on:
`state`, `anomaly_context.subsystem`, `anomaly_context.platform`,
`anomaly_context.metric`.

#### Adding new backends

New backends implement the `InvestigationRecordProvider` interface and
register in `providers/investigation/registry.py`. The interface
enforces write-once semantics — backends must not allow modification
of investigation data after creation.

Agent tools are exposed via an MCP server
(`agents/investigation/server.py`) with six tools: query, get, create,
append build history, link Jira, and close. Agents use
`AgentMCPClient.list_tools(include=...)` to expose only the tools
relevant to their role.

### External MCP Servers

`AgentMCPClient` supports both Python and non-Python MCP servers:

- `connect(server_script)` — launches a Python MCP server script
  with the current interpreter (used for all built-in agents)
- `connect_command(command, args)` — launches an arbitrary binary
  that speaks MCP over stdio (for external tools like Jumpstarter's
  `jmp mcp serve`)

Both methods share the same session management, tool routing, and
disconnect logic. `connect()` delegates to `connect_command()`
internally.

### SSH Executor

`SSHExecutor` (`providers/ssh.py`) provides async SSH command execution
with configurable timeouts, key paths, and PTY allocation. Used by the
provisioning agent for harness installation and the resource agent for
host validation.

## Event System

The `EventBus` (`providers/events.py`) provides a unified audit trail for
all agent activity. Every tool call, LLM interaction, state transition, and
error is recorded.

### Event Types

| Event | When | Key Data |
|---|---|---|
| `agent_started` | Agent begins processing | system_prompt, initial_messages |
| `agent_finished` | Agent completes | — |
| `agent_error` | Agent encounters an error | reason |
| `llm_request` | Before LLM call | iteration number |
| `llm_response` | After LLM responds | iteration, stop_reason, tool_calls, text |
| `tool_called` | Before tool execution | tool name, input |
| `tool_result` | After tool execution | tool name, is_error, content |
| `tool_skipped` | Tool call not executed | tool name, reason |
| `transition` | Ticket status change | new status, comment |
| `comment` | Comment added to ticket | body |

### Storage

Events are stored in two places:
- **In-memory** — For real-time queries via the state store API
- **JSONL files** — `~/.agentic-perf/logs/{ticket_id}.jsonl` for persistence
  and transcript rendering

The web dashboard polls the event API for live updates. The CLI `transcript`
command reads from the JSONL files.

### LLM Usage Tracking

Token usage and timing are captured via OpenTelemetry instrumentation
of the LLM SDKs (Anthropic, OpenAI). The `opentelemetry-instrumentation-
anthropic` and `opentelemetry-instrumentation-openai` packages
automatically produce spans for every LLM call with token counts,
model info, and duration.

A custom `EventBusSpanProcessor` (`providers/telemetry.py`) bridges
OTLP spans into the EventBus for per-ticket accumulation:

1. The agent loop sets a ticket ID on the OpenTelemetry context before
   each LLM call
2. The LLM SDK instrumentation produces a span with token usage
3. The span processor extracts usage from the span attributes,
   calls `EventBus.record_llm_usage()` for in-memory accumulation,
   and emits a `llm_usage` event to the JSONL log
4. The usage API (`/api/v1/tickets/{id}/usage`) computes totals from
   the persisted `llm_usage` events, which works across process
   boundaries (the state store and orchestrator are separate processes)

The OTLP span processor is the sole source of token accounting —
LLM providers do not extract usage from API responses. This keeps
the data path simple: SDK → instrumentor → span → span processor →
EventBus.

Optionally, spans can also be exported to an external OTLP collector
(Jaeger, Grafana Tempo, etc.) by configuring `telemetry.otlp_endpoint`
in `~/.agentic-perf/config.json`.

Cost estimation uses pricing from `providers/cost/pricing.yaml`, with
user overrides at `~/.agentic-perf/pricing.yaml`. The `estimate_cost()`
function matches model names by prefix (e.g., `claude-sonnet-4-6`
matches `claude-sonnet-4`) and falls back to default pricing for
unknown models.

Telemetry dependencies are optional — install with
`pip install -e ".[telemetry]"`. Without them, the system works
normally but token tracking is disabled.

### LLM Budget Guardrails

Configurable budget limits prevent runaway LLM costs at two levels:

**Per-ticket budgets** are set via `custom_fields.llm_budget`:

```json
{
  "llm_budget": {
    "max_tokens": 200000,
    "max_cost_usd": 5.00,
    "warn_pct": 80
  }
}
```

The agent loop checks the budget after each LLM call. At the warn
threshold (default 80%), a comment is posted. If the limit is
exceeded, the ticket transitions to `awaiting_customer_guidance`
so the user can increase the budget or abort.

**System-wide session budgets** are configured in
`~/.agentic-perf/config.json`:

```json
{
  "llm_budget": {
    "session_cost_usd": 50.00
  }
}
```

The orchestrator checks the session budget before each dispatch
cycle. If exceeded, no new agents are started (existing ones
finish). The session budget is scoped to the orchestrator process
lifetime, not calendar boundaries.

Both checks are deterministic — no LLM call needed. Budgets are
optional: if not configured, no checks run. The budget logic lives
in `providers/budget.py`.

## Orchestrator

The orchestrator (`orchestrator/`) is the control loop that drives the
pipeline:

1. **Config** (`config.py`) — Loads settings from `~/.agentic-perf/config.json`
   with environment variable overrides. Configures LLM backend, state store
   URL, harness repo URLs, SSH key, and poll interval.

2. **Poller** (`poller.py`) — Queries the state store for tickets in
   non-terminal statuses.

3. **Dispatcher** (`dispatcher.py`) — Maps ticket status to agent type and
   creates agent instances. Tracks active tickets to prevent duplicate
   dispatches. The same agent class (ResourceAgent) handles both
   `awaiting_hardware` (create mode) and `awaiting_teardown` (teardown mode).

4. **Main loop** (`main.py`) — Initializes providers, creates the dispatcher,
   and runs the poll-dispatch-process loop at the configured interval
   (default: 3 seconds).

### Startup (`start.sh`)

```bash
./start.sh
```

1. Reads `~/.agentic-perf/config.json` (required)
2. Starts FastAPI state store in background on configured port
3. Waits for `/api/v1/health` endpoint to respond
4. Prints web dashboard URL
5. Starts orchestrator in foreground
6. Cleanup trap kills state store on exit

## State Store API

The state store (`state_store/`) is a FastAPI application serving both the
REST API and the web dashboard.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/health` | Liveness check with ticket counts |
| POST | `/api/v1/tickets` | Create ticket |
| GET | `/api/v1/tickets` | List tickets (optional `?status=` filter) |
| GET | `/api/v1/tickets/{id}` | Get single ticket |
| POST | `/api/v1/tickets/{id}/transition` | Change ticket status |
| PATCH | `/api/v1/tickets/{id}/fields` | Update custom fields |
| POST | `/api/v1/tickets/{id}/comments` | Add comment |
| GET | `/api/v1/tickets/{id}/comments` | List comments |
| GET | `/api/v1/tickets/{id}/events` | Get events (pagination via `?since=&limit=`) |

### Ticket Model

```python
class Ticket:
    id: str                          # UUID
    summary: str                     # One-line description
    description: str                 # Full request text
    status: TicketStatus             # Current state machine position
    custom_fields: dict[str, Any]    # Agent-written structured data
    comments: list[Comment]          # Conversation thread
    created_at: datetime
    updated_at: datetime
    previous_status: TicketStatus    # For HITL resume
    transition_seq: int              # Monotonic counter
```

Custom fields are the structured workspace where agents store their
outputs: triage results, resource allocations, benchmark run IDs, review
verdicts, and operational directives.

## Skill Documentation

The `skills/` directory contains per-harness documentation that agents read
at runtime through `list_harness_docs` and `read_harness_doc` tools:

```
skills/
  crucible/
    cdm-query-guide.md     # How to query the CommonDataModel for results
    kube-endpoints.md       # Kubernetes endpoint configuration
    run-file-pitfalls.md    # Common run-file mistakes and solutions
    uperf-run-file.md       # Uperf-specific run-file guide
    userenv-guide.md        # User environment selection
  zathras/
    local-config-guide.md   # Local execution configuration
    scenario-construction.md # Building test scenarios
  kube-burner/
    config-guide.md         # Configuration reference
    workloads.md            # Available workloads
  k8s-netperf/
    config-guide.md         # Configuration reference
    workloads.md            # Available workloads and profiles
  benchmark-runner/
    workloads.md            # Supported workloads (OpenShift + VM)
  clusterbuster/
    config-guide.md         # Configuration reference
    workloads.md            # Cluster stress workloads
  vstorm/
    config-guide.md         # Configuration reference
    workloads.md            # VM stress workloads
  introspection/
    error-patterns.yaml         # Error classification regexes
    detection-thresholds.yaml   # Anomaly detection thresholds
    observer-prompt.md          # LLM system prompt for narrative
    tool-bypass-patterns.yaml   # Tool misuse detection patterns
```

This is the "skills" layer from the design philosophy: agents learn what a
harness can do by reading its skill docs, not from hardcoded knowledge.

## Key Design Patterns

### Ticket as Single Source of Truth

All durable state lives on the ticket document. Agents are stateless —
they can crash at any point, and a new instance can pick up where the
previous one left off by reading the ticket. This is why `custom_fields`
is a free-form dictionary: each agent writes its structured output there.

### MCP Tool Scoping

Each agent has its own MCP tool server with only the tools relevant to its
role. The triage agent can discover benchmarks but cannot SSH. The review
agent can query metrics but cannot modify infrastructure. This provides
natural trust boundaries.

### Provider Registry

Resource providers are discovered at startup based on which secret files
exist. If `~/.agentic-perf/secrets/aws/config.json` exists, the AWS
provider is available. If it doesn't, the system works fine without it.
This makes deployment flexible: a team with only bare-metal access
configures only QUADS; a cloud-first team configures only AWS.

### Skill Provider Aggregation

The `MultiHarnessSkillProvider` presents all configured harnesses as a
unified catalog. When triage resolves "run a network test," it finds
matching benchmarks across all harnesses and selects the best fit. Adding
a new harness extends the catalog without touching any agent code.

### Event-Driven Observability

Every agent action is captured as an event. This enables:
- **Live dashboards** — Web UI polls for events and renders them in
  real time
- **Post-hoc analysis** — CLI `transcript` command renders the full
  agent conversation
- **Debugging** — Every tool call and its result is recorded, making
  it possible to trace exactly what an agent did and why
