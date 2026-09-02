# Data flow, retention, and redaction

Status: current implementation notes. A client submits a ticket to the
FastAPI state store. The orchestrator claims it, dispatches a scoped agent,
and records transitions, tool events, usage, and results back to the store.
Artifacts and large tool responses are stored under the configured artifact or
ticket workspace paths; the dashboard reads them through API endpoints.

## Storage and archive behavior

Default roots are under `~/.agentic-perf` (override with
`AGENTIC_PERF_HOME`, `AGENTIC_PERF_ARTIFACTS`, and related path variables).
`agentic-perf archive` and `DELETE /tickets/{id}` apply only to closed tickets:
they remove the ticket from active service and move ticket/event files to the
archive directory. They do not promise secure deletion, result destruction,
or seven-day automatic purge/compression. `purge`, `export-logs`, `feedback`,
and `emergency-stop` are not current CLI commands.

## Redaction boundary

The `Redactor` applies registered per-ticket secret values and pattern rules
to event and audit payloads, including recursive structures and progress text.
It is fail-closed on redaction errors. Redaction is not encryption, cannot
recover secrets already submitted, and does not sanitize arbitrary external
systems or old unprocessed files. Operators must use filesystem permissions,
encrypted storage/backups, and secret providers; scrub historical event logs
with `scripts/scrub-event-logs.py` after reviewing its scope and backup needs.

Never put private keys, bearer tokens, passwords, or customer data in ticket
text. Use runtime secret resolution. Treat hostnames, IPs, commands, results,
workspace files, transcripts, and artifacts as potentially sensitive.
