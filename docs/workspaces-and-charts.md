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

## Namespaces and manifest kinds

New producers should use these ticket-workspace namespaces:

| Namespace | Purpose | Manifest `kind` examples |
|---|---|---|
| `context/` | Shared and benchmark-specific source guidance | `source_context` |
| `runfiles/` | Generated or validated harness run files | `runfile` |
| `results/` | Small summaries and metrics; references under `results/raw/` | `result_summary`, `raw_artifact` |
| `logs/` | Bounded logs or log references | `log` |
| `metadata/` | Run and provenance metadata | `metadata` |
| `scratch/` | Temporary agent data | `scratch` |

Crucible source snapshots are kept separately under
`context/sources/github/...` and `context/sources/controller/...`, with
provenance alongside each snapshot. `context/effective-context.json` records
phase-owned policy and selected workspace references without exposing source
selection to agents. The context gateway chooses the applicable source
internally, refreshes controller context through the ticket's SSH context when
appropriate, and caches only documents requested through the generic bootstrap,
search, and read operations. Agents do not inspect source snapshots or
repository catalogs directly. Existing files at the workspace root remain
valid for compatibility.
`list_workspace_files` includes `namespace` and `kind` so consumers can
distinguish these files without parsing their names.

Large raw results remain in the artifact store. Store only a small JSON object
containing its `artifact_ref` under `results/raw/`, then use the normal bounded
`jq_query`, `grep_file`, or `read_file_slice` tools for workspace data.

MCP capability availability is separate from default artifact visibility: an
agent may have the four workspace tools in its tool list while policy still
blocks an alternate source snapshot. Pass the explicit `include_alternates`
comparison option only when performing source drift/comparison work. Files
without a policy-manifest entry remain visible for backward compatibility.

## Skill gateway

Crucible agents use `get_skill_context` with subject `harness/crucible`:

```json
{"subject": "harness/crucible", "operation": "bootstrap"}
{"subject": "harness/crucible", "operation": "read", "ref": "<returned-ref>"}
{"subject": "harness/crucible", "operation": "search", "query": "example|another"}
```

Bootstrap advertises organization entrypoints, approved configuration views,
and phase-compatible software references. Read returned refs, or follow a
relative pointer with `from_ref` and `path`. Organization and software refs
remain distinct. Triage retains bounded GitHub catalog discovery; execution
and review use designated-controller documentation.

Organization documents and service configuration are pinned together for the
ticket lifetime. Raw configuration snapshots stay outside workspaces and
artifacts. Source availability, phase visibility, path containment, controller
caching, and audit records remain server-managed. Private administrator paths
and secret bindings are not model-selected resources. See the
[skill gateway design](design-skill-gateway.md).

The legacy `list_harness_docs`, `read_harness_doc`, and `read_skills` tools
remain available for other harnesses. They do not serve Crucible context.
Workspace files persist across handoffs; filesystem permissions and artifact
policy still apply to their contents.

## Charts

`generate_chart_from_workspace` selects a registered adapter (currently generic
JSON/metrics, CDM, and kube-burner adapters where their inputs match) and
stores a validated `ChartSpec`. Its optional `jq_filter` is applied to the
source workspace JSON before adapter selection. Invalid, unavailable, timed
out, multi-value, and null filters fail without creating a chart artifact.

The tool returns compact control-plane metadata (`chart_ref`, label, dataset,
and panel counts, plus a summary). The complete chart data remains only in the
referenced artifact until review submission loads it for the dashboard. The
dashboard consumes the spec rather than arbitrary HTML or JavaScript.

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

Generated charts must contain labels and at least one dataset with non-empty
values aligned to those labels. Review submissions that provide `chart_ref`
validate the referenced artifact before completing.
