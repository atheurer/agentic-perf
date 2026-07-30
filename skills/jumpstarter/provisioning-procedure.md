# Jumpstarter Provisioning Procedure

## Context

The platform agent has already flashed the OS image,
booted the board, discovered the IP, and injected SSH
keys. The board is SSH-reachable at the IP in
`hosts_provisioned`.

## Self-Installing Harnesses

For boot-time and arcaflow-plugins, no additional
provisioning is needed. The provisioning agent
auto-completes — the OS image and container runtime
provide everything.

## Other Harnesses

For harnesses that require installation (e.g., crucible):

1. SSH to the board at the IP in `hosts_provisioned`
2. Install prerequisites and the harness as you would
   on any remote host
3. Podman is available for containerized workloads

## Important

- Do NOT flash or power cycle the board — the platform
  agent handles device operations
- Do NOT use Jumpstarter MCP tools — they are not
  available to the provisioning agent
- The board is a single host acting as both controller
  and target
