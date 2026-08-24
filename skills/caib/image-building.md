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

## Build Modes

### Package mode (EBBR)

For S32G and TI boards using package-based EBBR images:

```bash
caib image build-dev manifest.aib.yml \
  --mode package \
  --format simg \
  --target ebbr \
  --internal-registry \
  --wait \
  --output-format json
```

### Bootc mode

For SA8775P and other boards using bootc container images:

```bash
caib image build manifest.aib.yml \
  --format simg \
  --target ride4_sa8775p_sx_r3 \
  --internal-registry \
  --disk \
  --wait \
  --output-format json
```

## Target Mappings

| Target | Boards | Flash command |
|---|---|---|
| `ebbr` | S32G, TI (J784S4) | `j storage flash oci://{image_uri}` |
| `rcar_s4` | Renesas R-Car S4 | `j storage flash oci://{image_uri}` |
| `ride4_sa8775p_sx` | SA8775P ES2.1 v2/v2.5 | `j storage flash oci://{image_uri}` |
| `ride4_sa8775p_sx_r3` | SA8775P ES2.1 v3 | `j storage flash oci://{image_uri}` |

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

> **Known limitation:** `masked_services` causes build failures when
> the masked service is conditionally enabled by AIB based on an RPM
> in the manifest (e.g., masking `podman-clean-transient.service`
> while `podman` is in the RPM list). Use `disabled_services` instead
> — it prevents the service from running without conflicting with
> AIB's conditional enablement logic.

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
