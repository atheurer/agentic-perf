# Crucible Tool-Params Reference

Tools are system profilers that collect data during a benchmark run.
They are configured in the `tool-params` array of the run file.

## Discovering Tool Parameters

You can discover valid parameters, presets, and validations for tools using:
1. **MCP Tool**: Call `get_tool_params(tool)` (e.g., `get_tool_params("sysstat")`).
2. **Filesystem**: On the Crucible controller, tool parameter definitions are stored in `/opt/crucible/subprojects/tools/<tool>/multiplex.json` and metadata in `tool-metadata.json`.

Like benchmarks, tool `multiplex.json` files define:
- `presets.defaults`: Default argument values applied if not specified.
- `validations`: Regex constraints and descriptions for valid argument values.

## Available Tools

| Tool | Description | Configurable Arguments | CDM indexed? |
|---|---|---|---|
| sysstat | Wrapper for sar, mpstat, iostat, pidstat | `subtools`, `interval` | Yes (mpstat, sar-*, iostat, pidstat) |
| procstat | /proc-based CPU, interrupt, memory stats | `files`, `interval` | Yes (procstat source) |
| ethtool | Per-queue and aggregate NIC statistics | `interfaces`, `interval` | Yes (ethtool source) |
| forkstat | Process lifecycle event monitoring | `events` | Yes (forkstat source) |
| kernel | Umbrella for turbostat, perf, etc. | `subtools` | Depends on subtool |
| bpf | eBPF-based profilers (tcp-window, etc.) | `subtools` | Yes (bpf source) |

**Always include at minimum:**
```json
"tool-params": [
  {"tool": "sysstat"},
  {"tool": "procstat"}
]
```

## Tool Parameter Details

### sysstat
- **`subtools`**: Comma-separated combination of `mpstat`, `sar`, `iostat`, `pidstat`.
  - Default: `"mpstat,sar,iostat,pidstat"`
  - Validation: `^(mpstat|sar|iostat|pidstat)(,(mpstat|sar|iostat|pidstat))*$`
- **`interval`**: Sampling interval in seconds (positive integer).
  - Default: `"3"`

```json
{
  "tool": "sysstat",
  "params": [
    {"arg": "subtools", "val": "mpstat,sar"},
    {"arg": "interval", "val": "1"}
  ]
}
```

### procstat
- **`files`**: Comma-separated list of relative `/proc` file paths to snapshot.
  - Default: `"interrupts,vmstat,slabinfo,softirqs,meminfo,schedstat,net/softnet_stat"`
- **`interval`**: Sampling interval in seconds (positive integer).
  - Default: `"3"`

```json
{
  "tool": "procstat",
  "params": [
    {"arg": "files", "val": "interrupts,vmstat,net/softnet_stat"},
    {"arg": "interval", "val": "1"}
  ]
}
```

### ethtool
- **`interfaces`**: Comma-separated list of network interface names to monitor (e.g. `"eth0,eth1"`). Omit to auto-detect all "up" interfaces except loopback.
- **`interval`**: Sampling interval in seconds (positive integer).
  - Default: `"3"`

```json
{
  "tool": "ethtool",
  "params": [
    {"arg": "interfaces", "val": "eth0,eth1"},
    {"arg": "interval", "val": "2"}
  ]
}
```

### forkstat
- **`events`**: Comma-separated list of forkstat event types to monitor (`fork`, `exec`, `exit`, `core`, `comm`, `clone`, `ptrce`, `uid`, `sid`, `nonzeroexit`), or `"all"`.
  - Default: `"all"`

```json
{
  "tool": "forkstat",
  "params": [
    {"arg": "events", "val": "fork,exec,exit"}
  ]
}
```

### kernel
The `kernel` tool is an umbrella — it does nothing by itself. You MUST specify subtools via the `subtools` parameter.

| Subtool | Description | CDM indexed? |
|---|---|---|
| turbostat | CPU frequency, C-states, power, IPC | Yes (turbostat source) |
| perf | perf stat counters (IPC, cache misses) | Yes (perf-stat source) |
| speed-select-util | Intel SST-PP frequency monitoring | Raw files only |
| trace-cmd | ftrace-based kernel tracing | Raw files only |
| sysfs-trace | sysfs attribute polling | Raw files only |

**turbostat example:**
```json
{
  "tool": "kernel",
  "params": [
    {"arg": "subtools", "val": "turbostat"}
  ]
}
```

**Multiple kernel subtools:**
```json
{
  "tool": "kernel",
  "params": [
    {"arg": "subtools", "val": "turbostat,perf"}
  ]
}
```

### bpf
| Subtool | Description | CDM indexed? |
|---|---|---|
| tcp-window | TCP cwnd/rwnd/swnd tracking | Yes |

```json
{
  "tool": "bpf",
  "params": [
    {"arg": "subtools", "val": "tcp-window"}
  ]
}
```

## tool-params Format

Parameters use `params: [{arg, val}]` — each entry becomes `--arg val`
passed to the tool's start script. Do NOT invent custom top-level fields.

**CORRECT:**
```json
{"tool": "kernel", "params": [{"arg": "subtools", "val": "turbostat"}]}
{"tool": "sysstat", "params": [{"arg": "interval", "val": "1"}]}
```

**WRONG (silently ignored by crucible):**
```json
{"tool": "kernel", "subtool": "turbostat"}
{"tool": "kernel", "subtools": "turbostat"}
{"tool": "sysstat", "interval": "1"}
```

See `run-file-pitfalls.md` for more wrong-format examples.

## CDM Metric Availability

Before querying CDM for tool data, check the `metrics` array from
`get_run_summary`. If a source is absent, the tool either wasn't
configured or its post-processor didn't run.

| CDM source | Produced by | Common metric types |
|---|---|---|
| mpstat | sysstat (subtool mpstat) | Busy-CPU (per-CPU utilization) |
| sar-mem | sysstat (subtool sar) | Memory-Used-Percent, Page-faults-sec |
| sar-net | sysstat (subtool sar) | L2-Gbps, packets-sec, errors-sec |
| iostat | sysstat (subtool iostat) | operations-sec, kB-sec, percent-utilization |
| pidstat | sysstat (subtool pidstat) | Busy-CPU, NonBusy-CPU |
| procstat | procstat | interrupts-sec, context-switches-sec, cpu-migrations-sec |
| ethtool | ethtool | `<counter-name>-sec` |
| forkstat | forkstat | fork, exec, exit, clone |
| turbostat | kernel (subtool turbostat) | cpu-busy-pct, cpu-freq-avg-mhz, package-power-watt, ipc |
| perf-stat | kernel (subtool perf) | ipc, cache-misses |
| bpf | bpf (subtool tcp-window) | tcp-window metrics (cwnd, rwnd, swnd) |

If a source is NOT in the `metrics` array, use `read_run_results` to
access raw files under the run's `tool-data/` directory instead.
