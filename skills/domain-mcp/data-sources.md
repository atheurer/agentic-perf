# Domain MCP Data Sources

## Dataset Types

The Domain MCP organizes historical data by dataset type.
When querying baselines or searching for anomalies, you must
specify the correct dataset type.

### `boot-time-verbose`

Boot timing measurements — kernel, initrd, userspace,
system init, and total boot time. Used for boot-time
investigations and harness results.

### `rhivos-perf-comprehensive`

**All non-boot-time performance data.** This is the primary
dataset type for any performance metric that is not boot
timing. It includes data from multiple workload tools in
each run — the set of tools is not static and evolves with
the CI pipeline.

Known workload categories (not exhaustive):
- **Storage I/O**: fio (IOPS, bandwidth, latency)
- **CPU stress**: stress-ng (bogo ops, timerlat)
- **CPU benchmark**: CoreMark-PRO (single/multi core)
- **Automotive benchmark**: autobench (scaling ratios)
- **Network/comms**: rusty-comms, uperf
- **Autonomous driving**: autoware stack

Each nightly CI run produces one dataset containing results
from ALL workloads run in that pass. Data from different
tools is distinguished by metric name prefix (e.g.,
`perf.fio.*`, `perf.stressng.*`, `perf.coremark.*`).

## Query Strategy

When investigating performance data:

1. **Boot time questions** → use `boot-time-verbose`
2. **Everything else** → use `rhivos-perf-comprehensive`

Do NOT try to guess dataset type names from the benchmark
tool name. For example:
- ❌ `fio`, `arcaflow-fio`, `fio-storage` — these don't exist
- ✅ `rhivos-perf-comprehensive` — contains fio data

If a query returns no data with `rhivos-perf-comprehensive`,
the metric may not be collected in the nightly CI pipeline.
Report this as "no historical data available" rather than
trying other dataset type names.

## Metric Names

Metrics follow the pattern `perf.<tool>.<measurement>`:
- `perf.fio.read.iops` — FIO read IOPS
- `perf.fio.write.iops` — FIO write IOPS
- `perf.fio.read.lat_p99_ns` — FIO read p99 latency
- `perf.stressng.bogo_ops_per_sec_real` — stress-ng throughput
- `perf.timerlat.irq.max_us` — max IRQ latency
- `perf.coremark.scaling` — CoreMark-PRO scaling ratio
- `perf.autobench.scaling` — autobench scaling ratio

Boot time metrics use a different prefix:
- `boot.time.total_ms`
- `boot.phase.kernel_ms`
- `boot.phase.initrd_ms`

When you don't know the exact metric name, query
`get_baseline_stats` with just the target and dataset type
— it returns all available metrics.
