## Acquiring Jumpstarter Lab Hardware

Jumpstarter provides physical embedded/automotive hardware via gRPC-based
device leasing from a controller. Devices are identified by label selectors
(e.g., `target=ride4_sa8775p_sx_r3`).

1. Call check_available_resources with provider "jumpstarter" and
   requirements from the ticket. Include:
   - `jumpstarter_selector`: label selector for the board type
   - `count`: number of devices needed (usually 1)
2. Call reserve_resources with:
   - `jumpstarter_selector`: same selector
   - `lease_duration_seconds`: based on expected workload (default 14400 = 4h)
   - `ticket_id`: for lease traceability
3. Record the `lease_id` from the reservation result.
4. Do NOT call validate_host — the board is not yet provisioned.
   The provisioning agent handles flashing, boot verification, and
   IP discovery.
5. Call submit_resource_result with the reservation details.

## Jumpstarter-Specific Fields

In submit_resource_result:
- `resource_provider`: "jumpstarter"
- `resource_reservation_id`: the lease_id from reserve_resources
- `resource_provider_metadata`: include `lease_id`, `exporter_name`,
  `selector`, `duration_seconds`
- `assigned_hardware_ips`: leave empty — the provisioning agent
  discovers the IP after flashing
- `ssh_user`: "root"
- `fresh_host`: true (board will be freshly provisioned)

## Lease Duration Guidelines

- Simple benchmark (stress-ng, sysbench): 2h (7200s)
- Multi-benchmark suite: 4h (14400s)
- Multi-cycle investigation: 8h (28800s)
- The CI nightly uses 1d — prefer shorter leases for agentic use
  since budget guardrails handle early termination

## Important

- Jumpstarter devices are physical boards (ARM embedded hardware).
  They must be flashed with an OS image before they can be used.
- The provisioning agent handles flashing — the resource agent only
  handles the lease.
- Leases are always cleaned up automatically on ticket teardown.
