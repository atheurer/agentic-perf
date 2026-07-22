# Jumpstarter Provisioning Procedure

## Prerequisites

- `jmp_connect` with the `lease_id` from ticket metadata
- `jumpstarter_flash` field on ticket (set by orchestrator)

## Flash

The orchestrator pre-resolves image URLs and stores the result
in `jumpstarter_flash`. Use the `flash_command` directly via
`jmp_run` with `timeout_seconds=600`.

If `jumpstarter_flash` has an `error` field, call
`request_clarification` with the error and available variants.

If flashing fails with a TLS/SSL certificate error, retry with
`--insecure-tls` added after `storage flash` in the command array.

If flashing fails for any other reason, retry once. If it fails
a second time, submit `provisioning_complete=false` with a note
describing the error. Do NOT retry more than once — repeated
flash failures indicate an infrastructure problem that retrying
cannot fix.

## Post-Flash

After flashing (which includes a power cycle), wait ~60 seconds
for the board to boot, then use `jmp_run` with
`command=["tcp", "address"]` to discover the IP address.

If `tcp address` returns no result, try `jmp_run` with
`command=["power", "cycle"]`, wait 60s, and retry.

## SSH Key Injection

The orchestrator's SSH public key is in
`jumpstarter_flash.ssh_public_key`. Inject it via `jmp_run`.

**CRITICAL: All SSH commands MUST use the `bash -c` pattern.**
Shell operators (`&&`, `>>`, `|`) are NOT interpreted when
passed as separate array elements. Always wrap the full remote
command in a single `bash -c` string:

```
jmp_run command=["ssh", "--", "bash", "-c", "mkdir -p /root/.ssh && chmod 700 /root/.ssh && echo '<key>' >> /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys"]
```

Do NOT split shell commands into separate array elements:
```
# WRONG — && is passed as a literal argument, not a shell operator
jmp_run command=["ssh", "--", "mkdir", "-p", "/root/.ssh", "&&", ...]
```

All device commands go through `jmp_run` with the appropriate
driver path as the command array. Do NOT use `j` CLI directly —
it is not available in the MCP context.

## Completion

Submit `provisioning_complete=true` with:
- `hosts_provisioned`: the discovered IP
- `ssh_hardware_ips`: controller and targets (same IP for single device)
- `ssh_user`: "root"
- `ssh_key_path`: from `jumpstarter_flash.ssh_key_path`
- `harness_name`: from ticket directives
- `configuration_applied`: include the board/exporter name

Do NOT attempt to install the benchmark harness. Provisioning
means flash + boot + key injection only.
