# CLI Reference

Complete reference for the `agentic-perf` command-line interface.

All commands accept `--store-url URL` to override the state store address
(default: `http://localhost:8090`). This can also be set via the
`STATE_STORE_URL` environment variable.

## submit

Create a new test ticket and start the pipeline.

```
agentic-perf submit SUMMARY [-d DESCRIPTION]
```

| Argument | Required | Description |
|---|---|---|
| `SUMMARY` | Yes | Natural-language test request (also used as the ticket summary) |
| `-d`, `--description` | No | Detailed description. Defaults to the summary if omitted. |

The ticket is created in `new` status and immediately transitioned to
`triage_pending`, which triggers the triage agent.

### Examples

```bash
# Simple request — summary only
agentic-perf submit "Run a 4K random read fio test"

# With description providing hosts and configuration
agentic-perf submit \
  "Compare NVMe throughput: 4K vs 128K block sizes" \
  -d "Controller: 10.1.2.1. Endpoint: 10.1.2.2. SSH key: ~/.ssh/id_ed25519. Use crucible with fio."

# Request using a specific resource provider
agentic-perf submit \
  "STREAM memory bandwidth test on bare metal" \
  -d "Use QUADS to get a host. Run with zathras."

# Kubernetes workload
agentic-perf submit \
  "Run kube-burner node-density test" \
  -d "Use AWS EC2. Deploy K3s. 100 pods per node."
```

## list

List tickets, optionally filtered by status.

```
agentic-perf list [-s STATUS]
```

| Argument | Required | Description |
|---|---|---|
| `-s`, `--status` | No | Filter by ticket status (e.g., `executing_benchmark`, `closed`) |

### Examples

```bash
agentic-perf list                          # All tickets
agentic-perf list -s executing_benchmark   # Only running benchmarks
agentic-perf list -s closed                # Completed tickets
```

## show

Display full ticket details including custom fields and comments.

```
agentic-perf show TICKET_ID
```

Shows: ticket ID, status, summary, all custom fields (triage results,
resource allocations, benchmark run IDs, review verdicts), and the
comment thread.

### Example

```bash
agentic-perf show abc12345-def6-7890-abcd-ef1234567890
```

## watch

Watch ticket progress in real time.

```
agentic-perf watch TICKET_ID [-i SECONDS] [-f] [-v]
```

| Argument | Required | Description |
|---|---|---|
| `TICKET_ID` | Yes | Ticket to watch |
| `-i`, `--interval` | No | Poll interval in seconds (default: 3.0) |
| `-f`, `--follow` | No | Keep watching after HITL pauses (don't exit at `awaiting_customer_guidance`) |
| `-v`, `--verbose` | No | Show agent events: tool calls, LLM interactions, transitions |

Without `-v`, watch shows only status changes and comments. With `-v`, it
reads the event log from `~/.agentic-perf/logs/` and displays tool calls,
LLM responses, and transitions as they happen.

Exits automatically when the ticket reaches `closed` status.

### Examples

```bash
# Basic — status changes only
agentic-perf watch TICKET_ID

# Follow mode with verbose output
agentic-perf watch TICKET_ID -f -v

# Faster polling
agentic-perf watch TICKET_ID -f -v -i 1
```

## reply

Respond to an agent's question when the ticket is paused at
`awaiting_customer_guidance`.

```
agentic-perf reply TICKET_ID MESSAGE [--abort]
```

| Argument | Required | Description |
|---|---|---|
| `TICKET_ID` | Yes | Ticket to reply to |
| `MESSAGE` | Yes | Your response text |
| `--abort` | No | Abort the ticket after replying (skips to teardown) |

The reply is added as a comment, and the ticket resumes to its
`previous_status` so the agent can continue. If `--abort` is specified,
the ticket moves to `awaiting_teardown` instead.

Fails if the ticket is not in `awaiting_customer_guidance` status.

### Examples

```bash
# Approve a run-file
agentic-perf reply TICKET_ID "Approved, looks good"

# Provide configuration the agent asked for
agentic-perf reply TICKET_ID "Use 8 cores and 4K block size"

# Reply and abort
agentic-perf reply TICKET_ID "Wrong config, cancel" --abort
```

## abort

Abort a paused ticket and skip directly to teardown and cleanup.

```
agentic-perf abort TICKET_ID [REASON]
```

| Argument | Required | Description |
|---|---|---|
| `TICKET_ID` | Yes | Ticket to abort |
| `REASON` | No | Reason for aborting (recorded as a comment) |

Only works when the ticket is in `awaiting_customer_guidance` status.
Posts the reason as a comment and transitions to `awaiting_teardown`.

### Examples

```bash
# Abort with default reason
agentic-perf abort TICKET_ID

# Abort with explanation
agentic-perf abort TICKET_ID "Wrong hardware allocated, need to restart"
```

## transcript

View the full agent conversation log for a ticket.

```
agentic-perf transcript TICKET_ID [--json] [--agent AGENT_NAME]
```

| Argument | Required | Description |
|---|---|---|
| `TICKET_ID` | Yes | Ticket to show transcript for |
| `--json` | No | Output raw events as JSON instead of formatted text |
| `--agent` | No | Filter to a single agent (e.g., `triage-agent`, `benchmark-agent`) |

Reads events from `~/.agentic-perf/logs/{ticket_id}.jsonl` and renders a
formatted transcript showing:
- User request
- Per-agent sections with system prompt preview
- LLM response text and tool calls
- Tool call inputs and results
- Status transitions
- Comments

### Examples

```bash
# Full transcript
agentic-perf transcript TICKET_ID

# Just the benchmark agent's conversation
agentic-perf transcript TICKET_ID --agent benchmark-agent

# Raw JSON for programmatic processing
agentic-perf transcript TICKET_ID --json

# Pipe to a file
agentic-perf transcript TICKET_ID > ticket-transcript.txt
```

## health

Check the state store status and ticket counts.

```
agentic-perf health
```

Reports:
- State store status (healthy/unhealthy)
- Total ticket count
- Ticket counts by status (only non-zero statuses shown)

### Example

```bash
$ agentic-perf health
State store: healthy
Total tickets: 12
  closed: 8
  executing_benchmark: 2
  awaiting_review: 1
  awaiting_customer_guidance: 1
```

## cleanup

Find and optionally terminate orphaned AWS EC2 instances tagged by
agentic-perf.

```
agentic-perf cleanup [--older-than HOURS] [--terminate] [-y]
```

| Argument | Required | Description |
|---|---|---|
| `--older-than` | No | Only show instances older than N hours |
| `--terminate` | No | Terminate matched instances (default: list only) |
| `-y`, `--yes` | No | Skip confirmation prompt when terminating |

Looks for running or stopped EC2 instances with the `agentic-perf=true`
tag. Requires AWS credentials at `~/.agentic-perf/secrets/aws/config.json`.

### Examples

```bash
# List all agentic-perf instances
agentic-perf cleanup

# List instances older than 24 hours
agentic-perf cleanup --older-than 24

# Terminate instances older than 48 hours, no prompt
agentic-perf cleanup --older-than 48 --terminate -y
```

## Ticket Statuses

For reference, the valid ticket statuses are:

| Status | Description |
|---|---|
| `new` | Just created, not yet triaged |
| `triage_pending` | Triage agent is parsing the request |
| `awaiting_hardware` | Resource agent is acquiring hosts |
| `awaiting_provision` | Provisioning agent is installing the harness |
| `executing_benchmark` | Benchmark agent is running the test |
| `awaiting_review` | Review agent is analyzing results |
| `awaiting_teardown` | Resource agent is cleaning up |
| `awaiting_customer_guidance` | Paused for human input |
| `closed` | Terminal — all work complete |
