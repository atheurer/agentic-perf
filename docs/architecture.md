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

All communication between components goes through the state store's REST API.
Agents never talk to each other directly — they read and write the shared
ticket document.

## State Machine

Tickets progress through a defined set of statuses. Each status maps to an
agent that processes the ticket at that stage.

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

### Status-to-Agent Mapping

| Status | Agent | Mode |
|---|---|---|
| `triage_pending` | TriageAgent | — |
| `awaiting_hardware` | ResourceAgent | create |
| `awaiting_provision` | ProvisioningAgent | — |
| `executing_benchmark` | BenchmarkAgent | — |
| `awaiting_review` | ReviewAgent | — |
| `awaiting_teardown` | ResourceAgent | teardown |

Terminal statuses (`closed`, `awaiting_customer_guidance`) do not dispatch
agents. `awaiting_customer_guidance` resumes to the previous status when the
user replies.

### Special Transitions

- **Rerun loop:** `awaiting_review` can transition back to `triage_pending`
  for iterative testing.
- **Abort:** From `awaiting_customer_guidance`, the user can jump directly to
  `awaiting_teardown` to skip remaining work.

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

Providers are lazy-loaded by `ResourceProviderRegistry` — a provider is
only instantiated when its secrets file exists. The registry maps provider
names to class paths and secret locations.

Each provider implements:
- `check_available(requirements)` — Query what's available
- `reserve(selection, description, duration_hours)` — Create reservation
- `get_reservation_status(reservation_id)` — Poll status
- `terminate(reservation_id)` — Release resources
- `setup_ssh(hosts)` / `cleanup_ssh_keys(hosts)` — SSH key management

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
