# Operations runbook

Status: current. The state store is on the configured port (default 8090).
Start with health and authenticated ticket/event inspection:

```bash
BASE=${STATE_STORE_URL:-http://localhost:8090}
TOKEN=$(cat "${AGENTIC_PERF_SECRETS:-$HOME/.agentic-perf/secrets}/api-token")
curl -fsS "$BASE/api/v1/health"
curl -fsS -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/tickets"
agentic-perf watch ID -f -v
```

## Stuck or unsafe work

- Check status, latest event, owner/claim, and orchestrator logs. A claim is a
  renewable 300-second lease; confirm the old process is stopped before taking
  over an expired claim.
- Graceful stop: `agentic-perf stop ID`; immediate cancellation:
  `agentic-perf stop ID --hard`; fleet-wide emergency: `agentic-perf stop-all
  --yes --hard`.
- Paused ticket: `agentic-perf reply ID "..."`; abort: `agentic-perf abort ID
  "reason"`; administrative escape: POST `/tickets/ID/force-close` and then
  independently verify resources.
- Check quota/rate-limit/context failures in events and usage. Do not solve a
  429 by disabling limits globally; correct the caller or wait for retry.

## Capacity and data safety

Monitor `$AGENTIC_PERF_HOME/tickets`, `logs`, `artifacts`, `skill-cache`, and
`investigation-records`, plus provider-side resources. Large outputs grow the
per-ticket workspace. Event/audit redaction is implemented for registered
secret values and known patterns, but operators must still avoid putting
secrets or customer data in tickets and should restrict the home directory.

`agentic-perf archive ID` (or `--all-closed`) archives closed ticket/event
files; it does not purge data. The DELETE ticket API has the same archive
semantics. Back up before archiving and retain the archive for compliance.

For model startup, validate provider credentials/config and run a mock smoke
ticket. For introspection, inspect the ticket's introspection/guidance fields;
it may run deterministically when its LLM toggle is disabled. For multi-instance
failures, compare `AGENTIC_PERF_HOME`, instance name, port, token, and PID/log
paths; see [multi-instance.md](multi-instance.md).
