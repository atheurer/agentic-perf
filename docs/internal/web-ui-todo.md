# Web UI — TODO

## Near Term

- **Migrate to React + Vite** — current vanilla JS works but will get unwieldy as we add features. Keep data fetching logic separate so the migration is mechanical.
- **SSE or WebSocket push** — replace polling with server-pushed events for lower latency and less wasted traffic on idle tickets.
- **Submit ticket from UI** — form to create a new ticket (POST `/api/v1/tickets` + transition to `triage_pending`), replacing the CLI for casual use.
- **Reply to ticket from UI** — text input to post a comment when a ticket is in `awaiting_customer_guidance`, so users can respond without switching to the CLI.
- **Refresh ticket status in detail view** — the header status badge updates on transition events, but if the user loads a stale page the badge doesn't re-fetch. Periodic ticket re-fetch or a status poll would fix this.
- **Event type filtering** — toggle visibility of event types in the transaction log (e.g., hide `llm_request` noise, show only tool calls and transitions).
- **Search within transcript** — text search across event content to find specific tool calls, errors, or keywords.

## Medium Term

- **Per-ticket process isolation** — ability to restart agentic-perf for new tickets without disturbing in-progress ones. The stateless agent model makes this feasible. The web UI would need to handle reconnection gracefully.
- **Multi-ticket live view** — dashboard showing multiple in-progress tickets side by side or in a summary grid with latest activity.
- **Timeline visualization** — horizontal timeline showing agent phases as colored segments with duration, making it easy to see where time is spent.
- **Custom field inspector** — richer display for structured custom fields (parsed_specs, assigned_hardware, run results) instead of raw JSON.
- **Notification/alerts** — browser notifications when a ticket transitions to `awaiting_customer_guidance` or hits an error.

## Longer Term

- **Authentication** — when exposed beyond localhost, add basic auth or token-based access control.
- **Run results integration** — embed CDM metric charts or link to the CDM web UI for tickets that have completed benchmark runs.
- **Historical analytics** — aggregate views across closed tickets: average time per phase, failure rates, resource utilization trends.
