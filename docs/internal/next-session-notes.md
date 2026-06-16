# Next Session Notes

## Recent Completions (Since April 2026)

### Harness Ecosystem Expansion
Five new harnesses added, bringing the total to seven:
- **Kube-Burner** — K8s cluster load generation (PRs #19-#22)
- **k8s-netperf** — K8s network performance, iperf3/netperf/uperf (PRs #26, #28, #31-#33, #44-#45)
- **Benchmark-Runner** — OpenShift + VM workloads, stressng/hammerdb/vdbench (PRs #27, #29-#30, #41-#48)
- **Clusterbuster** — OpenShift cluster stress testing (PR #49)
- **Vstorm** — VM storage/memory stress via KubeVirt (PRs #51-#52)

Each harness follows the established pattern: skill provider, keyword map,
skill docs (workloads.md + config-guide.md), tests, and registration.

### LLM-Driven Run-File Generation (Done)
Benchmark agent constructs run.json directly from natural language via LLM
instead of `generate_run_file` templates. Design doc:
[design-llm-runfile-generation.md](design-llm-runfile-generation.md).

### Triage Directives (Done)
`directives` field with `on_existing_install`, `harness`,
`user_pre_run_approval`, `host_cleanup`, `endpoint_type`, plus arbitrary keys.

### Clean Up Stale Valkey (Done)
Pre-flight check in `execute_benchmark` stops stale `crucible-valkey`
containers before `crucible run`.

### Remote Skills Phase 1 (Done)
Repo caching, doc tools, local skill docs. Orchestrator no longer needs
local harness installs for benchmark discovery.

### Review Agent Harness-Agnostic (Done, PR #13)
Discovers result retrieval via skill providers instead of hardcoding CDM.

### Absent Suite Blocking (Done)
Orchestrator blocks hardware allocation when no harness covers the
requested benchmark. Ticket pauses at `awaiting_customer_guidance`.

### Transcript CLI (Done)
`agentic-perf transcript` command renders full agent conversations from
JSONL event logs. Supports `--json` and `--agent` filtering.

### Abort Command (Done)
`agentic-perf abort` skips paused tickets to teardown.

### AWS Cleanup Command (Done)
`agentic-perf cleanup` finds/terminates orphaned EC2 instances.

### K3s/Kube Endpoint Support (Done, PR #12)
Single-host K3s-on-AWS for kube endpoints.

### Stdout Truncation Removed (Done, PR #25)
Tool results no longer truncated — agents see full command output.

---

## In Progress / Next Up

### Persist Validated Run-File (Priority: Medium)
Save validated run-file to `custom_fields.validated_run_file`. On
re-dispatch, skip run-file construction and go straight to execution.
Saves LLM iterations on retries.

### Harness Update Directive (Priority: Medium)
`update_harness: true` directive → provisioning agent runs harness update
command. Wire the execution config's `update_command` into the flow.

### Collaborative Negotiation (Priority: Medium)
Replace linear pipeline with concurrent Benchmark ↔ Resource planning
phase. Design doc: [collaborative-negotiation.md](collaborative-negotiation.md).
Not yet started.

### Orchestrator Fork Architecture (Priority: Medium)
Fork per-ticket processes so orchestrator restarts don't kill in-flight
agent work. Currently a restart kills all active agents.

### Checkpoint/Restart (Priority: Low)
`rewind` CLI command to transition ticket back to a previous state and
re-run a failed phase, preserving accumulated context (hardware IPs,
SSH key, harness version).

### Jira Backend (Priority: Low)
Replace local FastAPI state store with Jira Cloud as backend. Design doc:
[jira-polling-integration.md](jira-polling-integration.md). Not yet started.

### Migrate Crucible Cleanup to Crucible Project (Priority: Low)
Crucible-specific uninstall logic in provisioning agent should become
`crucible uninstall` upstream.

---

## Known Issues

### QUADS Orphaned Assignments
Terminate API returns 500 for assignments with expired schedules.
Teardown agent should handle this gracefully.

### Zathras Install Dependencies
- Install script doesn't install `gh` (GitHub CLI)
- `dnf config-manager --add-repo` broken on dnf5 (Fedora 41+)
- Should be more self-contained about deps

---

## Documentation (Updated June 2026)

New docs added this session:
- `docs/architecture.md` — system architecture, agents, providers, state machine
- `docs/cli-reference.md` — complete CLI command reference
- `docs/adding-a-harness.md` — step-by-step guide for adding a new harness
- README.md refreshed with all 7 harnesses, full CLI, web dashboard
