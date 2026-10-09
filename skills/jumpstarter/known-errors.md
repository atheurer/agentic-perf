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

## Board Powers Off Shortly After Successful Provisioning

**Symptom:** Board is flashed, boots, SSH is verified, but
~2-3 minutes later the board powers off. Serial capture
shows `systemd-shutdown` followed by `reboot: Power down`.
No benchmark samples are collected.

**Serial indicators:**
- `Initramfs unpacking failed: Decoding failed`
- `reboot: Restarting system` (first boot auto-reboot)
- `reboot: Power down` (second boot gives up)
- `Invalid GPT` / `Can't read GPT header`

**Cause:** AutoSD uses **sysboot** for boot health checks
and **ukiboot** for A/B partition management. When the
initramfs is corrupted:

1. First boot: initramfs fails → sysboot health check
   fails → `FailureAction=reboot-force` → automatic reboot
2. Second boot: initramfs fails again →
   `tries_remaining` exhausted → no valid boot slot →
   system powers off

The board appears to boot successfully (reaches login,
SSH works) because the rootfs on disk is intact — but
sysboot detects the unhealthy initramfs and triggers the
A/B rollback mechanism. With a fresh flash there is no
slot B to fall back to, so it powers off.

**Diagnosis:** Check the provisioning serial capture
(`platform-provision/serial-capture.log`) for
`Initramfs unpacking failed`. If present, the OS image
is corrupted or incompatible with this board.

**This is NOT an agentic-perf or Jumpstarter issue.** The
problem is in the OS image build. Report to the image
build team with:
- The exact image URL from `jumpstarter_flash.flash_targets`
- The board type and firmware version from serial output
- The full provisioning serial log as evidence

**Timing signature:** The consistent ~170s kernel timestamp
on the shutdown (across different boards) is the sysboot
health check timeout + ukiboot retry exhaustion. If you
see `Power down` at roughly the same kernel time on
multiple boards with the same image, it confirms an image
problem rather than a board-specific hardware issue.

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

## pexpect EOF During Flash (U-Boot Serial Connection Lost)

**Error:** `pexpect.exceptions.EOF: End Of File (EOF).
Empty string style platform.` with `searcher_string: 0: b'=>'`

**Cause:** The flash tool connects to the board's serial
console via a TCP port-forwarded gRPC tunnel.  During
`reboot_to_console()`, it sends ESC to interrupt U-Boot
autoboot and waits for the `=>` prompt.  EOF means the
serial TCP socket closed before the prompt appeared.

This is typically a **transient gRPC tunnel instability**,
not a board hardware problem.  The board's serial output
(visible in `serial-capture.log`) usually shows U-Boot
reaching the autoboot countdown normally.

**Diagnosis:**
- Check `serial-capture.log` — if U-Boot appears and
  reaches "Hit any key to stop autoboot", the board
  is healthy; the issue is the tunnel.
- Check `flash-diagnostics.json` — the unwrapped
  exception chain shows the full error path.
- The `BrokenResourceError` and `InvalidStateError:
  RPC already finished` messages in pod logs confirm
  gRPC tunnel drops.

**Fix:** Retry.  Transient gRPC instability usually
resolves on the next attempt.  If the error persists
across multiple boards and multiple attempts, escalate
to the Jumpstarter infrastructure team — the controller
or exporter networking may be degraded.

**This error is RETRYABLE** — do not treat it as
unrecoverable.  The flash tool's internal 4 retries may
not be enough if the gRPC tunnel is consistently
unstable during that window.

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
