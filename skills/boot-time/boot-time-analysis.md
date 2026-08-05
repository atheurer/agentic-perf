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

## Reboot Behavior

The harness supports multiple reboot methods depending on the
environment:

### Jumpstarter boards (with `--jumpstarter-serial`)

Reboot cycles use explicit Jumpstarter power control:
`j power off` → configurable delay → `j power on`. This is
more reliable than `j power cycle` or SSH-initiated reboots
on embedded boards.

The `--power-off-delay` parameter controls the wait between
power off and power on (default: 2 seconds). Boards that need
more settling time can increase this.

Serial output is captured during each boot cycle for detailed
timing analysis. The `capture-boot` helper handles serial
capture independently from power control (`--no-power` mode).

### SSH reboot with Jumpstarter fallback

When Jumpstarter is available but serial capture is not
enabled, the harness attempts SSH reboot first. If the SSH
reboot hangs (board doesn't go down within 120 seconds), the
harness falls back to Jumpstarter power cycling automatically.

Samples that required power cycle fallback are tracked:
- `power_cycle_fallbacks` count in `collection_status.json`
- Exit code 2 when any fallbacks occurred
- Affected samples listed in the run summary

### Standard SSH reboot (no Jumpstarter)

Uses `reboot` command via SSH. Requires the SUT to have a
stable IP across reboots.

## Provisioning Scope

The boot-time harness has NO provisioning step. The
`execute_boot_time_test` tool automatically installs
`boot-time-analysis-tools` on the SUT via SSH before running.
The provisioning agent should only ensure the board is flashed
and SSH-reachable — do NOT tell provisioning to install any
boot-time packages.

## SSH Connectivity

The `execute_boot_time_test` tool handles its own SSH connectivity.
It uses `sshpass` with password-based authentication (default
password: "password"), NOT SSH key-based authentication. Set
`ssh_password` in the ticket's custom_fields to override the
default.

**Do NOT** perform manual SSH checks (`check_host`, `check_hosts`,
or `set_ssh_context`) before calling the tool — they use key-based
SSH which will fail on boards provisioned with password auth.
The tool waits up to 60 seconds for the SUT to become SSH-reachable
before starting.

**Note:** The boot-time harness currently requires password-based
SSH access. Targets that only support key-based SSH auth are not
yet supported.

## What the Tool Does NOT Do

- **No analysis** — submit the raw KPIs; analysis belongs in the
  evaluate agent
- **No comparison** — do not compare results to baselines or prior runs
- **No diagnosis** — do not investigate why boot time is slow
- **No parameter tuning** — use the defaults unless the ticket
  explicitly requests different parameters
- **No improvisation** — if `execute_boot_time_test` fails, report
  the error and request clarification. Do NOT write your own reboot
  scripts, manually SSH into the SUT to reboot it, or attempt to
  replicate the tool's behavior with execute_command. The tool
  handles all reboot orchestration, timing collection, and result
  merging — manual alternatives will produce incompatible output.
- **One host per execution** — run `execute_boot_time_test` once
  against the assigned SUT, then submit your result. If the
  investigation requires testing multiple hosts, the system
  handles iteration automatically via loop-back — do NOT call
  the tool multiple times for different hosts in a single run.

## Common KPI Patterns

For RHIVOS / Automotive Linux targets:

```
NetworkManager|end0|eth0|systemd-modules-load|udev|dbus-broker.service|remote-fs.target|SELinux
```

For bootc/ostree targets, also include:

```
ostree-prepare-root|ostree-remount|initrd-switch-root
```

These services are bootc-specific:
- `ostree-prepare-root.service` — composefs erofs mount + sealing
- `ostree-remount.service` — OSTree bind mounts in real root
- `initrd-switch-root.service` — root pivot

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | All samples collected successfully |
| 2 | Partial success — samples collected but with issues (lease expiry, power cycle fallbacks) |
| 1 | Total failure — no usable samples collected |

Exit code 2 (partial) still produces usable results. The response
includes `samples_requested`, `samples_collected`, and
`power_cycle_fallbacks` for context. Power cycle fallback samples
may have slightly different timing characteristics than clean
SSH reboots — note this in the analysis if the count is high.
