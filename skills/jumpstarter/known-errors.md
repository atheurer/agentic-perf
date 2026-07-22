# Jumpstarter Known Errors

## Driver Compatibility

**Error:** `doesn't match any of the allowed patterns`
or `driver not found`

**Cause:** The `j` CLI via socket does not read the client
config's `drivers.unsafe` setting. It defaults to
`unsafe=False` with an empty allow list, rejecting drivers
like `jumpstarter_driver_snmp` (used by QC8775 boards).
Can also indicate a version mismatch between client and
exporter.

**Fix:** Ensure `JMP_DRIVERS_UNSAFE=true` is set in the
environment when launching `jmp mcp serve`. The
`attach_jumpstarter_mcp()` function does this
automatically. If the error persists, run
`scripts/setup-jumpstarter.sh` to reinstall drivers.

**IMPORTANT:** This error is FATAL — do not retry. Every
subsequent `jmp_run` call will fail the same way. Call
`request_clarification` immediately.

## Port 8080 Already In Use

**Error:** `[Errno 98] address already in use` on port 8080
during `j storage flash`

**Cause:** A previous flash operation left a stale HTTP
server process on the exporter host. This is an exporter-side
issue — the client cannot fix it.

**Fix:** The exporter administrator must kill the stale
process on the exporter host. Retrying or power cycling
will not help. Report the failure and request a different
board.

## Lease Cannot Be Satisfied

**Error:** `the lease cannot be satisfied`

**Cause:** No exporter matching the selector is available.
All matching devices may be leased by other users, offline,
or disabled.

**Fix:** Wait for a device to become available, or check
if the selector is correct via `list_jumpstarter_targets`.
