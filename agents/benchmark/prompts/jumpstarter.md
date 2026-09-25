## Jumpstarter Device Notes

This device was provisioned via Jumpstarter (a physical board or
virtual machine managed through a lab controller service).

- The device IP is in `assigned_hardware_ips.targets[0]`
- Use this as `sut_host` for boot-time tests
- SSH as root with the key path from the ticket fields
- Podman is available for running containerized benchmarks
- The device may have limited resources compared to full servers —
  check available cores and memory before choosing benchmark
  parameters
- Do NOT attempt to manage the Jumpstarter lease — that is handled
  by the resource agent during teardown
