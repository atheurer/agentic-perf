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
| **Triage** | Parse user intent, form hypothesis | Ticket read/write |
| **Resource** | Find and reserve hardware | QUADS API, AWS EC2, PSAP Control Center |
| **Provisioning** | Install benchmark harness on hosts via SSH | SSH, SCP, secrets vault |
| **Benchmark** | Generate run configuration, execute, monitor | Harness CLI, schema validator |
| **Review** | Analyze results, produce verdict | Metric query (CDM), result archive |

Each agent has its own MCP tool server with scoped capabilities — the review
agent cannot SSH, and the provisioning agent cannot query metrics. Agents are
stateless and crash-recoverable: all durable state lives on the ticket.

## Ticket Lifecycle

```
new → triage_pending → awaiting_hardware → awaiting_provision →
executing_benchmark → awaiting_review → awaiting_teardown → closed
```

Any stage can pause at `awaiting_customer_guidance` for human input, and the
user can reply to resume.

## Supported Benchmark Harnesses

Harness knowledge is provided by **skill providers**, not hardcoded in agents.
Adding a new harness means adding a skill provider — no agent code changes.

Currently supported:
- **[Crucible](https://github.com/perftool-incubator/crucible)** — fio, uperf,
  trafficgen, and other benchmarks via remotehosts or Kubernetes endpoints
- **[Zathras](https://github.com/RedHatPerf/zathras)** — STREAM, fio, iozone,
  uperf, HammerDB, SPECjbb, CoreMark, Linpack, and more via local execution

## Resource Providers

- **Null** — user provides hosts and SSH keys directly
- **QUADS** — automated bare-metal reservation from a self-service lab
- **AWS EC2** — on-demand cloud instances
- **PSAP Control Center** — GPU cluster reservations

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
        "model": "claude-sonnet-4-6"
    },
    "state_store": {
        "url": "http://localhost:8090",
        "port": 8090
    },
    "poll_interval": 3.0,
    "ssh_key": "~/.ssh/id_ed25519"
}
```

Set your API key:

```bash
export ANTHROPIC_API_KEY="your-key-here"
```

For Vertex AI backend, set `backend`, `project_id`, and `region` in the
`llm` section and authenticate with `gcloud auth application-default login`.

### Private Skills

Benchmark harness installation details (internal registries, install flags)
go in `~/.agentic-perf/private-skills/`. See `sample-private-skills/` for
templates.

### Secrets

Credentials (registry auth tokens, provider API keys) are stored in
`~/.agentic-perf/secrets/` and resolved at runtime by the secrets provider.
Secrets are never stored in skill configs, agent prompts, or this repository.

## Usage

### Start the system

```bash
./start.sh
```

This launches the state store (FastAPI on port 8090) and the orchestrator.

### Submit a request

```bash
python3 cli.py submit \
  "Run a 4K random read fio test on my storage server" \
  -d "Controller: 198.51.100.1. Endpoint: 198.51.100.2. SSH key: ~/.ssh/id_ed25519. Use crucible."
```

### Watch progress

```bash
python3 cli.py watch <TICKET_ID> -f -v
```

### Respond to agent questions

```bash
python3 cli.py reply <TICKET_ID> "Approved"
```

### Other commands

```bash
python3 cli.py list              # List all tickets
python3 cli.py show <TICKET_ID>  # Show ticket details
python3 cli.py transcript <ID>   # Show full agent conversation log
python3 cli.py health            # Check state store health
python3 cli.py cleanup           # Find orphaned cloud instances
```

## Architecture

```
agentic-perf/
  cli.py                  # CLI entrypoint
  start.sh                # Launch state store + orchestrator

  orchestrator/            # Async poll loop, config, dispatcher
  agents/                  # One agent per pipeline stage
    triage/                #   Parse user intent
    resource/              #   Acquire hardware
    provisioning/          #   Install harness via SSH
    benchmark/             #   Generate run-file, execute
    review/                #   Analyze results, produce verdict

  state_store/             # FastAPI REST API + ticket store
  providers/
    llm/                   #   Claude (direct + Vertex) and mock
    resource/              #   QUADS, AWS EC2, PSAP Control Center
    secrets/               #   File-based local secrets
    skills/                #   Harness capability providers

  sample-private-skills/   # Example harness configs (templates)
  skills/                  # Skill documentation
  docs/                    # Design docs and guides
  tests/                   # pytest test suite
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

- [Design Philosophy](docs/design-philosophy.md) — why the system is designed the way it is
- [Collaborative Negotiation](docs/collaborative-negotiation.md) — how agents plan and resolve conflicts
- [LLM Run-File Generation](docs/design-llm-runfile-generation.md) — how benchmark agents construct run configurations
- [Jira Integration](docs/jira-polling-integration.md) — replacing the local state store with Jira Cloud
- [E2E Testing Guide](docs/e2e-testing.md) — running the full pipeline against real hardware
- [Presentation](docs/presentation-agentic-perf.md) — overview slides explaining the project's motivation and results

## License

[Apache License 2.0](LICENSE)
