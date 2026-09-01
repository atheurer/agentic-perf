# CLI Reference

Complete reference for the command-line interface. Run it from the checkout
with `python3 cli.py`; the project does not currently install an
`agentic-perf` console entry point.

The global `--store-url URL` option must appear before the command. It
overrides the state store address (default: `http://localhost:8090`) and can
also be set with `STATE_STORE_URL`.

## submit

Create a new test ticket and start the pipeline.

```
python3 cli.py submit SUMMARY [-d DESCRIPTION] [--owners USER1,USER2] [--stop-after STEP]
```

| Argument | Required | Description |
|---|---|---|
| `SUMMARY` | Yes | Natural-language test request (also used as the ticket summary) |
| `-d`, `--description` | No | Detailed description. Defaults to the summary if omitted. |
| `--owners` | No | Comma-separated owners (multi-user mode). |
| `--stop-after STEP` | No | Stop after `triage`, `resource`, `provision`, `benchmark`, or `review` (debugging). |

The ticket is created in `new` status and immediately transitioned to
`triage_pending`, which triggers the triage agent.

### Examples

```bash
# Simple request — summary only
python3 cli.py submit "Run a 4K random read fio test"

# With description providing hosts and configuration
python3 cli.py submit \
  "Compare NVMe throughput: 4K vs 128K block sizes" \
  -d "Controller: 10.1.2.1. Endpoint: 10.1.2.2. SSH key: ~/.ssh/id_ed25519. Use crucible with fio."

# Request using a specific resource provider
python3 cli.py submit \
  "STREAM memory bandwidth test on bare metal" \
  -d "Use QUADS to get a host. Run with zathras."

# Kubernetes workload
python3 cli.py submit \
  "Run kube-burner node-density test" \
  -d "Use AWS EC2. Deploy K3s. 100 pods per node."
```

## list

List tickets, optionally filtered by status.

```
python3 cli.py list [-s STATUS]
```

| Argument | Required | Description |
|---|---|---|
| `-s`, `--status` | No | Filter by ticket status (e.g., `executing_benchmark`, `closed`) |

### Examples

```bash
python3 cli.py list                          # All tickets
python3 cli.py list -s executing_benchmark   # Only running benchmarks
python3 cli.py list -s closed                # Completed tickets
```

## show

Display full ticket details including custom fields and comments.

```
python3 cli.py show TICKET_ID
```

Shows: ticket ID, status, summary, all custom fields (triage results,
resource allocations, benchmark run IDs, review verdicts), and the
comment thread.

### Example

```bash
python3 cli.py show abc12345-def6-7890-abcd-ef1234567890
```

## watch

Watch ticket progress in real time.

```
python3 cli.py watch TICKET_ID [-i SECONDS] [-f] [-v]
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
python3 cli.py watch TICKET_ID

# Follow mode with verbose output
python3 cli.py watch TICKET_ID -f -v

# Faster polling
python3 cli.py watch TICKET_ID -f -v -i 1
```

## reply

Respond to an agent's question when the ticket is paused at
`awaiting_customer_guidance`.

```
python3 cli.py reply TICKET_ID MESSAGE [--abort] [--model MODEL]
                         [--provider PROVIDER] [--effort low|medium|high]
                         [--max-iterations N] [--remember]
```

| Argument | Required | Description |
|---|---|---|
| `TICKET_ID` | Yes | Ticket to reply to |
| `MESSAGE` | Yes | Your response text |
| `--abort` | No | Abort the ticket after replying (skips to teardown) |
| `--model MODEL` | No | Override the next agent's LLM model. |
| `--provider PROVIDER` | No | Override the next agent's LLM provider. |
| `--effort` | No | Reasoning effort: `low`, `medium`, or `high`. |
| `--max-iterations N` | No | Override the next agent's iteration cap. |
| `--remember` | No | Resume with context from the previous attempt. |

The reply is added as a comment, and the ticket resumes to its
`previous_status` so the agent can continue. If `--abort` is specified,
the ticket moves to `awaiting_teardown` instead.

Fails if the ticket is not in `awaiting_customer_guidance` status.

### Examples

```bash
# Approve a run-file
python3 cli.py reply TICKET_ID "Approved, looks good"

# Provide configuration the agent asked for
python3 cli.py reply TICKET_ID "Use 8 cores and 4K block size"

# Reply and abort
python3 cli.py reply TICKET_ID "Wrong config, cancel" --abort
```

## abort

Abort a paused ticket and skip directly to teardown and cleanup.

```
python3 cli.py abort TICKET_ID [REASON]
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
python3 cli.py abort TICKET_ID

# Abort with explanation
python3 cli.py abort TICKET_ID "Wrong hardware allocated, need to restart"
```

## transcript

View the full agent conversation log for a ticket.

```
python3 cli.py transcript TICKET_ID [--json] [--agent AGENT_NAME]
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
python3 cli.py transcript TICKET_ID

# Just the benchmark agent's conversation
python3 cli.py transcript TICKET_ID --agent benchmark-agent

# Raw JSON for programmatic processing
python3 cli.py transcript TICKET_ID --json

# Pipe to a file
python3 cli.py transcript TICKET_ID > ticket-transcript.txt
```

## health

Check the state store status and ticket counts.

```
python3 cli.py health
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
python3 cli.py cleanup [--older-than HOURS] [--terminate] [-y]
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
python3 cli.py cleanup

# List instances older than 24 hours
python3 cli.py cleanup --older-than 24

# Terminate instances older than 48 hours, no prompt
python3 cli.py cleanup --older-than 48 --terminate -y
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

## Additional commands

### `approve`, `deny`, `stop`, and `stop-all`

```text
python3 cli.py approve TICKET_ID [--ticket]
python3 cli.py deny TICKET_ID
python3 cli.py stop TICKET_ID [--hard]
python3 cli.py stop-all [--hard] [--yes|-y]
```

`approve` and `deny` operate on a pending command-approval record. Approval
is one-time by default; `--ticket` remembers the binary/host approval for
the ticket. This is legacy/manual command-approval behavior, distinct from
the normal benchmark run-file HITL flow handled by `reply`.

`stop` requests a graceful stop, or an immediate stop with `--hard`.
`stop-all` applies the same choice to every active ticket and asks for
confirmation unless `--yes` (or `-y`) is supplied.

### `archive`

```text
python3 cli.py archive [TICKET_ID ...] [--all-closed]
```

Archive only closed tickets. Supply IDs, or omit them and use `--all-closed`
to archive every closed ticket.

### `user`, `group`, `whoami`, `handoff`, and `claim`

```text
python3 cli.py whoami
python3 cli.py claim TICKET_ID
python3 cli.py handoff TICKET_ID [--add USER] [--remove USER]
python3 cli.py user create USERNAME [--admin]
python3 cli.py user list
python3 cli.py user disable USERNAME
python3 cli.py user enable USERNAME
python3 cli.py user rotate-token USERNAME
python3 cli.py group create NAME [-d DESCRIPTION]
python3 cli.py group list
python3 cli.py group delete NAME
python3 cli.py group add-member NAME USERNAME
python3 cli.py group remove-member NAME USERNAME
```

`whoami` shows the authenticated identity. `claim` adds the current user as
owner and requires a user token. `handoff` adds or removes an owner and then
prints the owner list; removing the last owner is rejected. User and group
administration commands require the applicable permissions; `--admin` grants
administrator privileges when creating a user.

### `cleanup`

```text
python3 cli.py cleanup [--older-than HOURS] [--terminate] [--yes|-y]
                       [--all-instances]
```

By default, list tagged AWS instances for the current deployment.
`--older-than` filters by age, `--terminate` terminates matches, `--yes`
skips confirmation, and `--all-instances` includes every deployment.
Credentials are read from `~/.agentic-perf/secrets/aws/config.json`.

## Parser parity check

The argparse parser in `cli.py` is authoritative. Run this focused check
after parser changes (the command set is coordinated with issue #651):

```bash
python3 cli.py --help
for command in submit list show watch reply approve deny abort stop stop-all \
  transcript health user group whoami handoff claim archive cleanup; do
  python3 cli.py "$command" --help >/dev/null || exit 1
done
```

See [Ticket Directives](ticket-directives.md) for API-submitted fields and
[E2E Testing](e2e-testing.md) for a complete startup example.
