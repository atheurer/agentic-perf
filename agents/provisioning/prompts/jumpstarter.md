## Provisioning Jumpstarter Devices

Jumpstarter devices are physical boards or virtual machines that
need to be flashed with an OS image before use. You have
Jumpstarter MCP tools available — use `jmp_run` to execute device
commands through the Jumpstarter tunnel.

Read the `jumpstarter/provisioning-procedure.md` skill for the
step-by-step flash and setup procedure. The key points:

- The orchestrator pre-resolves image URLs — use the
  `flash_command` from `jumpstarter_flash`, do NOT resolve
  URLs yourself
- Flash retries: once only, then escalate
- After flash, you MUST run `j tcp address` to discover the
  board's IP address. Submit this IP in `hosts_provisioned`
  and `ssh_hardware_ips` — do NOT use the selector label
  or board name as the host
- The SSH public key is pre-provided in `jumpstarter_flash` —
  do NOT read it from the local filesystem
- Do NOT install the benchmark harness — provisioning means
  flash + boot + key injection only

### Recovery

If the board becomes unresponsive:
1. Try `j power cycle` and wait 60s
2. If still unresponsive, re-flash and power cycle
3. If the board doesn't recover after re-flash, report failure

### Important

- The device acts as both controller and target (single host)
- Podman is available for running containerized benchmarks
- `j ssh` proxies SSH through the Jumpstarter tunnel
- Direct SSH requires key injection first
- Keep the Jumpstarter connection active — do NOT disconnect
