## Provisioning Jumpstarter Devices

Jumpstarter devices are physical embedded boards (ARM) that need to be
flashed with an OS image before use. You have Jumpstarter MCP tools
available — use `jmp_run` to execute device commands through the
Jumpstarter tunnel.

### Provisioning Flow

Follow these steps in order. All `jmp_run` commands require a
`connection_id` — get it from `jmp_connect` first.

#### Step 1: Connect to the leased device

Call `jmp_connect` with the `lease_id` from the ticket's
resource_provider_metadata. This establishes the tunnel to the
physical device and returns a `connection_id`.

#### Step 2: Flash the OS image

Use `jmp_run` to flash. The command depends on the board type:

**Single-image boards** (R-Car S4, NXP S32G):
```
j storage flash <IMAGE_URL>
```
Image URLs are pre-resolved by the orchestrator and stored in
the ticket's `jumpstarter_flash` field. Check this field for:
- `flash_command`: the complete `j storage flash` command to run
- `flash_targets`: list of {partition, url} for each partition
- `error`: if resolution failed, with `available_variants`

If `jumpstarter_flash` is present and has no error, use the
`flash_command` directly via `jmp_run`. This takes several
minutes — use a timeout of at least 600 seconds.

Do NOT try to resolve image URLs yourself. Do NOT fetch
test_images_info.json. The URLs are already resolved.

**Multi-partition boards** (Qualcomm RideSX4 SA8775P):
```
j storage flash -t system_a:<ROOT_IMAGE_URL> -t boot_a:<ABOOT_IMAGE_URL> -t boot_b:<ABOOT_IMAGE_URL>
```
If a QM var image is available:
```
j storage flash -t system_a:<ROOT_IMAGE_URL> -t boot_a:<ABOOT_IMAGE_URL> -t boot_b:<ABOOT_IMAGE_URL> -t system_b:<QM_VAR_URL>
```

Image URLs come from the ticket directives. If not provided, resolve
them from the image server:
- Base URL: `https://autosd.sig.centos.org/` (AutoSD) or as directed
- Path: `{image_version}/{release}/info/test_images_info.json`
- Match by board target label and image name/type

If flashing fails with a TLS/SSL certificate error, retry with
`--insecure-tls` added after `j storage flash`.

#### Step 3: Power cycle

```
j power cycle
```

This reboots the board from the newly flashed image.

#### Step 4: Wait for boot and discover IP

After power cycle, wait ~60 seconds for the board to boot, then:

```
j tcp address
```

This returns the device's IP and port (e.g., `192.168.1.100:22`).
Extract the IP address — this is `SUT_IP`.

#### Step 5: Verify SSH connectivity

```
j ssh -- uptime
```

If this shows load average output, the board is responsive. Also check
network interfaces:

```
j ssh -- ip -4 addr show
```

Verify there is a `scope global` interface (routable network).

#### Step 6: Set up SSH key access

The board uses password auth by default (`root`/`password`). Set up
key-based SSH so subsequent agents can connect directly.

Use `jmp_run` to inject the orchestrator's SSH public key via the
Jumpstarter tunnel. Run these as separate `jmp_run` calls:

```
j ssh -- "mkdir -p /root/.ssh && chmod 700 /root/.ssh"
```

```
j ssh -- sh -c "cat >> /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys" < /root/.ssh/id_rsa.pub
```

If the above stdin redirection doesn't work through the tunnel,
first read the public key with a local `jmp_run` call:
```
cat /root/.ssh/id_rsa.pub
```
Then inject it directly:
```
j ssh -- "echo '<PUBLIC_KEY_CONTENT>' >> /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys"
```

After injecting the key, verify direct SSH works using `execute_command`
to SSH directly to `SUT_IP` as root (not through the Jumpstarter tunnel).
Use `ssh_key_path` of `/root/.ssh/id_rsa`.

#### Step 7: Submit result

Call submit_provision_result with:
- `ssh_hardware_ips`: `{"controller": "<SUT_IP>", "targets": ["<SUT_IP>"]}`
  (single device acts as both controller and target)
- `ssh_user`: "root"
- `ssh_key_path`: path to the SSH key used
- `notes`: include the board name, image flashed, and any issues

### Recovery

If the board becomes unresponsive at any point:
1. Try `j power cycle` and wait 60s
2. If still unresponsive, re-flash the image (Step 2) and power cycle
3. If the board doesn't recover after re-flash, report the failure

### Important Notes

- These are embedded ARM boards, not x86 servers
- The board is a single device — it acts as both controller and target
- Podman is available in the OS image for running containerized benchmarks
- `j ssh` proxies SSH through the Jumpstarter tunnel (always works)
- Direct SSH to `SUT_IP` requires key injection (Step 6)
- Keep the Jumpstarter connection active — do NOT call `jmp_disconnect`
  until provisioning is complete
