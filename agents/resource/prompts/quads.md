## Acquiring QUADS / Scale Lab Resources

QUADS provides dedicated bare-metal servers with no virtualization overhead.

1. Call check_available_resources with provider "quads" and requirements
   from the ticket (cores, memory, NIC speed, disk type, host count).
2. Select resources from the available options.
3. Call reserve_resources with the selected options. Always include the
   ticket_id for assignment traceability.
4. Validate each host with validate_host.
5. Call submit_resource_result with the reservation details.

## QUADS Policy

- Max 10 hosts per assignment
- Max 5-day lifetime
