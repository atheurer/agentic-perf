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
the phase, policy, effective source, assumptions, and selected workspace
references. Triage uses GitHub context. Post-provision benchmark construction
uses controller context only when the ticket identifies a reachable installed
controller, its snapshot is available, and no update is expected; otherwise it
uses GitHub until controller refresh. The benchmark context gateway performs
that refresh through the ticket's SSH context, reading only the Crucible
catalog, safe documentation paths, and the selected benchmark's metadata into
the workspace. Unknown update policy is recorded as a `no_update` assumption.
Alternate sources remain available for drift comparison, but agents should
follow the effective manifest rather than read all cached context. Existing
files at the workspace root remain valid for compatibility.
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

## Crucible context gateway

Crucible-specific documentation is retrieved through the unified
`get_crucible_benchmark_context` gateway. Use `operation=list` to discover
source-backed documents and `operation=read` for a selected path. Stable
namespaces are `core/...` for the pinned Crucible checkout and
`benchmark/<name>/...` for the catalog-selected benchmark checkout. Explicitly
mapped local overlays use `local/...` and are supplemental, with harness,
benchmark, phase, agent, subject-area, and provenance metadata from
`skills/context-manifest.json`. The gateway discovers files under an explicit
safe-document policy, so renamed or newly added upstream documentation does not
require prompt changes; it returns repository, ref, commit, and deterministic
subject-area guidance for run-files, endpoints, execution, engines, tools,
benchmark semantics, and results.

Subject-area selection accepts a list such as `["run-file", "endpoints",
"execution"]`; separate calls are also deterministic. Comma-separated strings
are accepted for compatibility and normalized into the same selection.

Read operations persist source material and the phase-owned effective-context
manifest in the ticket workspace. Alternate snapshots are retained for
comparison and are not part of an agent's default view. The legacy
`list_harness_docs`, `read_harness_doc`, `read_skills`, and structured lookup
tools remain registered for migration and for harnesses without a source-aware
gateway. The Crucible benchmark agent uses the gateway for context and has the
three legacy document tools scoped out; other harnesses retain their existing
tool behavior.

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
