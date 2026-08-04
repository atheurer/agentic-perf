# Jumpstarter Lease Management

## Lease Duration

Jumpstarter leases have a maximum duration set by the controller
(typically 2 hours). Plan benchmark sample counts to fit within
the available lease time.

### Boot-time harness timing

Each boot-time sample cycle includes:
- Reboot command (~1s)
- Wait for board to power down (~5s)
- Boot time (~10-30s depending on board/OS)
- SSH reconnection (~5-10s)
- Data collection (~2-5s)
- Serial capture overhead (~30s with --power-off-delay)

**Estimated per-sample time: 45-75 seconds**

### Sample count guidelines for 2-hour leases

| Board type | Est. per-sample | Max samples in 2hr | Recommended |
|---|---|---|---|
| R-Car S4 | ~45s | ~150 | 50 (default) |
| NXP S32G | ~60s | ~110 | 50 (default) |
| SA8775P | ~75s | ~90 | 40 |

Add ~15 minutes overhead for flash, boot, harness install,
and metadata collection. For a 2-hour lease, plan for at
most 100 minutes of reboot cycles.

### When samples are lost to lease expiry

If the lease expires mid-benchmark, the harness collects
partial results. 40+ samples is statistically valid for
most boot-time investigations. The system should NOT
request a lease extension just to get the last few samples
— the existing data is sufficient.

## Lease Extension

Active leases can be extended when genuinely needed (e.g.,
multi-pass investigations where re-provisioning is expensive).
The resource agent or platform agent can extend using the
Jumpstarter API before the lease expires.

Extension should be considered when:
- A multi-pass investigation needs the same board state
- Re-flashing would lose important board configuration
- The remaining work is small relative to re-provision cost

Extension should NOT be used to:
- Hold hardware idle while waiting for user input
- Compensate for inefficient benchmark parameters
- Reserve boards for future unplanned work

## Lease Cleanup

When a ticket is torn down or aborted, the Jumpstarter lease
must be released. The resource agent handles this during
teardown. If the lease has already expired naturally, the
release is a no-op.
