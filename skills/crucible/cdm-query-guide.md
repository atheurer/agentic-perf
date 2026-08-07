# CDM Query Guide — Breakouts and Per-Pair Analysis

## Batch queries — save iterations with cdm_api_requests

**Use `cdm_api_requests` for all CDM queries** — it is the only CDM query
tool available. Pass a list of requests; they run concurrently and all
results return in one call, saving iterations.

```json
{
  "controller": "controller.example.com",
  "requests": [
    {
      "label": "uperf_gbps_s1",
      "method": "POST",
      "path": "/api/v1/metric-data",
      "body": {"run": "<run_id>", "period": "<period_s1>",
               "source": "uperf", "type": "Gbps", "resolution": 180}
    },
    {
      "label": "cpu195_s1",
      "method": "POST",
      "path": "/api/v1/metric-data",
      "body": {"run": "<run_id>", "period": "<period_s1>",
               "source": "mpstat", "type": "Busy-CPU",
               "breakout": ["hostname", "num=195", "type"],
               "resolution": 180, "aggregation": "sum"}
    },
    {
      "label": "cwnd_s1",
      "method": "POST",
      "path": "/api/v1/metric-data",
      "body": {"run": "<run_id>", "period": "<period_s1>",
               "source": "tcp-window", "type": "snd-cwnd",
               "breakout": ["hostname", "dport=30003"],
               "resolution": 180}
    }
  ]
}
```

Results are keyed by the `label` field you provide.

## Discovering available breakouts

**CRITICAL: breakouts are per source:type combination.**
The available breakouts for `mpstat::Busy-CPU` are DIFFERENT
from `procstat::interrupts-sec`. You MUST NOT assume a
breakout name from one source:type works for another.

For example:
- `mpstat::Busy-CPU` uses `num` for CPU number
- `procstat::interrupts-sec` uses `cpu` for CPU number
- Using `cpu` with mpstat will fail; using `num` with
  procstat will fail

## mpstat::Busy-CPU — aggregation bug (use "sum")

**Known bug (perftool-incubator/tool-sysstat#65):** `mpstat::Busy-CPU`
is stored with `default-aggregation: avg`, but the underlying data is
one sample per CPU type (usr, sys, soft, irq, …). These types are
mutually exclusive time slices that together sum to total CPU busy time.
Averaging them instead of summing them produces a value ~4× too low
(e.g., 25% instead of 99% for a saturated core).

**Workaround — always pass `"aggregation": "sum"`** when querying
`mpstat::Busy-CPU` without `type` in the breakout:

```json
{
  "source": "mpstat",
  "type": "Busy-CPU",
  "breakout": ["hostname", "num=195"],
  "aggregation": "sum",
  "resolution": 180
}
```

Alternatively, include `"type"` in the breakout to get per-mode values
and sum them manually. Do NOT rely on the default aggregation for this
metric.

**For every new source:type combination you query**, first
query WITHOUT breakouts to discover what's available via
`remainingBreakouts`. That list ONLY applies to that
specific source:type — do not reuse it for other
combinations.

If a query fails, READ the error message — the CDM API
returns the list of available breakouts and metric types
in the error response. Adjust and retry. Do NOT fall back
to reading raw files.

Example response (no breakouts requested):
```json
{
  "values": {
    "": [{"begin": ..., "end": ..., "value": 49.36}]
  },
  "usedBreakouts": [],
  "remainingBreakouts": [
    "benchmark-group",
    "benchmark-name",
    "benchmark-role",
    "cmd",
    "csid",
    "cstype",
    "engine-id",
    "engine-role",
    "engine-type",
    "tool-name"
  ]
}
```

Then re-query with the breakout you need:
```json
{
  "run": "<run-id>",
  "period": "<period-id>",
  "source": "uperf",
  "type": "Gbps",
  "breakout": ["csid"]
}
```

## Separating parallel pairs (e.g., RHEL 9 vs RHEL 10)

When a crucible run uses multiple IDs (e.g., `"ids": "1+2"`
for two OS pairs running in parallel), the default metric
query returns COMBINED results from all pairs. You MUST use
a breakout to get per-pair data.

For **benchmark metrics** (e.g., uperf Gbps):
- Use `csid` breakout — returns one value per
  client-server pair, labeled by csid
- `hostname` is NOT available as a breakout for benchmark
  metrics

For **tool metrics** (e.g., mpstat Busy-CPU, sar):
- `csid` works but labels are tool instance names like
  `remotehosts-1-sysstat-1`, not numeric IDs
- The tool instance number matches the order of remotes
  in the run file (sysstat-1 = first remote, sysstat-2 =
  second remote, etc.)
- Map tool instances to OS pairs using the run file's
  remote ordering

## Time-resolved queries with resolution

By default, CDM returns one aggregated value per period. Use
the `resolution` parameter to get multiple data points over
the period, revealing changes over time.

```json
{
  "run": "<run-id>",
  "period": "<period-id>",
  "source": "procstat",
  "type": "Busy-CPU",
  "breakout": ["hostname", "cpu"],
  "resolution": 10
}
```

- `resolution` = total number of data points in the period
- For a 30-second test, resolution=10 gives ~3s intervals
- Sample collection interval is rarely under 3 seconds
- Use this to detect CPU saturation that averages hide
  (e.g., 100% for 24s then 0% for 6s averages to 80%)
- Use this to detect irqbalance migrating IRQ destinations

## Filtering results to reduce response size

Use the `filter` parameter to exclude irrelevant data points.
This is critical on many-core systems where per-CPU queries
return hundreds of entries, most near zero.

**Syntax:** `"filter": ["gt:<value>"]` — return only values
greater than the threshold.

```json
{
  "run": "<run-id>",
  "period": "<period-id>",
  "source": "procstat",
  "type": "Busy-CPU",
  "breakout": ["hostname", "cpu"],
  "filter": ["gt:0.03"]
}
```

This returns only CPUs with >0.03% utilization, eliminating
hundreds of idle-CPU entries on a 768-thread system.

**Common filter patterns:**
- `"filter": ["gt:0.03"]` — exclude idle/noise-level CPUs
  (good default for Busy-CPU on many-core systems)
- `"filter": ["gt:0"]` — exclude exact zeros (useful for
  interrupts-sec to find only CPUs handling interrupts)
- `"filter": ["gt:100"]` — find only high-rate interrupt
  sources

Always use filters when querying per-CPU or per-IRQ data
on many-core systems. Without filtering, a 768-CPU system
returns ~768 entries per metric, most of which are noise.

## Filtering breakouts to specific values

To query only specific CPUs, interfaces, or other breakout
values, put the filter **inside the breakout array**:

```json
{
  "source": "procstat",
  "type": "Busy-CPU",
  "breakout": ["hostname", "cpu=738"],
  "resolution": 60
}
```

This returns data for CPU 738 only. For multiple values,
do separate queries (one per CPU).

Other breakout filter examples:
- `"breakout": ["hostname", "dev=eno16695np0"]` — specific NIC
- `"breakout": ["hostname", "cpu=738,739"]` — multiple CPUs

**Important:** The `filter` parameter (e.g., `gt:0.03`) is
a different thing — it filters by metric VALUE (not by
breakout dimension) and only works with `resolution=1`.
Do not confuse the two:
- Breakout filter: `"breakout": ["cpu=738"]` — select
  which CPU to return
- Value filter: `"filter": ["gt:0.03"]` — exclude values
  below a threshold (resolution=1 only)

## Understanding per-CPU metric values

When `cpu` is in the breakout, metric values are **per that
single CPU**, NOT system-wide percentages:

- A Busy-CPU value of **0.48** means that CPU is at **48%**
  utilization, not 0.48%.
- A value of **0.73** means **73%** of that single CPU.
- A value of **1.0** means **100%** — fully saturated.

This is the most common misinterpretation. On a 768-CPU
system, system-wide Busy-CPU might be 0.86%, but individual
CPUs handling network traffic may be at 48-97%. Always check
per-CPU values — system-wide averages hide single-core
bottlenecks.

## Time-resolved per-CPU analysis

To investigate CPU behavior over time:

**Step 1:** Find active CPUs with resolution=1 and a filter:
```json
{
  "source": "procstat",
  "type": "Busy-CPU",
  "breakout": ["hostname", "cpu"],
  "filter": ["gt:0.05"]
}
```
This returns only CPUs above 5% utilization.

**Step 2:** For each busy CPU, query with resolution=30 to
see time variation:
```json
{
  "source": "procstat",
  "type": "Busy-CPU",
  "breakout": ["hostname", "cpu"],
  "resolution": 30,
  "filter": ["cpu=766"]
}
```
Look for: CPUs hitting 100% at any point, irqbalance
moving IRQ destinations mid-sample, saturation that
averages hide.

Note: filter and resolution may not work together in all
cases. If they don't, get the active CPU list first with
resolution=1, then re-query with resolution=30 for those
specific CPUs.

## Per-CPU and interrupt analysis

For network performance investigation, key query patterns:

**Per-CPU utilization:**
```json
{
  "source": "procstat",
  "type": "Busy-CPU",
  "breakout": ["hostname", "cpu"]
}
```

**Interrupt rate per CPU:**
```json
{
  "source": "procstat",
  "type": "interrupts-sec",
  "breakout": ["hostname", "irq", "cpu"]
}
```

**NUMA node mapping (via package breakout):**
```json
{
  "source": "procstat",
  "type": "interrupts-sec",
  "breakout": ["hostname", "package", "cpu"]
}
```
The `package` breakout maps to NUMA node / CPU socket.

## Workflow for comparative analysis

1. Get the run summary to find iteration IDs and
   primary-period-ids
2. Query one period with NO breakouts to see
   `remainingBreakouts`
3. Re-query with `breakout: ["csid"]` to split by pair
4. Map csid values to OS pairs using the run file's
   endpoint ID assignments
5. For CPU data, map tool instance numbers (sysstat-1,
   sysstat-2, etc.) to hosts using remote order in the
   run file
