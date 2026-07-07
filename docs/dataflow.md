# Data Flow Diagram and Sensitive Data Handling

This document describes how data moves through agentic-perf and highlights where sensitive information is handled.

---

## System Data Flow

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  USER (CLI or Web Browser)                                                   │
│  - Natural-language test request                                             │
│  - SSH credentials (optional)                                                │
│  - Benchmark parameters (e.g., thread count, duration)                      │
└─────────────────────┬────────────────────────────────────────────────────────┘
                      │ HTTPS or HTTP (depending on deployment)
                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  STATE STORE (FastAPI)                                                       │
│  - REST API for ticket CRUD                                                  │
│  - In-memory ticket store (persists to disk)                                │
│  - Event log (append-only audit trail)                                       │
│  - Web dashboard (browser-based UI)                                          │
│                                                                               │
│  Ticket structure:                                                           │
│  ├─ summary, description                                                    │
│  ├─ status (state machine)                                                  │
│  ├─ custom_fields (triage result, resources, provisioning status, etc.)    │
│  ├─ event_log (sequence of all interactions)                               │
│  └─ feedback (user ratings and comments)                                    │
└────┬────────────────────────────────┬────────────────────────────────────────┘
     │ HTTP polling                   │ Browser WebSocket
     │ (Orchestrator)                 │ (Live updates)
     │                                │
     ▼                                ▼
┌──────────────────────┐         ┌──────────────────────┐
│  ORCHESTRATOR        │         │  WEB UI              │
│  - Dispatch loop     │         │  - Ticket list       │
│  - Agent lifecycle   │         │  - Status updates    │
│  - Resource cleanup  │         │  - Feedback buttons  │
└──────────┬───────────┘         └──────────────────────┘
           │
    ┌──────┴──────┬──────────┬──────────┬──────────┬──────────┐
    │             │          │          │          │          │
    ▼             ▼          ▼          ▼          ▼          ▼
┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
│  Triage  │ │Resource  │ │Provision │ │Benchmark│ │  Review  │
│  Agent   │ │  Agent   │ │  Agent   │ │  Agent   │ │  Agent   │
└────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘
     │            │            │            │            │
     │ Skill docs │ Benchmark  │ Harness    │ Harness    │ Metrics
     │ (read-only)│ discovery  │ install    │ execution  │ queries
     │            │            │            │            │
     └────┬───────┘            │            │            │
          │                    │            │            │
          └─────────┬──────────┴────────────┴────────────┴─────┐
                    │                                          │
                    ▼                                          ▼
            ┌──────────────────────┐            ┌──────────────────────┐
            │  EXTERNAL SYSTEMS    │            │  RESULT DATA         │
            │                      │            │                      │
            ├─ QUADS API           │            ├─ Benchmark metrics   │
            │  (bare-metal)        │            │ (throughput, latency)│
            ├─ AWS EC2 API         │            ├─ System info (CPU,   │
            │  (cloud instances)   │            │   memory, network)   │
            ├─ PSAP CC API         │            ├─ Logs from test      │
            │  (GPU clusters)      │            │                      │
            │                      │            ├─ Run configuration   │
            └──────────────────────┘            │  (for audit)         │
                    │                           │                      │
                    ▼                           └──────────────────────┘
            ┌──────────────────────┐                   │
            │  TEST SYSTEMS        │                   │
            │  (provisioned hosts) │                   │
            │                      │                   │
            ├─ SSH to hosts        │                   │
            ├─ Install harness     │◄──────────────────┘
            ├─ Configure params    │
            ├─ Run benchmark       │
            └──────────────────────┘
```

---

## Data Categories and Sensitivity

### 🟢 Non-Sensitive (Can Log Freely)
- Benchmark names and types (fio, uperf, STREAM, etc.)
- Test parameters (thread count, duration, block size)
- Performance results (throughput, latency, CPU usage)
- Agent decisions and reasoning
- User ticket summaries and descriptions

### 🟡 Moderately Sensitive (Log with Caution)
- **Hostname/IP addresses**: May reveal internal infrastructure topology
  - Logged in event trail but only when necessary
  - User should not include PII in hostnames
- **SSH user names**: May indicate role or department
  - Logged in event trail (needed for debugging)
  - Not exposed in web UI by default

### 🔴 Highly Sensitive (Never Log)
- **SSH private keys**: Never stored in logs or tickets
  - Injected at runtime as environment variables
  - Stored in `~/.agentic-perf/secrets/` (user responsibility)
  - Agent code never prints or logs keys
- **API tokens**: QUADS, AWS, PSAP credentials never logged
  - Stored in `~/.agentic-perf/secrets/`
  - Injected at runtime
- **Customer data**: If benchmark processes real customer data
  - Users must sanitize before submission
  - Results should not include PII
- **Passwords**: Should never be submitted in tickets
  - If user includes passwords, agent should flag for redaction

---

## Data Handling by Component

### CLI (User Interface)
- **Input**: User's natural-language request, SSH credentials (optional)
- **Storage**: In-memory session only (not persisted)
- **Output**: Ticket ID, status updates, results
- **Sensitive data handling**: 
  - User should not type passwords (use `~/.agentic-perf/secrets/` instead)
  - CLI does not cache credentials

### State Store (API & Persistence)
- **Storage**: `~/.agentic-perf/tickets/` (JSON per ticket)
- **Persistence**: On-disk, encrypted filesystem recommended for production
- **Sensitive data in tickets**:
  - SSH user/key path stored (needed for agents to SSH)
  - SSH private keys NOT stored (external reference only)
  - API tokens NOT stored
- **Event log** (`~/.agentic-perf/logs/{ticket_id}.jsonl`):
  - Append-only audit trail
  - Compressed after 7 days (default)
  - Contains all agent interactions, tool calls, decisions
  - Does NOT contain SSH private keys or API tokens
  - Compressed format: `{ticket_id}.jsonl.gz`

### Agents (Processing)
- **Memory**: Agent runs in container or sandbox (Podman recommended)
- **Secrets**: Injected at runtime as environment variables
  - `AGENTIC_PERF_SSH_KEY` — SSH private key content
  - `AGENTIC_PERF_QUADS_KEY` — QUADS API token
  - `AGENTIC_PERF_AWS_*` — AWS credentials
- **Logging**: Agents log to event stream, which captures tool calls but not secrets
- **Cleanup**: Containers/processes terminated after agent completes; temp files cleaned up

### External Systems (QUADS, AWS, PSAP)
- **SSH to test hosts**: Keys injected as env vars, never logged
- **API calls**: Authentication tokens passed in headers, not in URL parameters
- **Result retrieval**: Results stored on test system or S3; agents query via API

### Web Dashboard
- **Data display**: Shows public ticket info (summary, status, results)
- **Sensitive filtering**: 
  - SSH user names shown but not full key paths
  - API tokens never displayed
  - Host IPs shown (needed for troubleshooting)
- **User authentication**: Credentials stored in browser localStorage (HTTPS only)

---

## Data Flow: A Complete Test Execution

### 1. User Submits Request (Unencrypted)
```bash
agentic-perf submit "Test NIC performance" \
  -d "SSH key: ~/.ssh/id_ed25519. Hosts: 10.1.2.1, 10.1.2.2"
```
- CLI reads request from command line
- Ticket created in state store
- SSH key path stored (file path, not content)

### 2. Triage Agent Processes (LLM Call)
- **Input to LLM**: User request, list of available benchmarks
- **LLM output**: Selected benchmark, parameters
- **State store update**: Triage result saved to `custom_fields`
- **Event log**: `agent_started`, `llm_request`, `llm_response`, `tool_called`, `submit_triage`
- **Sensitive data**: None passed to LLM

### 3. Resource Agent Validates Hosts (SSH)
- **Secret injection**: SSH key loaded from `~/.agentic-perf/secrets/`
- **SSH call**: Connect to each host, read `/etc/os-release`, `nproc`, etc.
- **Logging**: Event log records `tool_called: validate_host` with host/user but not key path
- **Sensitive data**: SSH key is in-memory only, never logged

### 4. Provisioning Agent Installs Harness (SSH)
- **Secret injection**: SSH key injected as env var
- **Command execution**: Run `yum install`, `git clone`, harness setup
- **Logging**: Event log records each SSH command (redacted for security)
- **Sensitive data**: SSH key not in logs; command output sanitized

### 5. Benchmark Agent Executes Test (Harness CLI)
- **Input**: Run configuration (JSON)
- **Execution**: Harness runs benchmark on test system
- **Result collection**: Metrics stored on test system or returned to orchestrator
- **Logging**: Event log records benchmark status, intermediate results
- **Sensitive data**: Test results may include customer data; users responsible for sanitizing

### 6. Review Agent Analyzes Results (LLM Call)
- **Input to LLM**: Metrics, run configuration, previous results (for comparison)
- **LLM output**: Verdict, confidence, recommendations
- **State store update**: Review result saved
- **Event log**: `submit_review` event
- **Sensitive data**: Results may contain customer data; LLM processes it

### 7. Teardown Agent Cleans Up (SSH)
- **Execution**: Uninstall harness, release resources
- **Resource cleanup**: Call QUADS/AWS API to terminate instances
- **Logging**: Event log records cleanup completion
- **Sensitive data**: None new created during cleanup

### 8. User Views Results (Web UI or CLI)
- **Data accessed**: Ticket custom fields, event log (decompressed if old)
- **Sensitive filtering**: 
  - Web UI may redact SSH key paths
  - CLI shows full event log (raw audit trail)
- **Export**: User can download ticket data (full details)

---

## Security Best Practices

### For Users
1. **Don't put secrets in ticket descriptions**
   - Use `~/.agentic-perf/secrets/` instead
   - If you mention a host IP, don't also give the password
   
2. **Sanitize customer data**
   - Don't benchmark with real customer data
   - If you must, ensure results are treated as sensitive
   - After testing, clear the data from test systems
   
3. **Use SSH keys, not passwords**
   - Agents only support key-based SSH auth
   - Passwords cannot be submitted via tickets

### For Operators
1. **Encrypt at rest**
   - `~/.agentic-perf/` should be on encrypted filesystem
   - Recommend LUKS or encrypted home directory
   
2. **Control who can view tickets**
   - Use RBAC (in development) to limit access
   - Admin-only tickets for sensitive tests
   
3. **Rotate secrets regularly**
   - SSH keys: Rotate yearly or after team changes
   - API tokens (QUADS, AWS): Rotate per provider policy
   
4. **Monitor for data leaks**
   - Review event logs for accidental credential exposure
   - Alert on SSH key patterns in logs
   
5. **Archive and purge carefully**
   - Compress old tickets (gzip encryption optional in production)
   - Manual purge only for truly sensitive tickets
   - Keep audit trail for compliance (7+ year retention typical)

### For Developers
1. **Never log secrets**
   - Use `redacted` placeholder in logs
   - Secrets should only be in environment variables
   
2. **Filter event logs**
   - Before including tool output in logs, sanitize
   - Redact SSH key paths, API tokens, etc.
   
3. **Validate all input**
   - User-provided hostnames could hide injection attacks
   - Validate before using in shell commands
   
4. **Isolate agent processes**
   - Agents should run in containers (Podman) or VMs
   - Limit file system access to necessary directories only

---

## Audit and Auditability

### Event Log as Audit Trail
- All agent interactions captured in append-only event log
- Timestamp, agent name, event type, data on every record
- Compressed after ticket closes (gzip, not encrypted)
- Can be exported for external audit or compliance review

### What's Auditable
- ✅ Which benchmark was run and why
- ✅ Which hosts were used
- ✅ What parameters were tested
- ✅ What results were produced
- ✅ User feedback on results
- ✅ All LLM requests and responses (full prompt, full response)
- ✅ All SSH commands executed
- ✅ All tool calls and their results

### What's Not Auditable (By Design)
- ❌ SSH private key content
- ❌ API token values
- ❌ Passwords (which should never be submitted anyway)
- ❌ Customer data in benchmarks (user responsibility to sanitize)

### Auditability Features
- **Manual purge API**: `DELETE /api/v1/tickets/{id}` available for right-to-be-forgotten requests
- **Immutable audit trail**: Event log is append-only and cannot be edited after creation
- **Compressed archival**: Old logs are compressed (gzip) for long-term storage
- **Export capability**: Event logs can be exported for external audit systems

---

## Data Retention and Cleanup

### Default Retention Policy
- **Active tickets**: Stored indefinitely (until closed)
- **Closed tickets**: Event log compressed after 7 days
- **Compressed logs**: Stored indefinitely (can be purged manually if needed)

### Customization
Configure in `~/.agentic-perf/config.json`:
```json
{
  "compress_closed_after_days": 7,
  "manual_purge_enabled": true
}
```

### Manual Purge
```bash
agentic-perf purge ticket-id  # Requires admin role
```

Will delete:
- Ticket record
- Event log
- Result data
- All associated artifacts

### Archival (Production)
For long-term compliance storage:
1. Export event logs to external system (S3, Azure Blob, etc.)
2. Enable OTLP export in config (sends to Jaeger, Grafana Loki, Sumo Logic, etc.)
3. Compress and backup `~/.agentic-perf/` directory

---

## Integration with External Systems

### OpenTelemetry Integration (OTLP)
For deployments that need integration with observability platforms:

Configure in `~/.agentic-perf/config.json`:
```json
{
  "telemetry": {
    "otlp_exporter": {
      "endpoint": "http://localhost:4317",
      "headers": {"Authorization": "Bearer ..."}
    }
  }
}
```

**What's exported:**
- LLM call telemetry (model, tokens, duration)
- Tool call spans (tool name, duration, errors)
- Agent lifecycle events (started, completed, errored)

**What's NOT exported to OTLP:**
- Full event log (too large; use event log export instead)
- Secrets (sanitized in spans)
- Complete LLM prompts (send summary only)

### Event Log Export (For Archival or External Audit)
```bash
agentic-perf export-logs --format jsonl --output audit-backup.jsonl.gz
```

Exports complete event log for archival, external audit systems, or compliance review.

### Integration Points
- **Metrics**: Export token usage, cost, tool latencies to time-series databases (Prometheus, InfluxDB)
- **Logs**: Stream event logs to centralized logging (Splunk, ELK, Sumo Logic)
- **Traces**: Export spans to tracing backends (Jaeger, Tempo, DataDog)
- **Ticketing**: Webhook integration to create tickets in external systems on errors
