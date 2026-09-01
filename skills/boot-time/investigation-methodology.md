# Boot Time Investigation Methodology

## When to Use

This guide is for the analysis agent when investigating boot time
anomalies, regressions, or variance using existing data. Follow
these steps before concluding that new benchmark data is needed.

## Step 1: Understand the Alert Context

If this investigation was triggered by a Horreum alert:

- Check `anomaly_context` for the metric that triggered the alert
  (e.g., `satime.kernel`, `satime.initrd`, `satime.total`)
- Check `run_metadata` for the target board, OS version, and
  build that produced the alert
- The alert means Horreum's change detection already confirmed
  a statistically significant shift — do NOT re-confirm with
  new measurements. Investigate the cause.

If the ticket description or anomaly context references specific
run IDs from an external data source, query them directly:

- Call `get_run_info` with each run ID to retrieve the run's
  metadata (target, OS, build, timestamps)
- This gives you concrete data points to anchor the analysis
  rather than relying on aggregate statistics alone

## Step 2: Query Historical Data

Use Domain MCP tools to understand the anomaly:

1. **`get_key_metrics`** — retrieve the alerting metric's trend
   over recent runs. Look for step changes vs gradual drift.
2. **`get_baseline_stats`** — get the baseline mean, stddev,
   and sample count for the metric. Compare the alerting run's
   value against the baseline.
3. **`get_distribution`** — check if the metric is bimodal or
   has outliers. Boot time metrics on SA8775P are known to show
   bimodal distributions from firmware timing variation.
4. **`compare_run_to_baseline`** — quantify how far the alerting
   run deviates from baseline with z-scores.
5. **`get_run_info`** — retrieve build metadata (kernel version,
   OS version, image name) for the alerting run and compare with
   the baseline period.

## Step 3: Check Prior Investigations

Use `search_tickets` to find prior boot-time investigations on
the same board type. If a recent investigation already identified
the root cause for this metric on this board, reference it rather
than repeating the analysis.

Use `get_ticket_results` to read the findings from matched tickets.

## Step 4: Analyze Boot Time Phases

Boot time consists of distinct phases. Isolate which phase changed:

| Phase | Metric | What It Measures |
|---|---|---|
| Firmware/bootloader | `satime.sysinit` | CNTVCT offset — power-on to kernel start |
| Kernel | `satime.kernel` | Kernel init (DT parsing, driver probes) |
| InitRD | `satime.initrd` | Initramfs processing |
| Userspace | `satime.userspace` | Systemd unit startup to multi-user.target |
| Total | `satime.total` | Full boot time |

**Key relationships:**
- If `satime.kernel` varies but `satime.total` is stable,
  the variance may be a phase boundary shift, not real work change
- If `satime.sysinit` (CNTVCT offset) is bimodal (~1s step),
  this is firmware pre-boot timing variation, not kernel behavior
- Check whether adjacent phases have inverse correlation (one
  increases while the other decreases by the same amount)

## Step 5: Identify the Variance Source

For kernel phase variance specifically:

- **Pre-timer activity** (~250 kernel messages at monotonic time 0):
  Device tree parsing, reserved memory setup, GIC init, early CPU
  init. This is part of System Init, not `kernel_ms`. Cannot be
  broken down without the CNTVCT kernel module.
- **Post-timer / kernel phase** (`kernel_ms` from `systemd-analyze`):
  Driver probes, deferred probes, IOMMU, SCMI, UFS init, module
  loading. This is where `kernel_ms` variance originates. Use
  `initcall_debug` to attribute variance to specific subsystems.
- **Cold vs warm boot**: Power cycle (cold) produces different
  firmware behavior than SSH reboot (warm). Nightly CI uses warm
  reboot. If comparing data from different reboot methods, note
  the difference.

## Step 6: Determine Conclusiveness

Declare **conclusive** if you can answer:
- Which specific phase or subsystem changed?
- By how much (quantitative delta with confidence)?
- What likely caused it (kernel version change, config change,
  firmware update, measurement artifact)?

Declare **inconclusive** if:
- The available metrics don't break down to the subsystem level
- You need per-sample log data that isn't available via MCP
- The anomaly requires controlled A/B testing with specific
  images or configurations
- The metric change correlates with a build change but you
  can't determine causation from data alone

When declaring inconclusive, specify exactly what benchmark
parameters would resolve the question (board type, image version,
sample count, what to measure).

## Known Patterns on SA8775P

> **Skill reliability note:** Patterns documented here are based
> on analysis at a point in time and may be revised as new data
> emerges. Treat these as working hypotheses grounded in evidence,
> not irrefutable facts. If your data contradicts a documented
> pattern, investigate the discrepancy rather than dismissing
> your data.

- **Bimodal CNTVCT offset**: Cold boot shows two stable modes
  ~1s apart (~2.9s vs ~3.9s). This is firmware behavior, not
  kernel. Warm reboot has stable CNTVCT.
- **Kernel phase variance (warm reboot)**: 0.75-1.01s range,
  6.8% CoV. Root cause not yet confirmed.
- **Journal timestamp inflation**: Boot log entries from
  `journalctl` source have inflated timestamps (~2.6s offset
  on SA8775P) due to journal daemon flush time. Fixed in
  boot-time-analysis-tools 0.7.2 (uses `_SOURCE_MONOTONIC_TIMESTAMP`
  instead of `__MONOTONIC_TIMESTAMP`).

## Boot Phase Definitions and Measurement Windows

Understanding boot phases and their measurement boundaries is
critical for correct variance attribution. Misaligned phase
windows will produce misleading correlation results.

### Full Boot Timeline

```
Power On
  │
  ├─── Firmware (SoC ROM, PBL, SBL, UEFI) ───┐
  │                                             │
  │    Not visible in kernel/systemd logs.       │
  │    Can be observed via:                      │
  │    - Serial console output (SoC boot ROM     │
  │      emits microsecond timestamps for PBL,   │
  │      SBL, DDR init, UEFI phases)             │
  │    - CNTVCT kernel module can separate total  │
  │      firmware time from kernel pre-timer but  │
  │      cannot subdivide firmware phases         │
  │                                             │
  ├─── Kernel Pre-Timer ─────────────────────┐ │
  │    Kernel is executing but the monotonic  │ │ "System Init"
  │    clock has NOT started yet.             │ │ (CNTVCT service
  │    All dmesg messages show timestamp      │ │  measures this
  │    0.000000.                              │ │  entire window
  │    Includes: DT parsing, reserved memory, │ │  as one value)
  │    GIC init, early CPU init.              │ │
  │    ~210 messages on SA8775P.              │ │
  │                                           │ │
  ├─── Kernel Timer Start ────────────────────┘─┘
  │    Kernel monotonic clock initializes.
  │    Corresponds to KernelTimestampMonotonic
  │    in systemd. Note: the first dmesg entry
  │    with timestamp > 0 may appear AFTER this
  │    point — dmesg does not define the boundary.
  │
  ├─── Kernel Post-Timer ─────────────────────┐
  │    KernelTimestampMonotonic to             │
  │    InitRDTimestampMonotonic.               │
  │    Includes: driver probes, deferred      │ "Kernel Phase"
  │    probes, IOMMU init, UFS init,          │ (systemd-analyze
  │    module loading, initcall chain.         │  measures this)
  │    Ends when initrd starts.               │
  │                                           │
  ├─── InitRD / Initramfs ────────────────────┘
  │    initramfs (initrd) execution, root pivot.
  │    These terms are used interchangeably.
  │
  ├─── Userspace / Switchroot
  │    Root pivot from initrd, systemd target reach,
  │    services start. `systemd-analyze` calls this
  │    "userspace" (after switchroot).
  │
  └─── Boot Complete
```

### Key Measurement Points

Phase boundaries are defined by systemd timestamps, NOT by
dmesg log entries. `systemd-analyze` computes phase durations
from `KernelTimestampMonotonic`, `InitRDTimestampMonotonic`,
and `UserspaceTimestampMonotonic`. The first dmesg entry with
a timestamp > 0 is NOT the timer-start boundary — there can
be a gap between timer initialization and the first logged
message.

| Measurement | systemd-analyze field | Horreum labels | Tool output field | What it covers |
|---|---|---|---|---|
| **System Init** | N/A (CNTVCT derived) | `BOOT0 - SystemInit Duration *`, `boot.phase.system_init_ms` | `satime.sysinit` | Power-on to kernel monotonic clock initialization. The CNTVCT userspace service derives this by comparing the hardware counter to the kernel clock. Includes firmware + bootloader + kernel pre-timer. CNTVCT kernel module can separate firmware from kernel pre-timer; serial console can subdivide firmware phases. |
| **Kernel Phase** | `kernel` | `BOOT2 - Kernel Post-Timer Duration *`, `boot.phase.kernel_ms` | `satime.kernel`, `avg_kernel_s` | `KernelTimestampMonotonic` to `InitRDTimestampMonotonic`. ENTIRELY post-timer. Does NOT include pre-timer. |
| **InitRD / Initramfs** | `initrd` | `BOOT3 - Initrd Duration *`, `boot.phase.initrd_ms` | `satime.initrd`, `avg_initrd_s` | `InitRDTimestampMonotonic` to switchroot. Terms used interchangeably. |
| **Userspace / Switchroot** | `userspace` | `BOOT4 - Switchroot Duration *`, `boot.phase.switchroot_ms` | `satime.userspace`, `avg_userspace_s` | After switchroot to boot target. `systemd-analyze` labels this "userspace." |

### Critical Distinction: Kernel Phase vs Pre-Timer

**The kernel phase is defined by systemd timestamps, not
dmesg entries.** `systemd-analyze` computes it as
`InitRDTimestampMonotonic - KernelTimestampMonotonic`. This is
ENTIRELY post-timer — it does NOT include pre-timer activity.

The first dmesg entry with a monotonic timestamp > 0 is NOT
the same as `KernelTimestampMonotonic` — there can be a gap
between timer initialization and the first logged message.
Dmesg entries are useful for attributing events WITHIN the
kernel phase, but they do not define the phase boundaries.

The pre-timer phase is part of System Init, lumped together
with firmware and bootloader time. The only way to separate
pre-timer kernel time from firmware/bootloader time is with the
CNTVCT kernel module, which provides hardware counter timestamps
before the kernel clock starts.

### Common Misattribution Error

When analyzing kernel-phase CoV, ensure the measurement window
matches `systemd-analyze`'s kernel phase
(`InitRDTimestampMonotonic - KernelTimestampMonotonic`).

If an analysis defines “post-timer” using dmesg timestamps
instead of systemd timestamps, or uses only a subset of the
kernel phase window, it will produce incorrect phase
attributions.

### CNTVCT Instrumentation

The ARM CNTVCT (Counter-timer Virtual Count) is a hardware
counter that increments from power-on, independent of any
software clock. Two separate tools use it:

#### CNTVCT Userspace Service (standard)

A systemd service that runs in userspace after boot and reads
the current CNTVCT counter value. By comparing this to the
kernel monotonic clock, it calculates the **total time from
power-on to kernel timer start** — reported as `system_init_ms`.

This gives a single wall-clock duration for the entire
pre-kernel-timer window (firmware + bootloader + kernel
pre-timer combined). It CANNOT subdivide this window — it
only knows when the counter started (power-on) and when the
kernel clock started.

This service is included in standard nightly images.

#### CNTVCT Kernel Module (specialized)

A kernel module that reads the CNTVCT hardware counter at
multiple points DURING early kernel initialization, before
the monotonic clock starts. This provides discrete timestamps
for pre-timer kernel operations, enabling:

1. Separating firmware/bootloader time from kernel pre-timer
   time (previously lumped together in `system_init_ms`)
2. Subdividing pre-timer kernel work into discrete phases
   (DT parsing, reserved memory, GIC init, etc.)

This module is NOT in standard images — it requires a custom
kernel build.

#### Important

The CNTVCT hardware counter is a **measurement tool** — it
provides observability into time periods where no software
clock exists. It is NOT implicated as a root cause of boot
time variance. Do not confuse the counter itself with the
phenomena it measures.
