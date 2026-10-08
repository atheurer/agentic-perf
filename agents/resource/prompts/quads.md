## Acquiring QUADS / Scale Lab Resources

QUADS provides dedicated bare-metal servers with no virtualization overhead.

1. Call check_available_resources with provider "quads" and the ticket's
   required_hosts. Preserve requested host count and per-host constraints such
   as NIC speed, model, memory, and disk type. For example, a request for two
   hosts with 400G NICs means select exactly two hosts whose inventory reports
   a 400 Gbps interface.
2. Select only resources from the returned available options. Choose distinct
   hostnames and exactly the number of hosts requested.
3. Call reserve_resources with the selected hostnames and include the ticket_id
   for assignment traceability. Carry a requested OS through as selection.os;
   the server enforces a single OS requested for all provider-allocated hosts.
4. QUADS wipes and reprovisions hosts by default. If the user explicitly asks
   to preserve the existing OS or says not to wipe, triage sets
   directives.quads_wipe=false and the reservation must pass selection.wipe=false.
   Do not combine no-wipe with a requested OS: the OS cannot be guaranteed
   without reprovisioning, so ask the user which requirement to honor.
5. Use the requested OS title exactly (for example, "RHEL 10.1"); never silently
   substitute the QUADS default or a different version if the requested title is
   unavailable. Report the QUADS error and ask the user how to proceed.
   If QUADS reports conflicting OS requirements across the selected hosts, ask
   the user to choose one OS or request separate assignments.
6. Validate each host with validate_host.
7. Call submit_resource_result with the reservation details.

## QUADS Policy

- Max 10 hosts per assignment
- Max 5-day lifetime
- One OS title applies to every host in an assignment. Do not combine different
  per-host OS requirements in a single assignment.

For a request such as "find 2 hosts in QUADS with 400G NICs and reserve them
with RHEL 10.1," check that each selected host has a 400 Gbps interface, choose
two distinct hosts, and pass the exact OS title in selection.os.
