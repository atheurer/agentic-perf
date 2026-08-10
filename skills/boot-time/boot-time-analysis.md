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

## Boot Time Measurement Model

Understanding how boot phases are measured is critical for
correct analysis. The tooling combines two independent time
sources:

### Time Sources

**Kernel monotonic clock** — starts at 0 when the kernel
begins executing. All systemd timestamps
(`KernelTimestampMonotonic`, `InitRDTimestampMonotonic`, etc.)
are on this clock. The `systemd-analyze` phases (kernel,
initrd, userspace) are durations between these timestamps.

**CNTVCT hardware counter** — an ARM counter that starts when
the SoC powers on. A service (`cntvct@basic.service`) reads
this counter at `basic.target` time and logs the value. The
tool compares this to the kernel monotonic timestamp at the
same point to derive the **wall-clock offset** — how much
time passed between power-on and kernel start (firmware,
bootloader, EFI).

### How Phases Are Calculated

```
Power on                    Kernel starts              InitRD starts
    |--- firmware/boot ----->|--- kernel phase -------->|--- initrd --->
    |                        |                          |
    |<-- CNTVCT offset ----->|                          |
    |   (wall-clock time     |<- KernelTimestamp        |
    |    before kernel)      |   Monotonic = 0          |
```

**SystemInit (sysinit):** The CNTVCT offset — wall-clock
time from power-on to kernel start. This is pre-kernel
firmware/bootloader time.

**Kernel phase:** `InitRDTimestampMonotonic -
KernelTimestampMonotonic`. This is pure kernel-clock
duration. The CNTVCT offset is added to the absolute
timestamps for wall-clock positioning but **cancels out
in the duration calculation**. A varying kernel phase
duration represents real variation in kernel execution
time, NOT a measurement artifact.

**InitRD phase:** `UserspaceTimestampMonotonic -
InitRDTimestampMonotonic`. Same principle — pure
kernel-clock duration.

**Switchroot phase:** Time from userspace start through
switch-root completion. Includes systemd unit startup.

**Total:** Sum of all phases from power-on to
multi-user.target.

### Detailed Boot Logs (boot_logs)

Each sample includes a `boot_logs` array with entries from
three sources, all normalized to wall-clock timestamps
(kernel monotonic + CNTVCT offset):

| Source | Content | Count |
|--------|---------|-------|
| `systemd-dbus` | Boot phase boundaries (Kernel, InitRD, Switchroot, etc.) | ~18 |
| `saplot` | Per-service start/activation times from `systemd-analyze plot` | ~1200 |
| `journalctl` | Kernel dmesg and system journal messages | ~1650 |

Each entry has:
- `activating` — wall-clock timestamp in microseconds
  (start of the event)
- `time` — duration in microseconds (0 for instantaneous
  log entries)
- `name` — the log message or service name
- `source` — which subsystem produced this entry

To analyze what happens within a phase, select `boot_logs`
entries whose `activating` falls between that phase's
boundaries. For example, kernel-phase entries have
`activating` between the Kernel and InitRD timestamps.

**Pre-timer kernel messages:** On ARM platforms, the kernel
timer is not active during very early boot. Messages
emitted before the timer starts (device tree parsing,
reserved memory setup, etc.) all have kernel monotonic
timestamp 0, meaning they all appear at exactly
`cntvct_offset` in wall-clock time. There is no timing
information available to order these messages relative
to each other — they are ordered by the kernel ring
buffer sequence, not by time. Approximately 250 messages
fall into this category. Analysis of kernel-phase timing
variance must focus on post-timer messages (those with
`activating > cntvct_offset`).

### Important for Analysis

- **Phase durations are kernel-clock measurements.** They
  represent real execution time between systemd timestamp
  pairs. When the CNTVCT service is available (normal
  case), the offset is applied to absolute timestamps
  for wall-clock positioning but cancels out in duration
  calculations. Variation in a phase duration means that
  phase genuinely took different amounts of time.

- **Check whether CNTVCT offset is present.** If
  `cntvct_offset_us` in the metadata is 0, the CNTVCT
  service was not available and all timestamps are in
  kernel-clock time only — sysinit will be 0 and
  phases will not include firmware/bootloader time.
  This is not the normal case but can occur on systems
  without the cntvct service installed.

- **The CNTVCT hardware counter is reliable.** It is a
  monotonic SoC counter that starts at power-on. Do not
  treat the offset as a source of error. If the offset
  varies between boots, the firmware/bootloader genuinely
  took different amounts of time before handing off to
  the kernel.

- **Correlation between phases matters.** If two phases
  show inverse variation (one increases while the other
  decreases by the same amount), investigate what shared
  resource or handoff boundary connects them. If the
  total across correlated phases is stable, the
  underlying work may be constant with only the
  measurement boundary shifting.

- **Distinguish measurement boundaries from execution
  time.** The split between phases depends on when
  systemd records each timestamp. A phase appearing
  variable may reflect variation in when a boundary
  event occurs, not variation in the work within that
  phase. Look at the total time across adjacent phases
  to determine whether actual work changed or just the
  boundary moved.

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
  scripts or manually SSH into the SUT to reboot it. The tool
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
