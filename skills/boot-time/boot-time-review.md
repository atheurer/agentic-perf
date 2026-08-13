# Boot Time Review Notes

## Result Location

Boot-time test results are stored on the **orchestrator host**,
not the SUT. The SUT's `/tmp` is cleared on every reboot cycle,
so searching the SUT filesystem for results will find nothing.

The benchmark agent's completion comment contains all KPIs:
- `avg_total_boot_s`, `avg_kernel_s`, `avg_initrd_s`, `avg_userspace_s`
- `sample_count`, `samples_collected`
- `output_dir` (local path on orchestrator)

If `output_dir` is set on the ticket, use `list_benchmark_artifacts`
and `read_benchmark_artifact` to access the merged results JSON.
If not, rely on the KPIs from the benchmark completion comment —
they contain the same data.

Do NOT search the SUT's `/tmp` or `/var/log` for boot-time results.
They will not be there.

## Available Artifacts

The output directory contains per-sample result files:

| File | Content |
|---|---|
| `merged-results.json` | All samples merged with averaged KPIs |
| `all_summary.json` | Per-sample phase durations and metadata |
| `*-boot_time_logs.json` | **Per-sample boot logs** — normalized timing entries from systemd-dbus, journalctl, and saplot |
| `*-boot_time_summary.json` | Per-sample KPI summary |
| `collection_status.json` | Collection status and error counts |
| `metadata.json` | System metadata (kernel, architecture, etc.) |

## Root Cause Analysis with Boot Logs

When you identify variance in a specific boot phase, **read the
per-sample boot log files** (`*-boot_time_logs.json`) to determine
which services or drivers are causing the variance. This is how
you move from phase-level attribution to root cause identification.

### Boot Log Structure

Each `*-boot_time_logs.json` file contains a `boot_logs` array
with entries from three sources, all normalized to wall-clock
timestamps (microseconds):

| Source | Content |
|---|---|
| `systemd-dbus` | Boot phase boundaries (Kernel, InitRD, Switchroot) |
| `saplot` | Per-service start/activation times from `systemd-analyze plot` |
| `journalctl` | Kernel dmesg and system journal messages |

Each entry has:
- `activating` — wall-clock timestamp in microseconds (start)
- `time` — duration in microseconds
- `name` — service name or log message
- `source` — which subsystem produced this entry

### How to Use Boot Logs for RCA

1. **Identify fast and slow samples** from `all_summary.json`
   by comparing per-sample phase durations

2. **Read boot logs from one fast and one slow sample** using
   `read_benchmark_artifact`

3. **Filter entries to the variant phase** — select entries
   whose `activating` falls between that phase's boundary
   timestamps (from systemd-dbus entries)

4. **Compare service durations** between fast and slow samples:
   - Look at `saplot` entries (per-service timing) for the
     variant phase
   - Find services with significantly different `time` values
   - These are your root cause candidates

5. **Check journal entries** for error messages, timeouts, or
   retries that appear in slow samples but not fast ones

### Example: Switchroot Variance

If switchroot varies between fast and slow boots:

1. Find the Switchroot phase boundaries from systemd-dbus entries
2. Filter ALL entries within that phase (saplot for service
   timing, journalctl for kernel/system messages, systemd-dbus
   for sub-phase boundaries)
3. Compare service durations (saplot `time` field) between
   fast and slow samples — look for a service that takes
   significantly longer in slow boots
4. Check journalctl entries for errors, timeouts, retries,
   or warnings that appear only in slow samples
5. Common suspects: network services (DHCP timeout), storage
   mounts (slow device detection), container startup, SELinux
   relabeling

### Important Notes

- **Pre-timer kernel messages** (~250 entries at the start) all
  have `activating` equal to the CNTVCT offset. They cannot be
  ordered relative to each other — only post-timer entries have
  meaningful timestamps.
- **Boot logs can be large** (2000+ entries per sample). Use
  the `offset` and `limit` parameters on `read_benchmark_artifact`
  to paginate through them.
- **Compare specific samples**, not averages. Pick the fastest
  and slowest samples for comparison to maximize the signal.
