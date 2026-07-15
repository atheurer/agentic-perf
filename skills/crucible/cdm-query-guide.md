# CDM Query Guide — Breakouts and Per-Pair Analysis

## Discovering available breakouts

When querying metric data from the CDM API, the response
includes a `remainingBreakouts` field listing all breakout
dimensions you can use to split the data further. Always
query WITHOUT breakouts first, then inspect
`remainingBreakouts` to see what's available before
re-querying with the appropriate breakout.

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
