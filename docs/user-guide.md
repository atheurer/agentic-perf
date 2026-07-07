# Agentic-Perf User Guide

This guide is for end users and operational teams who submit performance testing requests and review AI-generated recommendations. For technical implementation details, see [Architecture](architecture.md).

---

## What Is Agentic-Perf?

Agentic-Perf is a multi-agent system that automates ad-hoc performance testing. You submit a natural-language request (e.g., "Compare network performance between two kernel versions"), and specialized AI agents:

1. **Understand** your request and form a hypothesis
2. **Reserve** hardware from available providers (QUADS, AWS, PSAP clusters, or user-provided hosts)
3. **Install** the benchmark harness on target systems
4. **Execute** the benchmark and monitor progress
5. **Analyze** results and deliver a verdict

All decisions are logged and auditable. Humans stay in the loop—you can pause, redirect, or stop an agent at any point.

---

## Key Features

### ✅ Autonomous Execution
- Submit a single natural-language request
- Agents handle all details: resource discovery, provisioning, schema validation, execution
- Multi-step workflows (investigation loops) with automatic convergence detection

### ✅ Human Oversight
- Review and approve critical actions (benchmark runs, configuration changes)
- Pause agents and reply to redirect execution
- Stop agents gracefully (finish current action, then wait for input) or hard (immediate kill)
- Monitor progress in real-time via CLI or web dashboard

### ✅ Trustworthy AI
- All agent reasoning is logged and auditable (event-based transcript)
- AI-generated outputs are explicitly tagged
- Decisions are traced back to source evidence and data
- Disclaimers remind you that AI-generated content may contain errors

### ✅ Flexible Resource Providers
- **User-provided hosts**: Direct SSH access to machines you control
- **QUADS**: Automated bare-metal reservation from a self-service lab
- **AWS EC2**: On-demand cloud instances
- **PSAP Control Center**: GPU cluster reservations for AI/ML workloads

### ✅ 10+ Benchmark Harnesses
Crucible, Zathras, Kube-Burner, k8s-netperf, Benchmark-Runner, Clusterbuster, Vstorm, Ioscale, Forge, Arcaflow plugins

---

## Getting Started

### 1. Submit a Request

```bash
agentic-perf submit "Run a 4K random read fio test"
```

Or with more details:

```bash
agentic-perf submit \
  "Compare memory bandwidth: default vs DDIO disabled" \
  -d "Use QUADS to get a bare-metal host. Run STREAM benchmark with zathras. Test both configurations."
```

The system creates a ticket and starts processing. Your ticket ID is printed:

```
Created ticket: ticket-abc123
Status: triage_pending
Summary: Compare memory bandwidth...
```

### 2. Monitor Progress

**Via CLI:**
```bash
agentic-perf show ticket-abc123
```

**Via Web Dashboard:**
```
http://localhost:8090
```

The dashboard shows live updates: agent activity, tool calls, token usage, and costs.

### 3. Respond to Pauses

If an agent pauses and asks for guidance, reply:

```bash
agentic-perf reply ticket-abc123 "Use the XXV710 NIC, not the ConnectX."
```

The agent resumes with your feedback.

### 4. Review Results

Once the ticket is `closed`, view the results:

```bash
agentic-perf show ticket-abc123
```

Results include the agent's verdict, metric comparisons, and recommendations—all with audit trail and source citations.

---

## What Agents Do (and Don't Do)

### Triage Agent
✅ **Does:**
- Parse your natural-language request
- Identify the best benchmark for your use case
- Ask clarifying questions if ambiguous

❌ **Doesn't:**
- Execute benchmarks (that's the Benchmark agent's job)
- Reserve hardware (Resource agent does that)

### Resource Agent
✅ **Does:**
- Find and validate available hosts
- Reserve hardware from QUADS, AWS, PSAP, or user-provided machines
- Check SSH connectivity and system compatibility

❌ **Doesn't:**
- Install software (Provisioning agent does that)
- Make final decisions about host suitability without user confirmation

### Provisioning Agent
✅ **Does:**
- Install benchmark harnesses on reserved hosts
- Configure platform-specific settings (kernel parameters, CPU partitioning, etc.)
- Validate the installation

❌ **Doesn't:**
- Modify production systems outside the test environment
- Install arbitrary software (only harness-approved packages)

### Benchmark Agent
✅ **Does:**
- Generate run configurations from your requirements
- Execute the benchmark
- Monitor progress and retry on transient failures

❌ **Doesn't:**
- Interpret results (Review agent does that)
- Modify kernel parameters mid-test (that's Provisioning's job)

### Review Agent
✅ **Does:**
- Analyze benchmark results
- Compare results to baselines or alternate configurations
- Produce a verdict with confidence levels

❌ **Doesn't:**
- Decide resource allocation (that's for you and Resource agent)
- Change benchmark parameters based on live results (use investigation loops for iterative refinement)

---

## When to Intervene

### 🟡 Graceful Stop (Yellow "Pause" Button)
Use this when an agent is **going off-course but not broken**:
- Agent picked the wrong benchmark → pause, reply with the correct one
- Configuration looks wrong → pause, provide corrected values
- Agent is stuck asking the same question → pause, give clear direction

**What happens:**
- Agent finishes its current action
- Ticket transitions to `awaiting_customer_guidance`
- You reply with direction; agent resumes

### 🔴 Hard Stop (Red "Stop" Button)
Use this only when an agent is **truly stuck or broken**:
- Infinite loop calling the same failing tool
- Burning budget on a broken approach
- Critical error that can't be recovered

**What happens:**
- Agent process is killed immediately
- Ticket marked with `interrupted: true`
- Test systems may be left in inconsistent state (e.g., packages half-installed)
- Teardown agent warns you to check for stale state
- You can resume by replying, but manual cleanup may be needed

### 🚨 Emergency Stop (Red Panic Button in Header)
Use this to **stop all active tickets at once** (e.g., infrastructure emergency):
- All active agents are paused
- All tickets transition to `awaiting_customer_guidance`
- Manual inspection recommended

---

## Understanding AI-Generated Output

### ⚠️ AI Disclaimer
All agent output is AI-generated and may contain errors. **Always verify recommendations before acting.**

Examples of AI mistakes:
- Misinterpreting a parameter (e.g., treating MHz as microseconds)
- Hallucinating a tool or API that doesn't exist
- Over-generalizing from examples in its training data
- Missing edge cases specific to your environment

### 🏷️ AI-Generated Tags
The system marks AI-generated content with an **AI-generated** badge. Human inputs (your replies, approvals) are marked differently.

### 📚 Source Citations
When an agent makes a recommendation, look for source citations. For example:

> **Finding**: XXV710 NICs peak at ~42 Mpps  
> **Sources**: 
> - Skill doc: `skills/trafficgen/nic-limits.md`
> - Metric query: Previous run `ticket-xyz789` (measured 41.8 Mpps at same config)

If the source is missing or weak, take the finding with skepticism.

---

## Submitting Good Requests

### ✅ Good Request: Clear and Specific
```
"Compare kernel routing performance on RHEL 8 vs RHEL 10 using trafficgen. 
Use physical NIC (not SR-IOV), single server forwarding traffic from client to target. 
Target: measure throughput and latency at line rate."
```

### ❌ Vague Request: Open-Ended
```
"Run some networking benchmarks"
```

### ✅ Good Request: Provides Context
```
"Evaluate whether disabling fq_codel improves kernel forwarding. 
Previous test (ticket-old123) used pfifo_fast. 
Use same hardware/config, just swap qdisc."
```

### ❌ Unhelpful Request: Contradictory
```
"Test on both QUADS and AWS. Single host or cluster, doesn't matter."
```

---

## Troubleshooting

### Agent Paused Without Asking
**Symptom**: Ticket stuck at `awaiting_customer_guidance` with no clear message.

**Fix**: View the ticket details to see what the agent was trying to do, then reply with clarification:

```bash
agentic-perf show ticket-abc123
agentic-perf reply ticket-abc123 "I have SSH access to 10.1.2.1. Try that instead."
```

### Benchmark Ran But Results Seem Wrong
**Symptom**: Throughput numbers don't match expectations, or variance is unusually high.

**Fix**: 
1. Review the run configuration (was it what you intended?)
2. Check if the test systems were in the right state (CPU frequency, IRQ affinity, etc.)
3. Inspect the raw metric data (not just agent summary)
4. Reply asking the agent to re-run with specific parameter changes

### Agent Exceeded Budget
**Symptom**: Ticket stopped with "budget exhausted" message.

**Fix**: The system has per-ticket and system-wide LLM token limits. This is a safety measure. Either:
- Wait for a reset (if a daily/hourly limit)
- Simplify your request (fewer investigation iterations)
- Contact the admin to increase budget for this ticket

---

## Best Practices

### 📋 Provide Baseline Data
If you have previous test results, include them in your request:

```
agentic-perf submit \
  "Reproduce the 20% throughput improvement from commit abc123" \
  -d "Baseline (main): 85 Gbps (from ticket-xyz). Target: commit abc123. Hardware: same as ticket-xyz."
```

### 🎯 One Hypothesis Per Ticket
Don't ask: "Try A, B, C, and D and tell me which is best."  
Instead: Submit separate tickets for each hypothesis, or ask for one investigation loop.

### ✅ Use Investigation Loops for Refinement
If you want the agent to iteratively improve parameters:

```
agentic-perf submit \
  "Tune CPU frequency scaling for STREAM benchmark convergence" \
  -d "Use investigation loop mode. Test default, 2.0 GHz, 2.5 GHz, 3.0 GHz. Stop when throughput variance < 1%."
```

### 🔐 Never Submit Credentials or PII
Don't include passwords, API keys, or personal information in ticket descriptions. Use the secret management system or environment variables instead.

### 📝 Document Unusual Findings
If you find an agent's recommendation surprising, note it:

```
agentic-perf reply ticket-abc123 "This result is unexpected because [reason]. Can you cross-check with [data source]?"
```

This feedback helps improve the system.

---

## Feedback and Support

### 📊 Give Feedback on Results
Did the agent's recommendation prove correct or wrong? Provide feedback:

```bash
agentic-perf feedback ticket-abc123 thumbs-up --comment "Recommendation saved 2 hours of manual testing."
agentic-perf feedback ticket-abc123 thumbs-down --comment "Result didn't reproduce; hardware may have been in wrong state."
```

### 🆘 Report Issues
Found a bug or unexpected behavior? Report it with:

```bash
agentic-perf show ticket-abc123  # Capture ticket ID and status
# Then file an issue: https://github.com/atheurer/agentic-perf/issues
```

Include:
- Ticket ID
- What you expected to happen
- What actually happened
- Relevant agent output

### 📚 Learn More
- [CLI Reference](cli-reference.md) — Complete command reference
- [Architecture](architecture.md) — How the system works internally
- [Design Philosophy](design-philosophy.md) — Why we made certain decisions
