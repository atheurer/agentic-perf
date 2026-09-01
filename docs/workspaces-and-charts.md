# Ticket workspaces, spilling, and charts

Status: current. Each ticket has a private scratchpad at
`$AGENTIC_PERF_HOME/tickets/<ticket-id>/workspace/`. Tool output exceeding
`tool_spill_threshold` is saved there and returned as a `workspace://relative`
reference; the threshold is configurable through the agent configuration.

Agents use `list_workspace_files`, `read_file_slice`, `grep_file`, and
`jq_query`. Queries are bounded and previews may be truncated: inspect the
returned `status`, size, and truncation metadata before relying on a result.
`jq_query` runs a jq filter against JSON and has limits for item count/output;
`read_file_slice` supports byte or line windows. References are resolved inside
the ticket workspace and path traversal is rejected.

Workspace files persist across agent handoffs and are included in the
workspace manifest/context supplied to the next agent. They are removed only
when the ticket data is cleaned up by the operator; they are not a general
shared filesystem or an authorization bypass. Restrict filesystem permissions
on the agent home and do not spill secrets deliberately.

## Charts

`generate_chart_from_workspace` selects a registered adapter (currently generic
JSON/metrics, CDM, and kube-burner adapters where their inputs match) and
returns a `ChartSpec`. The dashboard consumes the spec rather than arbitrary
HTML or JavaScript.

```json
{
  "title": "throughput",
  "type": "line",
  "labels": ["run-1", "run-2"],
  "datasets": [{"label":"MB/s", "values":[100,120], "unit":"MB/s"}],
  "panels": [], "source_file":"workspace://results.json",
  "sync_id":"storage"
}
```

Supported chart types are `bar`, `line`, and `doughnut`. The chart payload
should provide the dataset `values` consumed by the dashboard. Do not rely on
unregistered chart types or auxiliary statistical fields being rendered.
CDM specs can include synchronized panels/cursors through `sync_id`. The
dashboard renders the supported result with Chart.js and displays the source
file.

To add an adapter, implement `BaseChartAdapter.can_handle()` and
`build_chart()`, register it in the chart registry, and add focused tests for
input detection, units, labels, empty data, and malformed data. Do not claim a
new adapter is available until it is registered.
