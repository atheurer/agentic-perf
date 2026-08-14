# Network Performance Investigation Methodology

## Step 1: Identify the bottleneck host

For any client-server network benchmark, the first question is always:
which host is the bottleneck?

- Query per-host CPU usage via CDM (mpstat or procstat source,
  Busy-CPU metric type, with `hostname` breakout).
- **Do NOT use system-wide averages.** On many-core systems (e.g.,
  768 threads), a single saturated CPU handling all network traffic
  is invisible in aggregate stats (appears as <1% system CPU).
- The bottleneck host is the one with a saturated CPU core.

## Step 2: Find the bottleneck CPU

Use mpstat with `hostname,num` breakout to get per-CPU utilization.
**The mpstat CPU number breakout dimension is called `num`, NOT `cpu`.**
Using `cpu` with mpstat returns no breakout data — the query silently
aggregates and the per-CPU information is lost.

**Two-step pattern — always start with ranking, then drill into type:**

Step 1 — rank active CPUs (do this first):
```
crucible get metric --period <ID> --source mpstat --type Busy-CPU \
  --breakout hostname,num
```

Step 2 — for the hot CPUs only, add `type` to see work breakdown:
```
crucible get metric --period <ID> --source mpstat --type Busy-CPU \
  --breakout hostname,num,type --filter 'num=<hot_cpu>'
```

Adding `type` upfront splits each CPU into 4+ rows and makes it hard
to spot the busiest cores. Always rank first.

**Work types to look for (Step 2):**
- **usr** — user-space application (uperf, benchmark process)
- **sys (kernel) time** — TCP stack processing, socket operations
- **soft (softirq) time** — NIC NAPI polling, packet processing
- **irq (hardirq) time** — NIC interrupt handler

**Note:** procstat uses `cpu` as its breakout dimension — that is
correct for procstat. Only mpstat uses `num`.

For single-stream TCP, expect one CPU dominated by sys (the
application's kernel receive/send path) and one CPU dominated
by soft (the NIC's RX/TX softirq processing).

**Available mpstat topology breakouts:** `package`, `die`, `core`,
`thread`, `num`. A `node` (NUMA node) breakout does not yet exist in
CDM — map CPU numbers to NUMA nodes via host inventory or sysfs
(`/sys/devices/system/node/nodeN/cpulist`). Tracked in
perftool-incubator/tool-sysstat#68.

## Step 3: Time-resolved CPU analysis

Averages over the full test period can hide saturation. A CPU
at 85% average may have been at 100% for most of the test with
a brief period of 0% at the end (test teardown).

Use the CDM `resolution` parameter to get per-CPU usage over
time:
- resolution = number of data points you want in the time period
- For a 30-second test, resolution=10 gives ~3 second intervals
- Sample collection interval is rarely under 3 seconds, so
  resolution=10 is sufficient for a 30s test
- Look for CPUs hitting 100% at any point during the test
- Check if irqbalance moved IRQ destinations between time windows

## Identifying the correct NIC from CDM data

Systems often have multiple physical NICs from different vendors
(e.g., ConnectX-7 400G, ConnectX-6 100G, BCM57504 25G all in
the same server). Do NOT assume based on interface name alone.

**To identify the test NIC in CDM data:**
1. Query sar-net L2-Gbps with `hostname+dev` breakout
2. The interface with throughput matching the benchmark result
   (e.g., ~30 Gbps for a 30 Gbps uperf test) is the test NIC
3. Once identified, use that interface name to find its IRQs
   in procstat data

**Do NOT confuse NICs.** Different NICs may be on different
NUMA nodes. Finding IRQs for the wrong NIC leads to incorrect
NUMA locality conclusions.

## Step 4: IRQ destination and NUMA locality

Use procstat `interrupts-sec` with `hostname,irq,cpu` breakouts
to find which CPUs process NIC interrupts. Note: for **procstat**,
the CPU breakout dimension is `cpu`; for **mpstat**, it is `num`.
Additionally use the `package` breakout (which maps to socket/NUMA
node) to verify locality.

**Key checks:**
- Is the NIC on the same NUMA node as the IRQ-processing CPUs?
  Check: `cat /sys/class/net/<iface>/device/numa_node`
- Is the application process on the same NUMA node as the NIC?
  Find the CPU with high `usr` time (mpstat `hostname,num` breakout)
  and verify it falls in the NIC's NUMA node CPU range.
- Cross-NUMA traffic (NIC on one node, application on another)
  adds significant latency on AMD EPYC (Infinity Fabric) and
  Intel Xeon (UPI) platforms.

**NUMA locality assessment — always check these three things:**

1. NIC NUMA node: `cat /sys/class/net/<iface>/device/numa_node`
2. IRQ-processing CPUs: procstat `interrupts-sec` with
   `hostname,irq,cpu` breakout — confirm CPUs match the NIC's node
3. Application CPUs: mpstat `Busy-CPU` with `hostname,num` breakout —
   CPU(s) with high `usr` time are where the benchmark runs; confirm
   they are in the NIC's NUMA node range

**Tool overhead caveat:** Tools like bpftrace (used by the `bpf`
subtool) run unpinned and may land on NUMA-0 CPUs even when the
benchmark is correctly pinned to another NUMA node. At high packet
rates (>100G), bpftrace aggregation threads can saturate a single
core. If a NUMA-0 CPU is at 100% but pidstat shows it is `bpftrace`
(not the benchmark binary), tool overhead is the cause — not a
cross-NUMA placement bug. Tracked in perftool-incubator/tool-bpf#6.

**irqbalance migration:**
- If IRQ destinations differ between samples, irqbalance is
  active and migrating NIC IRQs.
- This causes sample-to-sample throughput variation.
- Throughput differences between samples often correlate with
  how well irqbalance placed IRQs relative to the NIC's NUMA
  node and the application thread.

## Step 5: Verify GRO/GSO effectiveness

**Do NOT infer GRO status from wire packet sizes.** Packets on
the wire are always limited by MTU (typically 1500 bytes). GRO
works by assembling wire-level packets into larger data
structures (sk_buff chains) inside the kernel. The packet rate
from sar-net reflects wire packets, not GRO segments.

To verify GRO is actually working:
1. `ethtool -k <iface> | grep generic-receive-offload` — check
   if the feature is enabled (necessary but not sufficient)
2. `ethtool -S <iface> | grep gro` — check driver-level GRO
   counters. If `rx_gro_packets` is 0, GRO may not be
   coalescing despite being "enabled".
3. For definitive proof, measure actual skb sizes seen by the
   TCP stack (requires bpftrace or similar eBPF tooling to
   trace `napi_gro_receive` or `tcp_recvmsg` and histogram
   the segment lengths).

**Do NOT blame MTU when GRO/GSO is available.** GSO/GRO enables
the kernel to process large aggregated segments internally and
only segment at the NIC. 1500B MTU with working GRO should
achieve far better than single-digit percent of line rate.

## CPU pinning for crucible uperf

The uperf benchmark in crucible supports a `cpu-pin` parameter:
- `cpu-pin: numa` — pin the uperf process to the NUMA node of
  the test NIC (recommended for single-stream tests)
- `cpu-pin: cpu:192-383` — pin to a specific CPU range

**CRITICAL: params default to role=client only.** If you do
not specify `"role": "server"`, the server uperf process will
NOT be pinned. For single-stream TCP tests, the server is
typically the bottleneck — pinning only the client is useless.
Always specify cpu-pin for BOTH roles:

```json
{"arg": "cpu-pin", "vals": ["numa"], "role": "client"},
{"arg": "cpu-pin", "vals": ["numa"], "role": "server"}
```

Use `cpu-pin: numa` to eliminate cross-NUMA overhead when
investigating single-stream TCP performance. Compare results
with and without pinning to quantify the NUMA penalty.

## When no CPU hits 100%

If single-stream throughput is lower than expected but no CPU
is near 100%, the bottleneck may not be where you think:

- **Check both client AND server** — the client may be the
  bottleneck, not just the server
- **Scheduler migration** — CFS may be moving the process
  between CPUs mid-test, causing cache cold-starts. Use
  time-resolved per-CPU data (resolution=30) to detect this.
- **External factors** — flaky physical link, switch in the
  path, NIC firmware issue, RX ring drops
- **rx_out_of_buffer drops** — check `ethtool -S` for ring
  exhaustion forcing TCP retransmits
