# Crucible Tool-Params Reference

Tools are system profilers that collect data during a benchmark run.
They are configured in the `tool-params` array of the run file.

## Available Tools

| Tool | Description | CDM indexed? |
|---|---|---|
| sysstat | Wrapper for sar, mpstat, iostat, pidstat | Yes (mpstat source) |
| procstat | /proc-based CPU, interrupt, memory stats | Yes (procstat source) |
| kernel | Umbrella for turbostat, perf, sysfs-trace, etc. | Depends on subtool |
| bpf | eBPF-based profilers (tcp-window, etc.) | Yes (bpf source) |

**Always include at minimum:**
```json
"tool-params": [
  {"tool": "sysstat"},
  {"tool": "procstat"}
]
```

## Tools With Subtools

### kernel tool

The `kernel` tool is an umbrella — it does nothing by itself. You MUST
specify subtools via the `--subtools` parameter.

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

### bpf tool

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
```

**WRONG (silently ignored by crucible):**
```json
{"tool": "kernel", "subtool": "turbostat"}
{"tool": "kernel", "subtools": "turbostat"}
```

See `run-file-pitfalls.md` for more wrong-format examples.

## CDM Metric Availability

Before querying CDM for tool data, check the `metrics` array from
`get_run_summary`. If a source is absent, the tool either wasn't
configured or its post-processor didn't run.

| CDM source | Produced by | Common metric types |
|---|---|---|
| mpstat | sysstat | Busy-CPU (per-CPU utilization) |
| procstat | procstat | interrupts-sec, context-switches-sec, cpu-migrations-sec |
| sar-net | sysstat | packets-sec, bytes-sec per interface |
| turbostat | kernel (subtool turbostat) | cpu-busy-pct, cpu-freq-avg-mhz, package-power-watt, ipc |
| perf-stat | kernel (subtool perf) | ipc, cache-misses |
| bpf | bpf | tcp-window metrics (cwnd, rwnd, swnd) |

If a source is NOT in the `metrics` array, use `read_run_results` to
access raw files under the run's `tool-data/` directory instead.
