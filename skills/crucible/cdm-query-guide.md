# CDM query workflow for agentic-perf

Use the review agent's `get_run_summary`, `list_cdm_periods`,
`get_cdm_metric`, and `get_run_metric_by_period` tools for CDM queries. Use the
run summary's reported metric inventory to check which sources and types are
available before querying. Read Crucible's current CDM documentation through
get_skill_context for request fields, filters, aggregation, and breakout
semantics; these details can vary by installed version and metric.

For each source/type combination, first inspect the available breakouts returned
by CDM, then request only dimensions needed to answer the ticket. Do not reuse a
breakout or aggregation assumption from a different metric. For multi-pair
results, use the run file and the returned metric metadata to map values to
benchmark IDs and hosts. If the API or metric metadata does not establish that
mapping, present the ambiguity for clarification rather than guessing.

Scope every metric-data query to a benchmark's active period. A run spans idle
gaps between periods (sample setup/teardown, inter-sample pauses); averaging a
tool metric such as mpstat CPU over the whole run dilutes real busy-time
utilization with idle time. Use the period-scoped tooling below rather than a
bare run scope.

For continuously sampled tool metrics (for example `mpstat` Busy-CPU, `sar-net`,
or `procstat`), use the `get_run_metric_by_period` tool. It discovers the run's
indexed primary periods and queries metric-data once per period in code,
returning a duration-weighted mean for each period plus an aggregate across
periods (`n_samples`, `mean`, `min`, `max`, `stddev`) with inter-period idle
time excluded. Supply only `source`/`type` (and an optional `breakout`); the
tool does the period time math. For a single period, `get_cdm_metric` accepts a
`period` id or both `begin` and `end` taken from `.../periods/range`.

Use read_run_results only for data that is not indexed in CDM or when CDM
cannot provide the requested evidence. Prefer bounded queries and cite the
returned values and period IDs in the review.
