# Custom Image Building with CAIB

## Overview

CAIB (Cloud Automotive Image Builder) builds custom AutoSD images
from AIB manifests. Use it when testing requires an image that
differs from the standard nightly — package overrides, service
changes, kernel config, or building from a specific AIB commit.

## When to Use

- Testing a fix before it lands in nightly builds
- A/B testing with package overrides (avoids system_config
  workarounds with dnf/rpm)
- Building from a specific automotive-image-builder commit
- Custom kernel configurations or service modifications
- Reproducing a specific build environment

## Concepts

### Targets vs Build Modes

**Target** = the hardware platform. It determines the partition
layout, bootloader format, and flash procedure:

| Target | Boards | Partition layout |
|---|---|---|
| `ebbr` | S32G, R-Car S4, TI (J784S4) | Single partition, EBBR firmware |
| `ride4_sa8775p_sx` | SA8775P (QC8775) ES2.1 v2/v2.5 | 4 partitions: system_a, system_b, boot_a, boot_b (aboot) |
| `ride4_sa8775p_sx_r3` | SA8775P (QC8775) ES2.1 v3 | 4 partitions (aboot) |

**Build mode** = the image type. It determines how packages are
managed on the running system. **Both modes work on all targets**:

| Mode | CLI | Image type | Package management | Use when |
|---|---|---|---|---|
| Package | `build-dev --mode package` | `regular` | `dnf` — persistent installs, mutable rootfs | Post-deploy modifications needed (kernel swaps, RPM installs, config changes) |
| Bootc/OSTree | `build --disk` or `build-dev --mode image` | `ostree` | `rpm-ostree` — atomic, immutable rootfs | Production-like testing, integrity verification |

Target and build mode are **independent choices**. A QC8775 board
can run either a package-mode or bootc image. An EBBR board can
run either type. Choose based on what the test requires, not the
hardware.

## Build Modes

### Package mode

Produces a mutable image with traditional `dnf` package management.
Use when you need to install RPMs or modify config post-flash:

```bash
caib image build-dev manifest.aib.yml \
  --mode package \
  --format simg \
  --target <target> \
  --push <registry-url> \
  --wait \
  --output-format json
```

### Bootc/OSTree mode

Produces an immutable image with atomic updates:

```bash
caib image build manifest.aib.yml \
  --format simg \
  --target <target> \
  --disk \
  --push <registry-url> \
  --wait \
  --output-format json
```

## AIB Manifest Format

Manifests (`.aib.yml`) define the image content:

```yaml
name: custom-test-image

content:
  rpms:
    - openssh-server
    - chrony
    - iproute
    - custom-package
  repos:
    - name: custom-repo
      baseurl: https://download.copr.fedorainfracloud.org/results/user/repo/centos-stream-10-$basearch/
  systemd:
    enabled_services:
      - sshd.service
      - custom.service
    disabled_services:
      - podman-clean-transient.service
    masked_services:
      - unwanted.service

> **Important:** `masked_services` is NOT supported by the AIB
> manifest schema. It will be silently converted to
> `disabled_services`. Only `enabled_services` and
> `disabled_services` are valid in the `systemd` section.

> **Network:** The manifest must include a `network` section.
> Use `network: { dynamic: {} }` for NetworkManager-based DHCP
> (the default). Without this, network interfaces may not be
> configured even if NetworkManager is installed.

image:
  image_size: 8 GiB
  sealed: false

auth:
  root_password: $6$...hashed...
  sshd_config:
    PermitRootLogin: true
    PasswordAuthentication: true
```

### Key Manifest Sections

| Section | Purpose |
|---|---|
| `content.rpms` | Packages to install |
| `content.repos` | Custom RPM repositories |
| `content.systemd` | Service enable/disable/mask |
| `content.add_files` | Add files (inline, path, or URL) |
| `content.remove_files` | Remove files from the image |
| `kernel.cmdline` | Kernel command line parameters |
| `kernel.kernel_package` | Custom kernel package |
| `image.image_size` | Disk image size |
| `image.sealed` | Enable dm-verity sealing |
| `auth` | Root password and SSH config |

## Build Lifecycle

1. **Submit**: `caib image build-dev` submits to the cloud
   build service
2. **Build**: runs osbuild in a remote environment (~10-30 min)
3. **Push**: result pushed to internal registry or external
   registry via `--push`
4. **Download/Flash**: `caib image download <name>` or
   flash directly via `j storage flash oci://<url>`

## CLI Reference

### Build Management

```bash
# List builds
caib image list --output-format json

# Show build details
caib image show <build-name> --output-format json

# Cancel a build
caib image cancel <build-name>

# Download artifact
caib image download <build-name> -o <output-dir>

# Follow build logs
caib image logs <build-name>
```

### Key Flags

| Flag | Purpose |
|---|---|
| `--wait` | Block until build completes |
| `--internal-registry` | Push to OpenShift internal registry |
| `--push <url>` | Push to external OCI registry |
| `--format simg` | Produce Jumpstarter-compatible image |
| `--target <name>` | Target platform |
| `--name <name>` | Set build name (for caching/reuse) |
| `--timeout <min>` | Build timeout (default: 60) |
| `--output-format json` | Machine-readable output |
| `-D KEY=VALUE` | Custom AIB definitions |
| `--aib-image <ref>` | Specific AIB version |
| `--extra-repo` | Add RPMs from workspace |

## Authentication

CAIB shares authentication with Jumpstarter. When
`jmp login` is configured, `caib login` derives the
build API endpoint automatically. The auth token is
cached at `~/.config/caib/cli.json`.

## Integration with agentic-perf

Custom builds replace the standard nightly image URL in the
provisioning flow. The platform agent flashes from the custom
image's OCI URL instead of the default nightly:

```
j storage flash oci://<registry>/<image>:<tag>
```

The build step occurs BEFORE hardware acquisition to avoid
holding a Jumpstarter lease during the build (~10-30 min).

## Required Packages by Target

The AIB base for each target is minimal. The nightly images add
many packages via their build profiles (qa, ps). When building
custom images, you must include packages that the nightly profile
would normally add.

### Packages required for remote access and testing

These must be in `content.rpms` for any custom build that needs
SSH access and benchmarking:

```yaml
content:
  rpms:
    - openssh-server
    - chrony
    - iproute
    - podman
    - NetworkManager       # critical — without this, network
                           #   interfaces are not configured
                           #   in Linux even if firmware assigns
                           #   an IP via DHCP
```

### QC8775 / SA8775P (ride4_sa8775p_sx) specifics

The nightly `qa/regular` image for this target uses
`kernel-ivos-qualcomm` (NOT `kernel-automotive`). Key packages:

| Package | Purpose |
|---|---|
| `kernel-ivos-qualcomm` | Qualcomm-specific kernel |
| `kernel-ivos-qualcomm-modules-extra` | Additional kernel modules |
| `kernel-ivos-qualcomm-modules-internal` | Internal kernel modules |
| `downstream-dtbs` | Device tree blobs for SA8775P |
| `kmod-qcom-scmi` | Qualcomm SCMI kernel module |
| `NetworkManager` | Network interface configuration |
| `dhcpcd` | DHCP client |

When overriding the kernel via the `kernel` manifest field for
QC8775, you must also provide matching DTBs (either via the
kernel RPM or a separate `downstream-dtbs-*` package in
`content.rpms`).

### EBBR boards (S32G, R-Car S4) specifics

EBBR boards use `kernel-automotive` and single-partition flash.
The base package set is simpler but still requires `NetworkManager`
for network access.
