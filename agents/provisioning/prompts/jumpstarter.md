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

#### Step 2: Resolve image URLs

Before flashing, you MUST resolve the actual image file URLs from
the image server's metadata. Do NOT guess filenames.

Use `jmp_run` to download and read the image info JSON:
```
j ssh -- curl -sL https://autosd.sig.centos.org/AutoSD-10/nightly/info/test_images_info.json
```

The JSON contains board-specific image paths keyed by target label
(e.g., `ride4_sa8775p_sx_r3` or `qc8775`). Find the entry matching
the board type and the requested image_name (e.g., `ps`) and
image_type (e.g., `regular`). Extract:
- For multi-partition boards: `root_image_path`, `aboot_image_path`,
  and optionally `qm_var_path`
- For single-image boards: `path`

Prepend the base URL to get full download URLs:
`https://autosd.sig.centos.org/AutoSD-10/nightly/<path_from_json>`

#### Step 3: Flash the OS image

Use `jmp_run` to flash with the resolved URLs. This takes several
minutes — use a timeout of at least 600 seconds.

**Multi-partition boards** (Qualcomm RideSX4 SA8775P):
```
j storage flash -t system_a:<ROOT_IMAGE_URL> -t boot_a:<ABOOT_IMAGE_URL> -t boot_b:<ABOOT_IMAGE_URL>
```
If a QM var image is available:
```
j storage flash -t system_a:<ROOT_IMAGE_URL> -t boot_a:<ABOOT_IMAGE_URL> -t boot_b:<ABOOT_IMAGE_URL> -t system_b:<QM_VAR_URL>
```

**Single-image boards** (R-Car S4, NXP S32G):
```
j storage flash <IMAGE_URL>
```

IMPORTANT: The image URLs must point to actual image files (.img,
.img.gz, .simg), NOT to the test_images_info.json metadata file.

If flashing fails with a TLS/SSL certificate error, retry with
`--insecure-tls` added after `j storage flash`.

#### Step 4: Power cycle

```
j power cycle
```

This reboots the board from the newly flashed image.

#### Step 5: Wait for boot and discover IP

After power cycle, wait ~60 seconds for the board to boot, then:

```
j tcp address
```

This returns the device's IP and port (e.g., `192.168.1.100:22`).
Extract the IP address — this is `SUT_IP`.

#### Step 6: Verify SSH connectivity

```
j ssh -- uptime
```

If this shows load average output, the board is responsive. Also check
network interfaces:

```
j ssh -- ip -4 addr show
```

Verify there is a `scope global` interface (routable network).

#### Step 7: Set up SSH key access

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

#### Step 8: Submit result

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
