## Jumpstarter Device Notes

This is a physical ARM embedded board provisioned via Jumpstarter.

- The device is a single host acting as both controller and target
- Use the SSH IP discovered during provisioning (in ssh_hardware_ips)
- SSH as root with the key path from the ticket fields
- Podman is available for running containerized benchmarks
- Arcaflow plugins are multi-arch (ARM images are available)
- The board has limited resources compared to x86 servers — adjust
  benchmark parameters accordingly (lower worker counts, shorter
  timeouts)
- Do NOT attempt to manage the Jumpstarter lease — that is handled
  by the resource agent during teardown
