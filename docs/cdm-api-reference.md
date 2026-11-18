# CDM (Common Data Model) API Reference for Review Agent

## Overview

The CDM query server is an Express.js REST API that runs inside a crucible
controller container on port 3000. It provides access to benchmark results
stored in OpenSearch. The data follows a hierarchical model:

```
run → iterations → samples → periods → metric data
```

## Key Concepts

- **run**: A single invocation of `crucible run`. Identified by a UUID (run-id).
- **iteration**: A unique combination of benchmark parameters within a run.
  For example, fio with `bs=4k,rw=randread` is one iteration; `bs=8k,rw=read`
  is another.
- **sample**: An actual execution of an iteration. Multiple samples per
  iteration provide statistical confidence (mean, stddev).
- **period**: A time window within a sample. Benchmarks define periods like
  "warmup" and "measurement." The **primary period** is where the benchmark's
  main metric is measured.
- **primary metric**: The benchmark's most important metric for an iteration.
  Format is `source::type`, e.g., `fio::iops` or `uperf::Gbps`. Different
  iterations may have different primary metrics.
- **metric source**: The tool or benchmark that produced the data (e.g., `fio`,
  `sar-net`, `mpstat`, `procstat`).
- **metric type**: The specific measurement within a source (e.g., `iops`,
  `Gbps`, `Busy-CPU`, `interrupts-sec`).
- **breakout**: A dimension for disaggregating metric data (e.g., by CPU,
  by network device, by direction). Breakouts allow drilling into which
  specific resource contributed to the aggregate value.

## API Base URL

The CDM server runs on the crucible controller host. The URL is:
```
http://<controller-ip>:3000
```

To access it from the review agent, SSH-tunnel or use the controller's
private IP if running within the same network.

## Endpoints

### Health Check

```
GET /health
```
Returns `{"status": "OK", "timestamp": "..."}` when the server is ready.

### Find Runs

```
GET /api/v1/runs?run=<run-id>
GET /api/v1/runs?name=<user>&email=<email>&harness=<harness>
```
Returns `{"runIds": ["uuid1", "uuid2", ...]}`.

### Run Metadata

```
GET /api/v1/run/<run-id>/tags
```
Returns `{"tags": [{"name": "key", "val": "value"}, ...]}`.

```
GET /api/v1/run/<run-id>/benchmark
```
Returns `{"benchmark": "fio"}`.

### Iterations

```
GET /api/v1/run/<run-id>/iterations
```
Returns `{"iterations": ["iter-uuid-1", "iter-uuid-2", ...]}`.

```
POST /api/v1/run/<run-id>/iterations/params
Body: {"iterations": ["iter-uuid-1", "iter-uuid-2"]}
```
Returns `{"params": [[{"arg": "bs", "val": "4k"}, {"arg": "rw", "val": "randread"}], ...]}`.
One param array per iteration, in order.

```
POST /api/v1/run/<run-id>/iterations/primary-metric
Body: {"iterations": ["iter-uuid-1", "iter-uuid-2"]}
```
Returns `{"primaryMetrics": ["fio::iops", "fio::iops"]}`.
Format is `source::type`. One per iteration.

```
POST /api/v1/run/<run-id>/iterations/primary-period-name
Body: {"iterations": ["iter-uuid-1", "iter-uuid-2"]}
```
Returns `{"periodNames": ["measurement", "measurement"]}`.

### Samples

```
POST /api/v1/run/<run-id>/iterations/samples
Body: {"iterations": ["iter-uuid-1", "iter-uuid-2"]}
```
Returns `{"samples": [["sample-uuid-1", "sample-uuid-2"], ...]}`.
Array of sample ID arrays, one per iteration.

```
POST /api/v1/run/<run-id>/samples/statuses
Body: {"sampleIds": [["sample-uuid-1", "sample-uuid-2"], ...]}
```
Returns `{"statuses": [["pass", "pass"], ...]}`.
Same structure as samples input — status per sample per iteration.

### Periods

```
POST /api/v1/run/<run-id>/samples/primary-period-id
Body: {"sampleIds": [["s1", "s2"], ...], "periodNames": ["measurement", ...]}
```
Returns `{"periodIds": [["period-uuid-1", "period-uuid-2"], ...]}`.

```
POST /api/v1/run/<run-id>/periods/range
Body: {"periodIds": [["period-uuid-1", "period-uuid-2"], ...]}
```
Returns `{"ranges": [[{"begin": 1718000000000, "end": 1718000030000}, ...], ...]}`.
Timestamps are milliseconds since epoch.

### Metric Sources and Types

```
GET /api/v1/run/<run-id>/metric-sources
```
Returns `{"sources": ["fio", "mpstat", "sar-net", "procstat", ...]}`.

```
POST /api/v1/run/<run-id>/metric-types
Body: {"sources": ["fio", "mpstat"]}
```
Returns `{"types": [["iops", "bw-KBps", "latency-usec"], ["Busy-CPU", "NonBusy-CPU"]]}`.

### Metric Data (Time-Series)

```
POST /api/v1/metric-data
Body: {
  "run": "<run-id>",         // or "period": "<period-id>"
  "source": "fio",
  "type": "iops",
  "begin": 1718000000000,    // epoch ms
  "end": 1718000030000,      // epoch ms
  "resolution": 1,           // seconds per data point
  "breakout": [],            // or ["cpu", "direction", ...]
  "filter": ""               // optional regex filter on breakout values
}
```
Returns metric data sets with values per breakout combination:
```json
{
  "values": {
    "": [{"begin": ..., "end": ..., "value": 980093.9, "duration": 1000}],
    "cpu=0": [...],
    "cpu=1": [...]
  }
}
```
When `breakout` is empty, there is a single key `""` with the aggregate value.

### Aggregated Iteration Data (Convenience)

```
POST /api/v1/iterations/metric-values
Body: {"runIds": ["run-uuid-1"]}
```
Returns a comprehensive result object with iterations, samples, primary
metrics, and metric values pre-joined. This is the most efficient way to
get a complete picture of a run's results in a single call.

## Typical Query Flow for Run Review

This is the sequence `get-result-summary.js` uses, and the review agent
should follow a similar pattern:

1. `GET /api/v1/runs?run=<run-id>` — confirm the run exists
2. `GET /api/v1/run/<id>/tags` — get run tags (test metadata)
3. `GET /api/v1/run/<id>/benchmark` — get benchmark name
4. `GET /api/v1/run/<id>/iterations` — get iteration IDs
5. `POST /api/v1/run/<id>/iterations/params` — get params per iteration
6. `POST /api/v1/run/<id>/iterations/primary-metric` — get primary metric per iteration
7. `POST /api/v1/run/<id>/iterations/primary-period-name` — get period names
8. `POST /api/v1/run/<id>/iterations/samples` — get sample IDs per iteration
9. `POST /api/v1/run/<id>/samples/statuses` — check which samples passed
10. `POST /api/v1/run/<id>/samples/primary-period-id` — get period IDs for passed samples
11. `POST /api/v1/run/<id>/periods/range` — get time ranges for those periods
12. `POST /api/v1/metric-data` — for each sample's primary period, get the metric value
13. `GET /api/v1/run/<id>/metric-sources` — list available tool data (sysstat, procstat, etc.)
14. `POST /api/v1/run/<id>/metric-types` — list metric types per source

The mean, min, max, stddev are computed from the per-sample primary metric
values. For primary benchmark metrics the LLM computes these from the
returned values; for continuously sampled tool metrics,
`get_run_metric_by_period` performs the period-scoped math in code.

## Example: Getting fio IOPS for a Run

```bash
# 1. Get iterations
curl -s http://localhost:3000/api/v1/run/540791c8-.../iterations
# {"iterations": ["iter-abc123"]}

# 2. Get primary metric
curl -s -X POST http://localhost:3000/api/v1/run/540791c8-.../iterations/primary-metric \
  -H "Content-Type: application/json" \
  -d '{"iterations": ["iter-abc123"]}'
# {"primaryMetrics": ["fio::iops"]}

# 3. Get samples and their values via metric-data
curl -s -X POST http://localhost:3000/api/v1/metric-data \
  -H "Content-Type: application/json" \
  -d '{"run": "540791c8-...", "source": "fio", "type": "iops",
       "begin": 1718000000000, "end": 1718000030000, "resolution": 1, "breakout": []}'
# {"values": {"": [{"begin": ..., "end": ..., "value": 980093.9, ...}]}}
```

## Notes for the Review Agent

- The CDM server runs on the controller, port 3000. Use the controller's
  SSH-accessible IP (from `ssh_hardware_ips.controller`) for API requests.
- All POST bodies must be `Content-Type: application/json`.
- Iteration/sample/period IDs are UUIDs — get them from the API, don't guess.
- The `metric-data` endpoint requires `begin`/`end` timestamps from the
  period range. Always fetch period ranges before requesting metric data.
- **Prefer `get_run_metric_by_period` for tool metrics.** For continuously
  sampled tool/benchmark metrics (e.g. `mpstat` `Busy-CPU`), the review agent
  exposes `get_run_metric_by_period`, which discovers the run's indexed
  primary periods and queries `metric-data` once per period in code. It
  returns a duration-weighted mean for each period plus an aggregate across
  periods (`n_samples`, `mean`, `min`, `max`, `stddev`). Callers pass only
  `source`/`type` (and an optional `breakout`), so the period time math is not
  left to the model and inter-period idle time is excluded.
- The structured `get_cdm_metric` tool requires a `period` id or both `begin`
  and `end`. A run-scoped metric-data query with no window spans the idle gaps
  between periods and dilutes tool metrics such as CPU utilization, so use
  `list_cdm_periods` to discover valid period ids and scope every query to
  one.
- `breakout` enables per-CPU, per-device, per-direction disaggregation.
  Start with empty breakout for aggregate values, then drill down if needed.
- Tool metrics (sysstat, procstat, mpstat) provide host-level context:
  CPU utilization, memory, interrupts, context switches. Query these to
  understand resource utilization during the benchmark.
