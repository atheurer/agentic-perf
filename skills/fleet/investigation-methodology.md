# Fleet Investigation Methodology

## When to Use

Use fleet investigation when you need to compare benchmark results
across multiple devices of the same type — for example, testing
boot times across all S32G boards to identify board-specific
anomalies vs platform-wide patterns.

Fleet works with any harness and any resource provider.

## How It Works

The fleet coordinator agent manages the iteration lifecycle:

1. **Benchmark or platform agent completes** → ticket enters
   `coordinating_fleet`
2. **Fleet coordinator records the result** — completed or partial
   (failures are data points, not showstoppers)
3. **Coordinator routes** — to `awaiting_hardware` for the next
   board, or to `evaluating_convergence` if exhausted

### Exhaustion Detection

The coordinator detects fleet exhaustion in three ways:

- **No board assigned** — the resource agent couldn't find any
  matching device (hard exhaustion)
- **Duplicate assignment** — the provider assigned a board that
  was already tested, indicating no new boards are available
  (soft exhaustion)
- **Resource HITL intercept** — the resource agent explicitly
  reported no untested boards available

Board selection is handled by the resource agent via
`exclude_hosts` filtering. The coordinator does NOT query
device inventory directly.

## Evaluate Agent Guidance

When evaluating a fleet investigation, you have per-host results
in `custom_fields.fleet_investigation.tested_hosts`. Each entry
has:

- `host_id`: device name (e.g., `nxp-s32g-vnp-rdb3-01`)
- `status`: `completed` or `partial`
- `metrics`: harness-specific key-value pairs (if completed)
- `failure_reason`: why the host failed (if partial)

### Analysis approach

1. **Compare metrics across hosts** — identify outliers vs
   fleet-wide patterns. A metric that varies across hosts
   suggests a device-specific issue; a metric consistent
   across hosts suggests a platform characteristic.

2. **Analyze ALL results including failures** — hosts with
   `status: partial` are data points, not noise. For each
   partial host, report:
   - The `failure_reason` and what it indicates
   - Whether the failure is board-specific (only this host)
     or systemic (multiple hosts fail the same way)
   - Any partial metrics that were collected before failure
   - Whether the failure pattern correlates with board
     attributes (firmware version, revision, pool labels)

   A fleet where 6/11 boards fail provisioning is a
   significant finding about infrastructure reliability,
   not something to gloss over.

3. **Categorize failure patterns** — group partial results by
   failure type (provisioning failure, benchmark failure,
   SSH timeout, etc.) and assess whether they represent:
   - Hardware issues (specific boards)
   - Infrastructure issues (lab-wide)
   - Software issues (image compatibility)
   - Transient issues (retry would succeed)

4. **Consider sample size** — each host has fewer samples than
   a single-host investigation. Statistical significance
   thresholds should account for the per-host sample count.

## Exhaustion Types

- **Hard exhaustion**: every matching device has been tested,
  or no devices were available at all. The fleet is
  definitively complete.
- **Soft exhaustion**: some devices were unavailable (leased
  by others, offline, or duplicate assignment detected). The
  fleet is complete for now but could be extended later.

## Pipeline Flow

```
triage → resource → platform → benchmark
           ↑                       ↓
           ↑              coordinating_fleet
           ↑                   ↓         ↓
           └──── awaiting_hardware   evaluating_convergence
                  (next board)        (fleet complete)
```

Each iteration resets the per-agent iteration budget via a
`fleet_iteration_epoch` event marker.

## Historical Data Confidence

When comparing fleet results against historical baselines,
assess confidence based on data age and collection context.
Fleet comparisons are especially sensitive to context drift
because hardware firmware versions, board revisions, and
lab configurations change independently across devices.

### Temporal decay

- **< 7 days**: Full confidence
- **7–30 days**: High confidence, note the age
- **30–90 days**: Medium confidence, flag explicitly
- **> 90 days**: Low confidence, contextual background only

### Context validation for fleet comparisons

Fleet comparisons require alignment on:

- **Firmware version per board**: Individual boards may have
  been updated at different times. A fleet baseline from
  3 months ago may include boards with older firmware.
- **Pool membership**: Boards may have been added to or
  removed from pools. The set of boards in a historical
  fleet run may not match the current pool.
- **Lab infrastructure**: Network topology, DHCP servers,
  and serial console configurations change. These affect
  boot timing and provisioning reliability.

When historical fleet data shows large per-board deviations
from current results, check whether the specific board's
firmware or configuration has changed before attributing
the difference to a performance regression.
