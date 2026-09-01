# Web dashboard guide

Status: current. The dashboard is served by the state store at `/` (default
`http://localhost:8090`). It uses the same bearer-token authorization as the
API; anonymous read is available only when explicitly configured. Controls are
disabled when the principal lacks ownership/admin permission or the ticket is
not in a compatible state.

The list view shows ticket id, summary, status, owner, outcome, and progress.
The detail view shows the status trail, custom fields, token/cost/cache usage,
comments, live events, transcript, guidance/introspection summary, artifacts,
and results. Polling/event refresh supports pause/scroll and progress views;
the SSE stream and event rendering are best-effort views of persisted events.

Available actions include reply/resume, graceful stop, hard stop, abort,
stop-all (admin), interject, claim/ownership operations, and artifact download.
The UI does not bypass API authorization. Use the CLI or
[rest-api-reference.md](rest-api-reference.md) when an action is disabled.

Result views render Chart.js `ChartSpec` data. They support bar/line/doughnut/
scatter charts, min/max/stddev datasets, multi-panel CDM charts, synchronized
cursors, metric matrices where the returned spec supplies them, and artifact
links/downloads. Missing, malformed, or truncated workspace data is shown as
an unavailable result rather than invented values. Chart generation contracts
are in [workspaces-and-charts.md](workspaces-and-charts.md).
