# agentic-perf

Multi-agent system for autonomous performance testing. Submit a natural-language
request, and specialized AI agents triage it, acquire hardware, install tooling,
execute benchmarks, and deliver a structured analysis — without a human in the
execution loop.

## The Problem

Ad-hoc performance work (new hardware evaluations, kernel comparisons, customer
escalations) takes days of engineer time: reserving hardware, installing
benchmark harnesses, writing run configurations, babysitting execution,
analyzing results. Traditional CI/CD automation handles repeatable regression
testing well, but can't handle the variability of investigative work.

## The Approach

Five specialized agents collaborate through a shared ticket (a structured JSON
document that serves as the single source of truth):

| Agent | Role | Scoped Tools |
|---|---|---|
| **Triage** | Parse user intent, form hypothesis, select benchmark | Benchmark discovery, resolution |
| **Resource** | Find and reserve hardware from providers | QUADS API, AWS EC2, PSAP Control Center |
| **Provisioning** | Install benchmark harness on hosts via SSH | SSH, platform contracts, K3s |
| **Benchmark** | Construct run configuration, execute, monitor | Harness docs, schema validation, CLI |
| **Review** | Analyze results, produce verdict | Metric query, result comparison |

Each agent has its own MCP tool server with scoped capabilities — the review
agent cannot SSH, and the provisioning agent cannot query metrics. Agents are
stateless and crash-recoverable: all durable state lives on the ticket.

## Ticket Lifecycle

### Ad-hoc test execution

```
new → triage_pending → awaiting_hardware → awaiting_provision →
executing_benchmark → awaiting_review → awaiting_teardown → closed
```

### Recursive investigation

```
new → triage_pending → gathering_context → planning_investigation →
awaiting_provision → executing_benchmark → evaluating_convergence →
    │── loop back to planning_investigation (refine params)
    │── loop back to awaiting_provision (re-flash hardware)
    └── synthesizing_results → awaiting_teardown → closed
```

Both paths can pause at `awaiting_customer_guidance` for human input, and the
user can reply to resume. Tickets can also be aborted to skip directly to
teardown.

## Supported Benchmark Harnesses

Harness knowledge is provided by **skill providers**, not hardcoded in agents.
Adding a new harness means adding a skill provider — no agent code changes.

| Harness | Benchmarks | Endpoint Type |
|---|---|---|
| **[Crucible](https://github.com/perftool-incubator/crucible)** | fio, uperf, trafficgen, iperf, cyclictest, oslat | remotehosts, Kubernetes |
| **[Zathras](https://github.com/redhat-performance/zathras)** | STREAM, fio, iozone, uperf, HammerDB, SPECjbb, CoreMark, Linpack | local execution |
| **[Kube-Burner](https://github.com/kube-burner/kube-burner)** | Kubernetes cluster load generation (node-density, cluster-density, etc.) | Kubernetes |
| **[k8s-netperf](https://github.com/cloud-bulldozer/k8s-netperf)** | Kubernetes network performance (iperf3, netperf, uperf) | Kubernetes |
| **[Benchmark-Runner](https://github.com/redhat-performance/benchmark-runner)** | stressng, hammerdb, vdbench, fio, uperf on OpenShift/VMs | OpenShift, VMs |
| **[Clusterbuster](https://github.com/redhat-performance/clusterbuster)** | OpenShift cluster stress testing (pod density, startup latency) | Kubernetes |
| **[Vstorm](https://github.com/gqlo/vstorm)** | VM storage and memory stress testing | VMs |
| **[Ioscale](https://github.com/ekuric/ioscale)** | VM storage I/O and database benchmarks (fio, HammerDB with MariaDB/PostgreSQL) | Kubernetes (KubeVirt VMs) |
| **[Forge](https://github.com/openshift-psap/forge)** | LLM inference performance (RHAIIS/vLLM, LLM-D); 53+ model presets, FP8/W8A8/W4A16 quantization | Kubernetes (GPU) |
| **[Arcaflow Plugins](https://github.com/arcalot/arcaflow-plugin-catalog)** | stress-ng, fio, sysbench, uperf, iperf3, CoreMark-PRO, and more — containerized benchmarks from the Arcalot community | remotehosts (podman) |

## Resource Providers

- **Null** — user provides hosts and SSH keys directly in the ticket
- **QUADS** — automated bare-metal reservation from a self-service lab
  (filters by model, NIC vendor/speed, disk type)
- **AWS EC2** — on-demand cloud instances with configurable instance types,
  AMIs, and root volume sizing
- **PSAP Control Center** — GPU cluster reservations for AI/ML workloads
  (returns cluster API URL, not SSH hosts)

## Prerequisites

- Python 3.12+
- An Anthropic API key (direct API or Google Cloud Vertex AI)
- SSH access to target hosts (for benchmark execution)

## Installation

```bash
git clone https://github.com/atheurer/agentic-perf.git
cd agentic-perf
pip install -e .
```

## Configuration

Create `~/.agentic-perf/config.json`:

```json
{
    "llm": {
        "provider": "claude",
        "model": "claude-haiku-4-5",
        "backend": "vertex",
        "project_id": "your-gcp-project",
        "region": "us-east5"
    },
    "state_store": {
        "url": "http://localhost:8090",
        "port": 8090
    },
    "poll_interval": 3.0,
    "ssh_key": "~/.ssh/id_ed25519",
    "harness_repos": {}
}
```

For direct Anthropic API, use `"backend": "direct"` and set:

```bash
export ANTHROPIC_API_KEY="your-key-here"
```

For Vertex AI backend, authenticate with `gcloud auth application-default login`.

### Local Directory (`~/.agentic-perf/`)

All user-specific configuration, credentials, and runtime data lives outside
the repository in `~/.agentic-perf/`:

```
~/.agentic-perf/
  config.json              # Main configuration (LLM, state store, SSH key, poll interval)
  private-skills/          # Org-specific harness install configs (JSON)
    crucible.json           #   e.g., internal registry URLs, install flags
    zathras.json            #   See sample-private-skills/ for templates
  secrets/                 # Credentials resolved at runtime by the secrets provider
    crucible/               #   e.g., container registry auth tokens
    aws/                    #   e.g., access keys and session tokens
    quads/                  #   e.g., QUADS API credentials
    psap-cc/                #   e.g., PSAP Control Center API tokens
  logs/                    # Agent conversation transcripts (JSONL per ticket)
```

**Private skills** define how *your organization* installs a benchmark harness
(internal registries, install flags, vault references). The public skill
definitions in the repo define *what* a harness can do. See
`sample-private-skills/` for templates you can copy and customize.

**Secrets** are never stored in skill configs, agent prompts, or this
repository. They are injected at runtime by the secrets provider, scoped to
the agent that needs them.

## Usage

### Start the system

```bash
./start.sh
```

This launches the state store (FastAPI on port 8090) and the orchestrator.
The web dashboard is available at `http://localhost:8090`.

### Submit a request

```bash
agentic-perf submit \
  "Run a 4K random read fio test on my storage server" \
  -d "Controller: 198.51.100.1. Endpoint: 198.51.100.2. SSH key: ~/.ssh/id_ed25519. Use crucible."
```

### Watch progress

```bash
agentic-perf watch <TICKET_ID> -f        # Follow mode
agentic-perf watch <TICKET_ID> -f -v     # Verbose: show tool calls and LLM interactions
```

### Respond to agent questions

```bash
agentic-perf reply <TICKET_ID> "Approved"
```

### Abort a paused ticket

```bash
agentic-perf abort <TICKET_ID>                   # Skip to teardown
agentic-perf abort <TICKET_ID> "wrong config"     # With reason
```

### View agent conversation transcript

```bash
agentic-perf transcript <TICKET_ID>               # Full conversation
agentic-perf transcript <TICKET_ID> --agent triage-agent   # Single agent
agentic-perf transcript <TICKET_ID> --json         # Raw events as JSON
```

### Other commands

```bash
agentic-perf list                          # List all tickets
agentic-perf list -s executing_benchmark   # Filter by status
agentic-perf show <TICKET_ID>             # Show ticket details and custom fields
agentic-perf health                        # Check state store (ticket counts)
agentic-perf cleanup --older-than 24       # Find orphaned AWS instances
agentic-perf cleanup --terminate -y        # Terminate orphaned instances
```

## Web Dashboard

The state store serves a web dashboard at `http://localhost:8090` with:

- **Ticket list** — all tickets with status badges, filterable by status
- **Ticket detail** — live event stream showing agent activity, tool calls,
  LLM responses, and transitions in real time
- **Agent navigator** — jump between agent phases in the event stream

The dashboard requires no build step (vanilla HTML/JS/CSS) and auto-refreshes.

## Architecture

```
agentic-perf/
  cli.py                  # CLI entrypoint (submit, watch, reply, abort, transcript)
  start.sh                # Launch state store + orchestrator

  orchestrator/            # Async poll loop, config, dispatcher
  agents/                  # One agent per pipeline stage
    triage/                #   Parse user intent → hypothesis + benchmark
    resource/              #   Acquire hardware (QUADS, AWS, PSAP, or user-provided)
    provisioning/          #   Install harness via SSH, optional K3s for kube endpoints
    benchmark/             #   Construct run-file from docs + schema, execute
    review/                #   Retrieve results, analyze, compare to hypothesis
    investigation/         #   MCP server for Investigation Record CRUD

  state_store/             # FastAPI REST API + ticket store + web dashboard
    static/                #   Single-page web dashboard
  providers/
    llm/                   #   Claude (direct + Vertex) and mock providers
    resource/              #   QUADS, AWS EC2, PSAP Control Center, provider registry
    investigation/         #   Investigation Record storage (pluggable backends)
    secrets/               #   File-based local secrets
    skills/                #   10 harness skill providers + multi-provider aggregator
    events.py              #   Event bus for audit trail (JSONL per ticket)
    ssh.py                 #   Async SSH executor

  sample-private-skills/   # Example harness configs (templates)
  skills/                  # Skill documentation per harness
    crucible/              #   CDM queries, run-file pitfalls, kube endpoints, userenvs
    zathras/               #   Scenario construction, local config
    kube-burner/           #   Config guide, workloads
    k8s-netperf/           #   Config guide, workloads
    benchmark-runner/      #   Workloads (OpenShift + VM)
    clusterbuster/         #   Config guide, workloads
    vstorm/                #   Config guide, workloads
    ioscale/               #   VM storage + database workloads
    forge/                 #   LLM inference models, workload profiles
  docs/                    # Design docs and guides
  tests/                   # pytest test suite (16 test files)
```

## Design Principles

1. **LLM decides intent; code enforces invariants.** The LLM handles ambiguity
   (natural language → benchmark config). Code handles correctness (schema
   validation, hostname resolution, secret injection).

2. **Multiple focused agents over one omniscient agent.** Trust boundaries,
   replaceability, and debuggability outweigh orchestration complexity.

3. **Skills for capabilities, prompts for reasoning, code for correctness.**
   Each piece of knowledge has exactly one home.

4. **The ticket is the single source of truth.** Agents are stateless; state
   lives where everyone can read it.

5. **Contracts over hardcoded procedures.** Installation follows declarative
   contracts from skill configs, not imperative scripts.

6. **Human on the loop, not in the loop.** AI handles execution; humans own
   the judgment calls.

See [docs/design-philosophy.md](docs/design-philosophy.md) for the full
rationale behind each principle.

## Testing

```bash
python3 -m pytest tests/ -v
```

Unit tests use a mock LLM provider and don't require SSH or API keys.

For end-to-end testing with real infrastructure, see
[docs/e2e-testing.md](docs/e2e-testing.md).

## Documentation

- [Architecture](docs/architecture.md) — system architecture, agents, providers, state machine
- [Design Philosophy](docs/design-philosophy.md) — why the system is designed the way it is
- [CLI Reference](docs/cli-reference.md) — complete command reference with examples
- [Adding a Harness](docs/adding-a-harness.md) — how to add a new benchmark harness
- [LLM Run-File Generation](docs/design-llm-runfile-generation.md) — how benchmark agents construct run configurations
- [E2E Testing Guide](docs/e2e-testing.md) — running the full pipeline against real hardware
- [CDM API Reference](docs/cdm-api-reference.md) — querying Crucible's metric storage
- [Collaborative Negotiation](docs/collaborative-negotiation.md) — future design for multi-agent planning
- [Jira Integration](docs/jira-polling-integration.md) — replacing the local state store with Jira Cloud
- [Web Dashboard](docs/web-ui.md) — dashboard architecture and event stream
- [Presentation](docs/presentation-agentic-perf.md) — overview slides explaining the project's motivation and results

## License

[Apache License 2.0](LICENSE)
