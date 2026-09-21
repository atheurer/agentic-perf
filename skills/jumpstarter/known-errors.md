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

**Fix:** The platform agent sets `drivers.unsafe=True`
via the client config. If the error persists, run
`scripts/setup-jumpstarter.sh` to reinstall drivers.

**IMPORTANT:** This error is FATAL — do not retry.

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

## Boot Failure Diagnosis via Serial Capture

When `serial_capture: true` is set in ticket directives,
the platform agent captures serial output during the
flash→boot→verify sequence. On provisioning failure,
the last 2000 characters of serial output are included
in the diagnostics.

Common serial output patterns:

- **`ApplyOverlay: ufdt apply overlay failed`** — DTB
  overlay incompatibility. The kernel or DTB in the
  image does not match the board's firmware expectations.
  Requires a board-specific DTB overlay.
- **`Kernel panic`** — Kernel crash during boot. Check
  for driver incompatibilities or missing modules.
- **No output at all** — Board did not reach firmware
  stage. May indicate a flash failure or power issue.
- **Output stops at U-Boot** — Kernel failed to load.
  Check image format and partition layout.

Serial logs are saved as artifacts at
`platform-provision/serial-capture.log` and can be
downloaded from the ticket's artifact list.

## Lease Cannot Be Satisfied

**Error:** `the lease cannot be satisfied`

**Cause:** No exporter matching the selector is available.
All matching devices may be leased by other users, offline,
or disabled.

**Fix:** Wait for a device to become available, or check
if the selector is correct via `list_jumpstarter_targets`.

## Exporter Disconnect During Provisioning

**Error:** `gRPC UNAVAILABLE: Stream removed (Socket closed)`
or `Connection to exporter lost: exporter is offline`

**Cause:** The Jumpstarter exporter's gRPC session drops
during or after a board reboot. This is a transient
condition — the exporter typically reconnects within
30-60 seconds.

**Recovery:** The provisioning code automatically retries
TCP address resolution with backoff (30s, 45s, 60s waits).
If all retries fail, the platform agent escalates to HITL.

**User action if HITL:** Retry — the exporter usually
recovers. If the board consistently fails, try a different
board of the same type.

## Failed to Get U-Boot Prompt

**Error:** `RuntimeError: Failed to get U-Boot prompt`
(often wrapped in `ExceptionGroup: unhandled errors in
a TaskGroup`)

**Cause:** The board did not reach the U-Boot bootloader
prompt after power cycle. This typically indicates a power
sequencing issue — the board needs a longer delay between
power off and power on to fully discharge capacitors and
reset the boot ROM.

**Fix:** Power cycle with a longer wait before retrying:
```
j power cycle --wait 180
```

The 180-second wait allows the board's power subsystem to
fully reset. After this extended power cycle, retry the
flash operation. This is a known issue with some NXP S32G
boards and Qualcomm SA8775P boards.

**For the platform agent:** When provisioning fails with
a U-Boot prompt error, retry with an extended power-off
delay (180s) before the next flash attempt. Do not
immediately retry with the default short delay.

## ExceptionGroup / TaskGroup Errors

**Error:** `ExceptionGroup: unhandled errors in a TaskGroup
(1 sub-exception)`

**Cause:** This is a Python wrapper around the real error.
The actual cause is in the sub-exception. Common wrapped
errors include:
- `Failed to get U-Boot prompt` — power sequencing issue
- `Stream removed (Socket closed)` — exporter disconnect
- `Connection to exporter lost` — board went offline

**Diagnosis:** Check the full exception chain in the pod
logs or diagnostics. The sub-exception contains the
actionable information.
