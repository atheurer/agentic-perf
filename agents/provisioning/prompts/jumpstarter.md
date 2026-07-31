## Jumpstarter Devices — Provisioning Notes

Jumpstarter devices are physical boards or virtual machines.
The platform agent has already flashed the OS image, booted
the board, discovered the IP, and injected SSH keys.

For self-installing harnesses (boot-time, arcaflow-plugins),
no additional provisioning is needed — the provisioning agent
auto-completes.

For other harnesses, the board is SSH-reachable at the IP
in `hosts_provisioned`. Install the harness as you would on
any remote host.

### Important

- The device acts as both controller and target (single host)
- Podman is available for running containerized benchmarks
- Do NOT attempt to flash or power cycle the board — the
  platform agent handles device operations
- Do NOT use Jumpstarter MCP tools — they are not available
  to the provisioning agent
