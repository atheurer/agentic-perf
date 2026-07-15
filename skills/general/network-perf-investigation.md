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

Use procstat or mpstat with `hostname+cpu` breakouts to get
per-CPU utilization. Look for:

- **sys (kernel) time** — TCP stack processing, socket operations
- **soft (softirq) time** — NIC NAPI polling, packet processing
- **irq (hardirq) time** — NIC interrupt handler

For single-stream TCP, expect one CPU dominated by sys (the
application's kernel receive/send path) and one CPU dominated
by soft (the NIC's RX/TX softirq processing).

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

Use procstat `interrupts-sec` with `hostname+irq+cpu` breakouts
to find which CPUs process NIC interrupts. Additionally use the
`package` breakout (which maps to NUMA node) to verify locality.

**Key checks:**
- Is the NIC on the same NUMA node as the IRQ-processing CPUs?
  Check: `cat /sys/class/net/<iface>/device/numa_node`
- Is the application process on the same NUMA node as the NIC?
  For single-thread tests, the process CPU is the one with all
  the sys time — no need to hunt for the PID.
- Cross-NUMA traffic (NIC on one node, application on another)
  adds significant latency on AMD EPYC (Infinity Fabric) and
  Intel Xeon (UPI) platforms.

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

Use `cpu-pin: numa` to eliminate cross-NUMA overhead when
investigating single-stream TCP performance. Compare results
with and without pinning to quantify the NUMA penalty.
