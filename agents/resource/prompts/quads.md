## Acquiring QUADS / Scale Lab Resources

QUADS provides dedicated bare-metal servers with no virtualization overhead.

1. Call check_available_resources with provider "quads" and requirements
   from the ticket (cores, memory, NIC speed, disk type, number of hosts).
2. Select resources from the available options.
3. Call reserve_resources with selection `{ "hostnames": [ ... ] }`, using
   the `hostname` from each selected option. Select distinct eligible hosts
   that match the ticket's managed host requirements, one per required host.
   Do not pass cloud instance fields in a QUADS selection. Always include the
   ticket_id for assignment traceability.
4. Validate each host with validate_host.
5. Call submit_resource_result with the reservation details.

## QUADS Policy

- Max 10 hosts per assignment
- Max 5-day lifetime
- The selection must contain a nonempty list of distinct `hostnames` returned
  by check_available_resources. If the returned options do not satisfy every
  managed host requirement, ask for guidance instead of guessing hostnames.
