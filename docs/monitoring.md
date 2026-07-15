# Monitoring and Operational Runbook

This document provides operational guidance for monitoring agentic-perf's health, detecting issues early, and conducting periodic audits.

---

## Overview

Agentic-perf is a complex system with multiple agents, external integrations, and AI components. Effective monitoring requires tracking:

1. **System health** — Is the orchestrator running? Are services responsive?
2. **Agent performance** — Are agents completing tasks? How often do they fail?
3. **AI reliability** — Is the LLM producing correct output? Any drift or hallucinations?
4. **Resource usage** — Token spend, compute costs, convergence efficiency
5. **User satisfaction** — Feedback on results, perceived accuracy

---

## Key Metrics

### System Health

| Metric | Target | Alert Threshold | How to Check |
|---|---|---|---|
| **Orchestrator uptime** | 99.9% | < 99% | `systemctl is-active agentic-perf-orchestrator` |
| **State store API latency** | < 100ms p99 | > 500ms | Web UI dashboard or OTLP traces |
| **State store disk usage** | < 80% | > 90% | `df ~/.agentic-perf` |
| **Event log write latency** | < 50ms | > 200ms | OTLP metrics (append duration) |

### Agent Performance

| Metric | Good | Warning | Poor |
|---|---|---|---|
| **Avg completion time per agent** | < 10 min | 10-30 min | > 30 min |
| **Agent failure rate** | < 1% | 1-5% | > 5% |
| **HITL escalation rate** | < 10% | 10-30% | > 30% |
| **Re-run rate** (agent retries) | < 10% | 10-20% | > 20% |
| **Convergence rate** (investigation loops) | > 80% | 60-80% | < 60% |

### Token and Cost Tracking

| Metric | Target | Budget Alert | Over Budget |
|---|---|---|---|
| **Avg tokens per ticket** | < 50k | > 80% of limit | Exceeded |
| **Avg LLM cost per ticket** | < $0.10 | > 80% of limit | Exceeded |
| **Token spike detection** | < 100k/ticket | > 150k | > 200k (auto-pause) |
| **System-wide daily spend** | < $100 | > $80 | > $100 (auto-pause) |

### AI Quality Metrics

| Metric | Target | Track | How to Measure |
|---|---|---|---|
| **User feedback positive rate** | > 80% | Monthly | Thumbs-up vs thumbs-down |
| **Result reproducibility** | > 95% | Per harness | Rerun same config, compare variance |
| **Hallucination rate** | < 5% | Per agent | Manual review of failed tasks |
| **Citation accuracy** | > 95% | Spot checks | Verify sources match claims |

### Resource Utilization

| Metric | Good | Investigate |
|---|---|---|
| **Avg hosts reserved per ticket** | 2-4 | > 8 (scope creep?) |
| **Avg reservation duration** | < 4 hours | > 8 hours (agents slow?) |
| **QUADS utilization** | 60-80% | < 30% or > 95% |
| **AWS instance cost** | < $10/ticket | > $50 |

---

## Introspection Agent

The introspection agent provides automated, continuous monitoring of
individual tickets. When enabled, it runs alongside the pipeline agents
and detects anomalous patterns in real time without requiring manual
log review.

### What It Detects

| Anomaly | Severity | Description |
|---|---|---|
| Consecutive failures | medium/high | Same tool failing 2+ times in a row with similar errors, even when the agent changes input flags between retries |
| Repeated tool errors | medium/high | Same tool failing 3+ times total (non-consecutive) |
| Retry loops | medium/high | Same tool called with identical input 3+ times |
| Wasted iterations | medium/high | 25%+ of an agent's LLM calls produced only failed tool results |
| Max iterations | high | Agent exhausted its iteration budget |

The detection also classifies errors:
- **Infrastructure** (port conflict, disk full, permission denied) — retrying won't help
- **Transient** (timeout, rate limit, connection reset) — retrying may help
- **Logic** (wrong arguments, missing prerequisites) — agent needs a different approach

Importantly, failures are detected from tool result *content*, not just
the `is_error` flag. MCP tools that return `{"exit_code": 1, "error": "..."}`
or `{"success": false}` are recognized as failures even when the tool
handler itself didn't crash.

### Enabling

Globally in `~/.agentic-perf/config.json`:
```json
{
    "introspection": {
        "enabled": true
    }
}
```

Or per-ticket via `custom_fields.introspection_enabled: true` at
submission time.

### Customizing Detection

Detection parameters are loaded from skill files, not hardcoded.
Tune for your environment:

**Error patterns** (`skills/introspection/error-patterns.yaml`):
Regex patterns for classifying errors as infrastructure, transient,
or logic. Add org-specific patterns via private skills.

**Detection thresholds** (`skills/introspection/detection-thresholds.yaml`):
Consecutive failure count, similarity threshold, wasted iteration
percentage, retry loop count, etc. Override via private skills.

**Private overrides** (`~/.agentic-perf/private-skills/introspection.json`):
```json
{
    "error_patterns": {
        "infrastructure": ["our custom storage error"]
    },
    "thresholds": {
        "consecutive_failure_min": 3,
        "wasted_iterations_pct": 40
    }
}
```

Private error patterns are *appended* to the shipped defaults.
Private thresholds *replace* matching shipped defaults.

### Viewing Results

The web dashboard shows introspection observations in a dedicated card
below the LLM Usage section in the ticket detail view. The card updates
every poll cycle and shows:
- Anomaly count with severity breakdown
- Individual anomaly descriptions
- Status summary (events, LLM calls, tool errors)
- Collapsible narrative of recent activity

### Relationship to Other Monitoring

Introspection complements the other monitoring tools:
- **Retrospective agent** analyzes tickets *after* completion;
  introspection watches *during* execution
- **Budget guardrails** enforce cost limits; introspection detects
  *behavioral* waste (retry loops, repeated errors) that may not
  trigger budget alerts
- **Stale task watchdog** catches agents with no events;
  introspection catches agents that are *active but unproductive*

---

## Monitoring Dashboard Setup

### Real-Time Web UI Metrics
The web dashboard at `http://localhost:8090` shows:
- **Live ticket list** with status, token usage, age
- **System health** (orchestrator uptime, LLM API status)
- **Cost summary** (total spend, remaining budget)
- **Agent activity** (current active agents, recent completions)
- **Introspection** (anomaly detection, when enabled)

View on a persistent monitor in your NOC/war room:
```bash
# Use a browser on a display, or pipe via ssh:
ssh monitor-host "watch -n 10 'curl -s http://localhost:8090/api/v1/health | jq .'"
```

### OpenTelemetry Export (Jaeger/Grafana)
If configured, metrics are exported to your observability stack:

```json
{
  "telemetry": {
    "otlp_exporter": {
      "endpoint": "http://jaeger-collector:4317",
      "headers": {"Authorization": "Bearer token"}
    }
  }
}
```

**Key traces to monitor:**
- `agentic_perf.agent.duration` — Agent execution time by agent type
- `agentic_perf.llm.tokens` — Token usage per ticket
- `agentic_perf.tool.duration` — Tool call latency by tool name
- `agentic_perf.orchestrator.poll_duration` — Polling cycle duration

**Queries in Jaeger:**
```
# Find slow agents
SELECT * FROM agentic_perf.agent.duration WHERE duration > 30m

# Find costly tickets
SELECT * FROM agentic_perf.llm.tokens WHERE total_tokens > 100k

# Tool failure rates
SELECT * FROM agentic_perf.tool.* WHERE error_count > 0
```

### Local Event Log Analysis
For real-time investigation without external tools:

```bash
# Stream event log as tickets complete
tail -f ~/.agentic-perf/logs/ticket-*.jsonl | \
  jq 'select(.event_type == "agent_finished") | {ticket_id, agent, duration_sec: .data.duration}'

# Count errors per agent
jq 'select(.event_type == "agent_error")' ~/.agentic-perf/logs/*.jsonl | \
  jq -s 'group_by(.agent) | map({agent: .[0].agent, error_count: length})'

# Find slow tool calls
jq 'select(.event_type == "tool_result" and .data.duration > 30) | {ticket_id, tool: .data.tool, duration_sec: .data.duration}' ~/.agentic-perf/logs/*.jsonl
```

---

## Alerting Rules

### Critical (Page on-call)
1. **Orchestrator down** — Confirm with `systemctl is-active agentic-perf-orchestrator`
   - Action: SSH to host, restart orchestrator, check logs for errors
   
2. **State store unresponsive** — `curl http://localhost:8090/health` fails
   - Action: Check disk space (`df ~/.agentic-perf`), restart state store
   
3. **LLM API error rate > 50%** — Check OTLP traces or event log
   - Action: Verify Anthropic API status, check API key, review error details
   
4. **Budget exceeded** — Event log shows `agent_paused: budget_limit`
   - Action: Investigate which tickets overspent; pause new submissions or increase budget
   
5. **Data corruption** — Event log or ticket file becomes unreadable
   - Action: Restore from backup; investigate disk health

### Warning (Log and review next business day)
1. **Agent failure rate > 5%** — More failures than normal
   - Action: Review failed agent transcripts; identify pattern (specific harness? provider?)
   
2. **HITL escalation rate > 30%** — Agents pausing too often
   - Action: Review agent prompts; may need refinement or updated skill docs
   
3. **Mean agent completion time > 30 min** — Agents are slow
   - Action: Check external system latency (SSH, QUADS API, AWS); review convergence loops
   
4. **Disk usage > 80%** — Running out of space
   - Action: Compress old closed tickets, archive to external storage, or expand filesystem
   
5. **User feedback negative rate > 20%** — More thumbs-down than normal
   - Action: Review negative feedback comments; identify common complaint patterns

### Informational (Log for trend analysis)
1. **New harness added** — Track first-use issues
2. **Agent prompt updated** — Monitor for regression vs improvement
3. **New resource provider integrated** — Track success rate
4. **Skill docs updated** — Check if agent behavior improves

---

## Daily Checklist (Ops Team)

- [ ] Orchestrator is running: `systemctl status agentic-perf-orchestrator`
- [ ] State store is responding: `curl http://localhost:8090/health`
- [ ] No critical alerts in past 24 hours
- [ ] Event logs have new entries: `ls -lt ~/.agentic-perf/logs/ | head -5`
- [ ] Disk usage normal: `df ~/.agentic-perf`
- [ ] No error patterns in recent failures

**Action**: If any checks fail, investigate and document in log.

---

## Weekly Review (Engineering Team)

### Metrics Review
```bash
# Week-to-date stats
echo "=== TICKETS PROCESSED ==="
find ~/.agentic-perf/logs -name "ticket-*.jsonl" -mtime -7 | wc -l

echo "=== AGENT SUCCESS RATE ==="
jq 'select(.event_type == "agent_finished") | .data.status' ~/.agentic-perf/logs/*.jsonl | \
  sort | uniq -c

echo "=== AVG TOKENS PER TICKET ==="
jq 'select(.event_type == "llm_usage") | .data.output_tokens' ~/.agentic-perf/logs/*.jsonl | \
  jq -s 'add / length'

echo "=== HITL ESCALATIONS ==="
jq 'select(.event_type == "transition" and .data.to_status == "awaiting_customer_guidance") | .ticket_id' \
  ~/.agentic-perf/logs/*.jsonl | sort | uniq | wc -l
```

### Issue Triage
- Review failed tickets: What went wrong? Pattern or one-off?
- Review slow tickets: Why did they take > 30 min?
- Review negative feedback: What are users complaining about?
- Review new errors in event log: Unseen error patterns?

### Action Items
- Update skill docs if agents misunderstand capability
- Refine agent prompts if they make recurring mistakes
- Tighten command policy if agents attempt unauthorized operations
- Increase budget if users are hitting limits on legitimate work

---

## Monthly Audit (Management/Compliance)

### AI Safety Audit
1. **Drift Detection** — Are agents behaving differently than last month?
   - Compare error rates, feedback scores, convergence rates
   - Any suspicious patterns (e.g., increased hallucinations)?
   
2. **User Feedback Analysis** — Aggregate and categorize feedback
   - What are the top complaints?
   - What are the top praise items?
   - Any consistency issues (e.g., specific harness always fails)?
   
3. **Permissions Audit** — Review who can do what
   - List all users and their roles
   - Verify no privilege creep (agents accessing unexpected tools)
   - Verify no orphaned admin accounts
   
4. **Accuracy Spot-Check** — Randomly sample 5 completed tickets
   - Did the benchmark run as described?
   - Did the analysis match the data?
   - Would you trust the recommendation?
   
5. **Security Review** — Scan for credential leaks
   - Grep event logs for SSH key patterns: `grep -E "BEGIN.*KEY|-----" ~/.agentic-perf/logs/*.jsonl`
   - Grep for common secret patterns: `grep -E "password|api.?key|token" ~/.agentic-perf/logs/*.jsonl`
   - Action: If found, rotate credentials immediately and redact logs

### Cost Analysis
```bash
# Total LLM spend
jq '.data.cost_usd' ~/.agentic-perf/logs/*.jsonl 2>/dev/null | \
  jq -s 'add | "Total spend: $\(.)"'

# Cost per ticket type
jq 'select(.event_type == "submit_triage") | .ticket_id as $id | 
    {ticket: $id, benchmark: .data.benchmark}' ~/.agentic-perf/logs/*.jsonl | \
  jq -s 'group_by(.benchmark) | 
         map({benchmark: .[0].benchmark, count: length, tickets: map(.ticket)})'
```

### Resource Provider Health
- **QUADS**: Success rate? Avg wait time? Any API errors?
- **AWS**: Cost overruns? Instance launch failures? Termination issues?
- **PSAP**: Cluster availability? Kubeconfig delivery latency?

### SLA Tracking
- Avg time to triage: ___ minutes
- Avg time to provision: ___ minutes
- Avg time to run benchmark: ___ minutes
- Avg time to review: ___ minutes
- **Total end-to-end:** ___ minutes

Compare to SLA targets. If trending up, investigate why.

---

## Quarterly Deep Dive (All Teams)

### 1. Model Performance Review
- Pull transcripts from 20 random tickets
- Grade agent reasoning: correct decisions? good resource choices?
- Grade analysis: accurate conclusions? good recommendations?
- Estimate **hallucination rate** and **accuracy rate**
- Identify top failure modes (e.g., "triage agent always picks wrong benchmark for X")

### 2. System Health Review
- Any infrastructure issues? (disk, network, API latency)
- Any reliability regressions?
- Any scalability concerns?
- Plan for next quarter (more capacity? new features?)

### 3. Skills and Prompts Review
- Review agent prompts for drift or stale content
- Review skill docs for gaps or errors
- Identify most requested features (from user feedback)
- Plan prompt/skill updates for Q+1

### 4. Roadmap Alignment
- Are we tracking toward goals?
- Any blocker issues?
- Any new harnesses or providers to add?
- Any compliance requirements to address?

---

## Troubleshooting Guide

### Symptom: Orchestrator consuming high CPU
**Possible causes:**
- LLM polling loop running too fast
- Database query inefficiency
- Infinite tool-call loop

**Investigation:**
```bash
# Check orchestrator log
tail -100 ~/.agentic-perf/logs/orchestrator.log | grep -E "ERROR|WARNING"

# Check for runaway agents
ps aux | grep agentic-perf

# Check poll interval config
grep poll_interval ~/.agentic-perf/config.json
```

**Fix:**
- Increase `poll_interval` in config (default: 3 seconds)
- Restart orchestrator: `systemctl restart agentic-perf-orchestrator`

### Symptom: Tickets stuck at `awaiting_customer_guidance`
**Possible causes:**
- Agent asked a question; user forgot to reply
- Agent error; escalated to human

**Investigation:**
```bash
agentic-perf show <ticket_id>  # Check custom_fields for agent message
```

**Fix:**
```bash
agentic-perf reply <ticket_id> "your clarification"
```

### Symptom: LLM API errors spiking
**Possible causes:**
- Rate limit hit
- API credentials expired
- Anthropic API outage

**Investigation:**
```bash
# Check recent LLM errors
jq 'select(.event_type == "agent_error" and .data.error_type == "llm") | .data' \
  ~/.agentic-perf/logs/*.jsonl | tail -10
```

**Fix:**
- Verify API key in `~/.agentic-perf/secrets/`
- Check Anthropic API status page
- If rate limited, stagger submissions or add delay between agents

### Symptom: Results look wrong or incomplete
**Possible causes:**
- Benchmark configuration was incorrect
- Data collection interrupted
- Harness schema changed

**Investigation:**
```bash
# Check the run configuration
agentic-perf show <ticket_id> | grep -A 100 "runfile"

# Check benchmark logs on test system
ssh user@host "tail -100 /opt/crucible/.../run.log"
```

**Fix:**
- Review configuration; resubmit with corrected parameters
- Check test system state; may need to restart harness
- Verify harness version matches expected schema

---

## Performance Optimization

### If agents are slow:
1. Increase parallelism (currently 1 agent per ticket in sequence)
2. Cache benchmark schema locally (avoid repeated downloads)
3. Pre-warm LLM context (cache system prompts in OTLP)

### If token usage is high:
1. Reduce prompt verbosity (remove examples)
2. Cache skill docs in agent context
3. Use smaller models for triage/review phases

### If QUADS reservation is slow:
1. Use AWS for faster turnaround
2. Request dedicated capacity reservation
3. Pre-allocate hosts in off-hours

### If benchmarks timeout:
1. Increase timeout thresholds in orchestrator config
2. Use QUADS hosts (more stable) vs AWS (more variable)
3. Reduce benchmark duration (faster feedback loop)

---

## Runbook: Adding a New Harness

When adding a new benchmark harness:

1. **Deploy harness** to test system
2. **Create skill provider** (`providers/skills/{harness}/`) with:
   - Harness schema (run-file format)
   - Platform contract (pre/post conditions)
   - Configuration guide
3. **Test with agents**: Submit 5 test tickets, monitor for issues
4. **Monitor closely first 2 weeks**: High alert threshold
5. **After 2 weeks**: Promote to production, normal monitoring

---

## Runbook: Emergency Incident

### Agent Stuck in Infinite Loop
1. **Immediate**: Hard-stop the ticket: `agentic-perf emergency-stop --hard`
2. **Pause new submissions**: Temporarily disable new ticket ingestion
3. **Investigate**: Review event log, identify root cause
4. **Fix**: Update agent prompt or skill docs to prevent recurrence
5. **Resume**: Re-enable submissions, resubmit failed ticket

### LLM API Outage
1. **Pause new submissions**: No new work while API is down
2. **Wait for recovery**: Check Anthropic status page
3. **Resume**: When API is healthy, resume normal operations

### Data Corruption
1. **Isolate**: Stop orchestrator immediately
2. **Backup**: `cp -r ~/.agentic-perf ~/.agentic-perf.backup-$(date +%s)`
3. **Restore**: From known-good backup or git repository
4. **Verify**: Checksums match, event log is intact
5. **Resume**: Start orchestrator, monitor closely

### Security Incident (Credential Leak)
1. **Alert**: Notify security team immediately
2. **Revoke**: Rotate leaked credentials (SSH keys, API tokens)
3. **Audit**: Grep logs for evidence of misuse
4. **Redact**: Remove or encrypt sensitive logs
5. **Update**: Push new credentials to all agents
6. **Monitor**: Watch for suspicious activity

---

## Contacts and Escalation

- **On-call Ops**: [On-call page link]
- **Engineering Lead**: [Name/email]
- **Security Team**: [Email]
- **Anthropic Support**: [API support contact]
- **Resource Providers**: QUADS admins, AWS TAM, PSAP contacts
