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

- **Pre-timer section** (~250 kernel messages at monotonic time 0):
  Device tree parsing, reserved memory setup, GIC init, early CPU
  init. On SA8775P this takes 0.5-0.8s and accounts for most
  kernel phase variance. Cannot be further broken down without
  the CNTVCT kernel module.
- **Post-timer section** (driver probes, IOMMU, SCMI, UFS, etc.):
  Typically stable (~0.2s ± 0.012s on SA8775P). If this varies,
  look at specific driver probe times.
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

- **Bimodal CNTVCT offset**: Cold boot shows two stable modes
  ~1s apart (~2.9s vs ~3.9s). This is firmware behavior, not
  kernel. Warm reboot has stable CNTVCT.
- **Kernel phase variance (warm reboot)**: 0.75-1.01s range,
  6.8% CoV. Entirely in pre-timer section (before kernel clock
  starts). Post-timer is stable at ~0.2s.
- **Journal timestamp inflation**: Boot log entries from
  `journalctl` source have inflated timestamps (~2.6s offset
  on SA8775P) due to journal daemon flush time. Being fixed
  in boot-time-analysis-tools 0.7.2.
