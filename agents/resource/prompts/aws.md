## Acquiring AWS Resources

1. Call check_available_resources with provider "aws" and requirements
   from the ticket (instance type, count, OS, region).
2. Select resources from the available options.
3. Call reserve_resources with the selected options. Always include the
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

## Cost Awareness

Cloud instances do not expire automatically — teardown is critical to avoid
ongoing costs. Always set resource_provider and resource_reservation_id so
teardown can terminate them.
