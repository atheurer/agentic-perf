## Cloud Provider Context

Before making AWS-specific resource decisions, call
`get_skill_context(subject="resource/aws", operation="bootstrap")` and read
the returned guidance. This resource subject contains agentic-perf provider
behavior; it does not contain AWS credentials. Keep using the deterministic
resource tools for availability checks, reservations, validation, and teardown.

## Acquiring AWS Resources

1. Call check_available_resources with provider "aws" and requirements
   from the ticket (instance type, count, OS, region).
2. Select resources from the available options.
3. Pass either `{ "instance_type": "...", "count": N }` for a uniform
   selection, or `{ "instance_specs": [{ "instance_type": "...",
   "count": N, "role": "..." }, ...] }` for per-role instance types.
   Counts must cover the ticket's managed hosts. Do not combine these two
   formats or pass another provider's selection fields. Always include the
   ticket_id for instance traceability.
4. Validate each host with validate_host.
5. Call submit_resource_result with the reservation details.

## Cloud Provider IP Handling

reserve_resources returns both public and private IPs. In
assigned_hardware_ips, use the IPs from the reserve_resources result — do
NOT substitute hostnames from validate_host. The system automatically maps
IPs to their public/private counterparts for SSH access vs run-file entries.

validate_host is for verifying connectivity and gathering system info only.
The IPs from reserve_resources are the canonical identifiers.

## AWS Selection Fields

- `instance_specs` entries require `instance_type`; `count` defaults to 1 and
  `role` is optional.
- The flat selection requires `instance_type`; `count` defaults to 1.
- Optional fields are `ami`, `os`, `root_volume_gb`, and the legacy alias
  `instance_count` for the flat count.

## Cost Awareness

Cloud instances do not expire automatically — teardown is critical to avoid
ongoing costs. Always set resource_provider and resource_reservation_id so
teardown can terminate them.
