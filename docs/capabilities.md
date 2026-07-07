# Agent Capabilities and Tool Inventory

This document lists all tools, APIs, and data sources available to each agent. This is the authoritative source for what agents can and cannot do.

---

## Overview

Each agent has scoped MCP tools. The system enforces **least privilege**: agents can only use tools required for their specific role. This ensures focused reasoning and prevents accidental misuse of powerful capabilities.

---

## Triage Agent

**Role**: Parse user intent, select benchmark harness, form hypothesis

### Read Tools
- **list_benchmarks()** — List all available benchmark suites
- **get_benchmark_details(name)** — Get suite configuration, parameters, endpoint types
- **resolve_benchmark(description, workload_type?)** — Match natural-language request to benchmark

### External APIs (Read-Only)
- Skill provider catalog (all installed harnesses)
- Previous ticket search (lookup related prior tests)

### Constraints
- ❌ **Cannot SSH** to hosts
- ❌ **Cannot execute** benchmarks
- ❌ **Cannot reserve** hardware
- ❌ **Cannot query** metrics from results

### Output
Structured **triage result** with:
- Selected benchmark suite
- Hypothesis to test
- Recommended parameters
- Resource profile (# of hosts, role assignments, required capabilities)

---

## Resource Agent

**Role**: Find, reserve, validate hardware

### Read Tools
- **parse_host_config(text)** — Extract host IPs, roles, SSH credentials from free-form text
- **validate_host(host, user, key_path)** — SSH to host, verify connectivity and system info (OS, CPU, RAM)

### Write Tools (Resource Providers)
- **reserve_resources(provider, spec)** — Reserve hardware from:
  - `provider="null"` — User-provided (stored in ticket)
  - `provider="quads"` — QUADS bare-metal self-service
  - `provider="aws"` — AWS EC2 on-demand
  - `provider="psap"` — PSAP Control Center GPU clusters

### External APIs
- QUADS API (host search, assignment, SSH key management)
- AWS EC2 API (instance launch, security groups, termination)
- PSAP Control Center API (cluster reservation, kubeconfig endpoints)
- SSH connections to candidate hosts (read-only validation)

### Constraints
- ❌ **Cannot install** software
- ❌ **Cannot execute** benchmarks
- ❌ **Cannot modify** system configuration
- ❌ **Cannot query** metrics
- ⚠️ **SSH is read-only** for validation; no `sudo` or configuration changes

### Output
Structured **resource result** with:
- Reserved host list (IP address, hostname, role, SSH user, SSH key path)
- Provider metadata (reservation ID, billing info, termination deadline)
- Validation report (OS, CPU count, RAM, NIC model/count)

---

## Provisioning Agent

**Role**: Install benchmark harness, configure platform

### Read Tools
- **list_harnesses()** — List installed harnesses and their install modes
- **get_harness_install_contract(harness, platform)** — Get install procedure: required packages, pre-conditions, post-conditions, secrets

### Write Tools
- **ssh_execute(host, user, key_path, commands)** — Execute shell commands on host (SSH)
- **transfer_file(host, user, key_path, local_path, remote_path)** — Copy files to host

### External APIs
- SSH to reserved hosts (full shell access, can run arbitrary commands with constraints)
- Package managers (yum, apt, etc. via SSH)
- Harness repositories (git clone, etc. via SSH)

### Constraints
- ❌ **Cannot query** metrics
- ❌ **Cannot execute** benchmarks (only configure for them)
- ❌ **Cannot reserve** additional resources
- ⚠️ **SSH is scoped**: Commands restricted by command policy per agent
  - Allowed: package install, git, harness CLI, configuration
  - Denied: `rm -rf /`, `mkfs`, `dd to /dev/`, `chmod 777`, `shutdown`, etc.

### Output
Structured **provisioning result** with:
- Installation status (success, warnings, errors)
- Harness version and configuration
- Test readiness report (all prerequisites met, ready for benchmark execution)

---

## Benchmark Agent

**Role**: Construct run configuration, execute, monitor

### Read Tools
- **get_harness_schema(harness)** — Get JSON schema for run configuration
- **get_harness_defaults(harness)** — Get recommended default parameters
- **read_skill_doc(skill_id, doc_name)** — Read harness-specific documentation (run-file pitfalls, best practices)

### Write Tools
- **validate_runfile(harness, runfile)** — Validate run configuration against schema
- **submit_benchmark(runfile)** — Execute benchmark, returns run ID

### Read (Execution Monitoring)
- **get_benchmark_status(run_id)** — Poll for execution progress (stage, elapsed time, errors)
- **get_benchmark_intermediate_results(run_id)** — Get partial results as they become available

### External APIs
- Harness CLI (crucible, zathras, kube-burner, etc.) via SSH
- SSH to provisioned hosts (monitor via log files, CLI queries)
- Benchmark result storage (retrieve final results)

### Constraints
- ❌ **Cannot SSH directly** (uses provisioned harness via CLI)
- ❌ **Cannot modify** run configuration mid-test
- ❌ **Cannot query** historical metrics (only current run results)
- ❌ **Cannot reserve** resources or install software
- ⚠️ **Schema validation is mandatory**: No invalid run-files reach execution

### Output
Structured **benchmark result** with:
- Benchmark status (success, partial, failed)
- Raw metrics (throughput, latency, CPU usage, errors)
- Run configuration (for audit trail)
- Warnings/errors encountered

---

## Review Agent

**Role**: Analyze results, produce verdict, recommendations

### Read Tools
- **get_benchmark_results(run_id)** — Retrieve complete result data
- **query_metrics(run_id, metric_name)** — Query specific metrics (throughput, latency, variance, etc.)
- **compare_results(run_id1, run_id2, metric_list)** — Compare two benchmark runs
- **read_skill_doc(skill_id, doc_name)** — Read analysis guidance and interpretation rules

### External APIs
- Metrics database (query, aggregation, statistical analysis)
- Previous ticket results (search, retrieve, compare)

### Constraints
- ❌ **Cannot SSH** to hosts
- ❌ **Cannot execute** benchmarks or make changes
- ❌ **Cannot reserve** resources
- ⚠️ **Read-only**: All operations are queries and analysis

### Output
Structured **review verdict** with:
- Primary finding (hypothesis supported/refuted/inconclusive)
- Confidence level (high/medium/low)
- Statistical summary (mean, variance, outliers)
- Comparisons to baseline (if applicable)
- Recommendations (e.g., "repeat with X variation", "investigate Y factor")

---

## Retrospective Agent

**Role**: Post-mortem analysis, skill improvement feedback

### Read Tools
- **get_transcript_analysis(ticket_id)** — Read transcript, detect patterns:
  - Tool errors and retry sequences
  - Convergence failures
  - HITL escalations (pauses/user guidance)
  - Self-correction language (agent catching own mistakes)
  - Max-iteration hits (agent quit by safety limit)

### Write Tools
- **submit_retrospective(findings, classification)** — Submit classified findings for skill improvement

### External APIs
- Event log (read transcript of agent interactions)
- Ticket metadata (custom fields, final status)

### Constraints
- ❌ All other tools
- ⚠️ **Read-only except for retrospective submission**

### Output
Structured **retrospective** with:
- Classified findings (tool defect, skill gap, schema mismatch, prompt gap, convergence failure)
- Recommendations for improvement (new skill docs, prompt refinement, schema relaxation)

---

## Cross-Agent Shared Data

All agents can read from the shared **ticket** document:
- `summary`, `description` — User's request
- `custom_fields` — Arbitrary structured data
- `status` — Current workflow state
- Event log — Complete interaction history

Agents **cannot modify** custom fields directly. Instead, they write results back using scoped write tools (`submit_*` structured output), which update the ticket atomically.

---

## Guardrails and Safety

### Command Policy
The **command policy** (`agents/infra/command_policy.py`) restricts SSH commands globally and per-agent:

**Global Denied Patterns:**
- `rm -rf /` (recursively delete root)
- `mkfs` (format filesystem)
- `dd if=/dev/zero of=/dev/` (overwrite block device)
- `chmod 777 /` (open filesystem permissions)
- `shutdown`, `reboot` (stop system)
- `poweroff`, `halt` (stop system)

**Per-Agent Allowlists** (in `agents/infra/policies/*.json`):
- **Provisioning**: Can run `yum`, `apt`, `git`, `harness-cli` but not `root` shell
- **Benchmark**: Restricted to harness CLI and read commands
- **Teardown**: Only `harness-uninstall` and resource cleanup commands

### Budget Enforcement
- Per-ticket LLM token limit
- System-wide cost limit (USD)
- Budget warnings at 80%, enforced hard limit at 100%
- Protects against infinite loops and accidental overspend

### Convergence Safeguards
- Max iterations per investigation loop (default: 10)
- Consecutive-pass requirement (2 consecutive runs with same result = converged)
- Min-iteration requirement (at least 3 runs before converging)
- If max iterations hit, agent halts and escalates to user

---

## Least Privilege in Practice

### Example: Resource Agent Cannot SSH Install
The **Resource** agent validates host connectivity with `validate_host()`, which is SSH read-only:
- Can run `cat /etc/os-release` to check OS
- Can run `nproc` to count CPUs
- **Cannot** run `yum install` or modify anything

If a host needs provisioning, **Provisioning** agent takes over with its `ssh_execute()` tool.

### Example: Review Agent Cannot Query Live Metrics
The **Review** agent can only query `historical` results from completed benchmarks. It cannot:
- SSH to a host and run `top` to check live CPU
- Access a metrics dashboard
- Query in-flight performance data

This keeps Review focused on analysis, not troubleshooting.

### Example: Benchmark Agent Cannot Reserve Resources
The **Benchmark** agent gets a ready-provisioned host and run-file. It cannot:
- Call `reserve_resources()`
- Change resource allocation mid-test
- Decide to use different hardware

This prevents scope creep and ensures deterministic execution.

---

## Adding New Tools

When adding a tool to an agent:

1. **Add to MCP server** (`agents/{agent}/mcp_server.py`)
   - Define the tool's purpose, input schema, output format
2. **Implement the tool** in the same file or separate `tools.py` module
3. **Update this documentation** with a new section in the agent's tools list
4. **Add tests** verifying the tool works and respects constraints
5. **Review for privilege creep**: Does this agent really need this tool? Are there weaker alternatives?

---

## External System Guardrails

### QUADS Integration
- Agent can only reserve from available pool, cannot force allocation of unavailable hosts
- SSH keys are automatically generated and scoped to reservation
- Reservations auto-expire after deadline (default: 28 days)

### AWS Integration
- Agent can only launch pre-approved AMIs
- Instance types restricted by harness requirements (e.g., minimum vCPU count)
- Cost limits enforced at API call layer
- Instances auto-terminate after deadline (default: 24 hours)

### SSH Access
- All SSH commands logged (event log includes command, host, user, result)
- SSH keys stored securely (not in logs or tickets)
- SSH connections use key-based auth only (no passwords)
- Command allowlist prevents destructive operations

---

## Data Access Restrictions

### PII and Secrets
- Agents are told NOT to ask for passwords, API keys, or personal information
- If user submits credentials in ticket, agents should flag for redaction
- Secrets stored in `~/.agentic-perf/secrets/` are injected as env vars at runtime, not passed through logs

### Benchmark Results
- Results are stored in `~/.agentic-perf/tickets/{id}/results/`
- Results contain performance data (metrics, logs) but **not** raw system state (dmesg, memory dumps, etc.)
- Users should not store sensitive data in benchmark configs
