# Host Tuning for Network Benchmarks

Host tuning for network performance benchmarks covers three distinct domains.
They interact in a specific order and must be applied correctly together.

## The Three Domains

### 1. NIC Tuning (`tune_nic`)
Settings scoped to a specific network interface via ethtool:
- **Channel/queue count** — how many combined queues the NIC exposes
- **Ring buffer sizes** — how many descriptors per TX/RX ring
- **Offload flags** — GRO, GSO, LRO, etc.

**Critical ordering constraint:** `tune_nic` MUST be called before `pin_irq`.
Changing the channel count changes which IRQ numbers the NIC has. If you pin
an IRQ and then change the channel count, the pin is on the wrong IRQ.

Typical value for single-stream benchmarks: `channels=1` (one combined queue,
one IRQ to pin).

### 2. TCP/Network Stack Tuning (`tune_tcp`)
TCP stack settings — sysctl values plus direct interface qdisc via `tc`.

**BBR + fq are a matched pair.** BBR relies on the Fair Queue (`fq`) qdisc
for per-flow pacing. Without `fq`, BBR cannot control its send rate properly
and throughput suffers. `fq_codel` is a different algorithm focused on latency
management and is NOT a substitute for `fq` with BBR.

**Critical gotcha:** `sysctl -w net.core.default_qdisc=fq` only affects
interfaces created AFTER the change. For a NIC that is already up (e.g.
`eno16695np0`), you must also run `tc qdisc replace dev <interface> root fq`
to change the qdisc on the live interface. Always pass `interface` to
`tune_tcp` — it handles both the sysctl and the `tc` command.

Always set both together, with the interface:
```python
tune_tcp(host, interface="eno16695np0", congestion_control="bbr", qdisc="fq")
```

Optionally set socket buffer sizes — RHEL defaults (~208KB) may limit
throughput on high-bandwidth paths. Only set these if the user explicitly
requests a specific value; do not guess at a value without data:
```python
tune_tcp(host, interface="eno16695np0", congestion_control="bbr", qdisc="fq",
         rmem_max=<user_specified>, wmem_max=<user_specified>)
```

### 3. IRQ Pinning (`pin_irq`)
Pins NIC interrupt(s) to a specific CPU core and prevents irqbalance from
overriding the pin during a run.

**Must be called after `tune_nic`** — channel count determines which IRQs exist.

## IRQ Pinning and irqbalance

irqbalance periodically moves IRQs between CPUs to balance load. If you pin
an IRQ via `/proc/irq/N/smp_affinity` and irqbalance is running, it will
eventually reset your pin. You must coordinate with irqbalance.

Three modes, choose based on the situation:

| Mode | What it does | When to use |
|------|-------------|-------------|
| `ban_irq` (default) | Adds the NIC IRQ(s) to `IRQBALANCE_BANNED_INTERRUPTS`; irqbalance restarts and keeps balancing everything else | Preferred — surgical, least disruptive |
| `ban_cpu` | Adds the target CPU to `IRQBALANCE_BANNED_CPUS`; irqbalance won't place any interrupt on that CPU | Use when you also want the application CPU free from all irqbalance activity |
| `disable` | Masks and stops irqbalance entirely | Use only when you control all IRQ affinity on the host and don't want any automatic balancing |

## Application CPU and IRQ CPU Relationship

For maximum throughput on NUMA-attached NICs:
- The NIC IRQ CPU and the application (benchmark) CPU should be on the **same
  NUMA node** as the NIC.
- They should be **different cores** — ideally adjacent cores sharing the same
  CCD/CCX (Compute Complex Die on AMD, Core Complex on Intel) for lowest
  inter-core latency.
- Having both the IRQ and the application thread on the same core (hyperthreading)
  causes contention and degrades throughput.

Example (AMD R7725, ConnectX-7 NIC on NUMA node 1):
- IRQ CPU: 194 (handles NIC interrupts)
- Application CPU: 195 (runs uperf, adjacent core, same CCD)
- This arrangement lets the CPU doing receive processing (194) hand off
  immediately to the application (195) with minimal cross-core latency.

## Correct Tuning Order

```
1. tune_nic(host, interface, channels=1)       # Set channel count first
2. tune_tcp(host, congestion_control="bbr",    # TCP stack (order vs nic doesn't matter,
            qdisc="fq")                        # but must be before benchmark starts)
3. pin_irq(host, interface, cpu=194,           # Pin IRQ after channel count is set
           irqbalance_mode="ban_irq")
4. verify_host_tuning(host, interface,         # Confirm everything applied
   expected={
       "congestion_control": "bbr",
       "qdisc": "fq",
       "channels": 1,
       "irq_cpu": 194,
   })
```

Include the full `verify_host_tuning` result in the provisioning completion
report. The benchmark agent uses this to confirm the host was correctly tuned
before launching a run.

## Post-Benchmark Verification

Call `verify_host_tuning` again after the benchmark completes (from the review
agent) to detect drift:
- Did irqbalance override the IRQ pin during the run?
- Did a kernel update change the qdisc?
- Did channel count change (e.g., driver reload)?

Drift detected in the post-run verification is a primary suspect when
throughput is lower than expected. Include the before/after comparison in the
review output.

## Common Mistakes

- Setting `fq_codel` instead of `fq` with BBR — looks similar, behaves very
  differently. BBR + `fq_codel` will not reach full throughput.
- Pinning IRQ before setting channel count — the IRQ number may change when
  channels are reduced, leaving the old IRQ unpinned and the new one floating.
- Forgetting to restart irqbalance after editing its config file — the new
  banned list only takes effect after restart.
- Pinning the IRQ and the application thread to the same physical core —
  they compete for execution slots. Use adjacent cores.
