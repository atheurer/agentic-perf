# AWS Resource Provider Guidance

## Selecting and Reserving Resources

1. Call `check_available_resources` with provider `aws` and the ticket's
   instance type, count, operating system, and region requirements.
2. Select resources from the returned options.
3. Call `reserve_resources` with the selected options and the ticket ID for
   instance traceability.
4. Validate each host with `validate_host`.
5. Call `submit_resource_result` with the reservation details.

## Cloud Provider IP Handling

`reserve_resources` returns public and private IPs. Use the IPs from that
result in `assigned_hardware_ips`; do not substitute hostnames returned by
`validate_host`. The system maps the addresses for SSH access and benchmark
run-file entries. `validate_host` verifies connectivity and gathers system
information only.

## Cost Awareness

Cloud instances do not expire automatically. Teardown is required to avoid
ongoing costs. Always set `resource_provider` and
`resource_reservation_id` so teardown can terminate the instances.
