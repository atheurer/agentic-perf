# Jumpstarter Known Errors

## Driver Compatibility

**Error:** `doesn't match any of the allowed patterns`
or `driver not found`

**Cause:** Version mismatch between the Jumpstarter client
and the exporter. The exporter runs a driver class that the
client doesn't recognize (e.g., exporter on 0.9, client on
0.8.1).

**Fix:** Upgrade the Jumpstarter client and all driver
packages to match the exporter version. Run
`scripts/setup-jumpstarter.sh` to reinstall.

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
