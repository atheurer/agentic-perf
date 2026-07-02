# Boot Time Analysis

## Overview

Boot time analysis measures how long a Linux system takes to boot by
performing multiple reboot cycles and collecting timing data from
`systemd-analyze`, `dmesg`, and kernel clock tick counters. Results
are collected per-sample and merged into a single structured JSON
document suitable for trend analysis.

## Tool

Use the `execute_boot_time_test` tool. It handles:

1. Installing `boot-time-analysis-tools` on the SUT (idempotent)
2. Running the reboot cycles and collecting timing data
3. Merging per-sample results into aggregated KPIs

**NEVER** run this against localhost or the orchestrator host — the
tool reboots the target.

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `sut_host` | (required) | IP address of the System Under Test |
| `samples` | 50 | Number of reboot cycles. More samples = better statistical confidence |
| `kpi_pattern` | "" | Regex for log lines to highlight as KPIs |
| `clean_journal` | false | Delete systemd journal before each reboot |
| `description` | "" | Human-readable test description |

### Sample Count Guidelines

- **Quick validation:** 5–10 samples
- **Standard regression test:** 50 samples (default)
- **High-confidence analysis:** 100+ samples

## Output KPIs

The tool returns averaged timing metrics across all samples:

| KPI | Unit | Description |
|-----|------|-------------|
| `avg_total_boot_s` | seconds | Total boot time (kernel + initrd + userspace) |
| `avg_kernel_s` | seconds | Kernel initialization time |
| `avg_initrd_s` | seconds | initramfs processing time |
| `avg_userspace_s` | seconds | Userspace startup time |
| `sample_count` | count | Number of samples successfully collected |

## What the Tool Does NOT Do

- **No analysis** — submit the raw KPIs; analysis belongs in the
  evaluate agent
- **No comparison** — do not compare results to baselines or prior runs
- **No diagnosis** — do not investigate why boot time is slow
- **No parameter tuning** — use the defaults unless the ticket
  explicitly requests different parameters

## Common KPI Patterns

For RHIVOS / Automotive Linux targets:

```
NetworkManager|end0|eth0|systemd-modules-load|udev|dbus-broker.service|remote-fs.target|SELinux
```

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | All samples collected successfully |
| 2 | Partial success — some samples collected before a failure |
| 1 | Total failure — no samples collected |

Exit code 2 (partial) still produces usable results. The response
includes `samples_requested` and `samples_collected` for context.
