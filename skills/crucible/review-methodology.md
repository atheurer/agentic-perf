# Crucible Review Methodology — Network throughput and CDM API

## Network Benchmark Analysis (uperf, trafficgen, iperf, etc.)

Identify which host is the bottleneck — client or server? Query per-host CPU usage via cdm_api_request. The bottleneck host is the one with a saturated CPU core (not system-wide — see below).
- Look at **per-CPU utilization**, not system-wide averages. On a many-core system (e.g., 768 threads), a single saturated CPU handling all network interrupts is invisible in aggregate stats (appears as <1% system CPU). Use procstat/mpstat data broken out by CPU number.
- Check NIC-level metrics: packets/sec, bytes/sec, errors, drops.
- Check interrupt distribution — are IRQs for the test NIC spread across CPUs or pinned to one?
- Do NOT blame MTU when GSO/GRO is available. GSO/GRO enables the kernel to process large aggregated segments internally and only segment at the NIC. 1500B MTU with GSO/GRO should achieve far better than single-digit percent of line rate. Understand what GSO/GRO actually does before recommending MTU changes.

### Investigation methodology for network throughput

Follow this order unless the user directs otherwise:

1. **Find the bottleneck host** — compare CPU usage between client and server.
   The host with a CPU core at or near 100% is the bottleneck.
2. **Find the bottleneck CPU** — break down by individual CPU. Which core(s) are saturated? Are they handling interrupts, softirqs, or userspace?
3. **Check the NIC interrupt affinity** — is the test NIC's IRQ pinned to the saturated core? Are there better affinity options?
4. **Check TCP stack tuning** — buffer sizes (net.core.rmem_max, net.core.wmem_max, net.ipv4.tcp_rmem, net.ipv4.tcp_wmem), congestion control algorithm, GSO/GRO/TSO status on the interface.
5. **Check NUMA topology** — is the test NIC on the same NUMA node as the CPUs handling its traffic? Cross-NUMA memory access adds latency.
   - **Use host inventory first.** If the ticket includes a Host Inventory section, it contains the authoritative NUMA topology: node count, CPU-to-node mapping, and NIC-to-node mapping. Use this data.
   - If no inventory, call `query_numa_topology(host, iface)` to get the NIC's NUMA node and per-node CPU lists directly.
   - The `package` breakout in CDM procstat data maps to NUMA node / CPU socket. Use it to correlate interrupt-processing CPUs with NIC locality.
   - Do NOT assume NUMA node count. A system with 768 CPUs may have only 2 NUMA nodes. Read the actual count from inventory or sysfs.
   - Do NOT confuse CPU numbers with NUMA node numbers. CPU 511 is not on NUMA node 511 — look up which node owns that CPU.
6. **Measure actual transfer rate vs theoretical** — calculate what the current bottleneck allows and compare to what the link supports.

### Understanding per-CPU metric values

When `cpu` is in the CDM breakout, values are **per that single CPU**:
- A Busy-CPU value of 0.48 = **48%** of that CPU, NOT 0.48% system-wide
- A value of 0.73 = **73%** of that CPU
- A value of 1.0 = **100%** — fully saturated

System-wide averages hide single-core bottlenecks. On a 768-CPU system, system-wide Busy-CPU of 0.86% can mean individual CPUs are at 48-97%. Always report per-CPU values as percentages (multiply by 100 if needed).

When using sar-net, packet counts reflect **wire-level packets** which are always MTU-sized (~1500 bytes). These counts tell you NOTHING about GRO coalescing — GRO assembles packets into larger skb chains inside the kernel, after the NIC counters.

### Data-driven analysis — prove it, don't speculate

Every claim must be backed by queried data. If a tool can answer the question, call the tool — do not say "likely", "almost certainly", or "probably" when a CDM query or one of the host-query tools would give the answer.

Before concluding about:
- **NUMA locality** — query host inventory or call `query_numa_topology(host, iface)`
- **Interrupt affinity** — query procstat `interrupts-sec` with `hostname+irq+cpu` breakout, not assumptions about default behavior
- **GRO/GSO status** — call `get_ethtool_info(host, iface, mode="features")` and `get_ethtool_info(host, iface, mode="stats")`
- **TCP tuning** — call `get_sysctl_values(host, ["net.core.rmem_max", "net.core.wmem_max", "net.ipv4.tcp_rmem", "net.ipv4.tcp_wmem"])`

Present actual numbers in findings, not qualitative descriptions. "CPU 341 at 72% soft" is useful. "The CPU appears busy" is not.

To query host state when results are on a remote controller, first call `set_ssh_context` with the ticket ID to initialize SSH credentials, then use the appropriate host-query tool (`get_ethtool_info`, `get_sysctl_values`, `query_numa_topology`, `list_interfaces`, `read_remote_file`, `read_remote_dir`). Do NOT use SSH tools if local artifacts are available — if you already have a Local Artifacts section in your context, all data is accessible via `read_benchmark_artifact` without SSH.

### Using CDM API for metric queries

The CDM REST API on the controller (port 3000) provides per-host, per-CPU metrics collected during the benchmark. Use `cdm_api_request` to query:
- `/api/v1/iterations` — list iterations and their parameters
- `/api/v1/iterations/metric-values` — get metric data with breakouts
- Filter by metric source (mpstat, procstat, sar-net, uperf), metric type, and breakout fields to get specific per-CPU or per-interface data.

When the result set is large, use breakout filters to narrow to the specific host, CPU, or interface you need.

### CDM metric availability — check before querying

The `metrics` array returned by `get_run_summary` is the DEFINITIVE list of what is queryable via CDM. Each entry has a `source` and `types` array. Before making any CDM query:
1. Check if the source appears in the metrics array
2. If YES → query via cdm_api_requests
3. If NO → the data was not indexed into CDM. Use read_run_results to list and read the raw tool output files instead.

Do NOT query CDM for sources absent from the metrics list — they will return HTTP 500 errors and waste iterations.
