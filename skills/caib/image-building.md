# Custom Image Building with CAIB

CAIB (Cloud Automotive Image Builder) produces a Jumpstarter-compatible
AutoSD image from an AIB manifest. Use the `image_build` ticket directive for
image content that must exist at boot: RPMs, repositories, services, kernel
packages, or a pinned AIB image. The image-builder agent runs before hardware
acquisition, so a failed build does not consume a lease.

## Prerequisites and authentication

Local installation requires the CAIB CLI, a working `jmp login`/Jumpstarter
client configuration, network access to the CAIB service, and a CAIB service
token at `~/.agentic-perf/secrets/caib/token` (or the configured
`AGENTIC_PERF_SECRETS/caib/token`). The deployment also needs registry
credentials with pull permission for exporters and push permission for the
build result. Put push credentials in
`secrets/caib/registry-auth.json`; never place tokens in a manifest or commit
them. In the container image, CAIB is installed during the Containerfile
build; verify with `caib --help` because that installation is warning-only.

```bash
jmp login
caib --help
CAIB_SECRETS_DIR="${AGENTIC_PERF_SECRETS:-$HOME/.agentic-perf/secrets}"
test -s "$CAIB_SECRETS_DIR/caib/token"
```

The provider pushes `<push_registry>:<build-name>`. Quay tags are assigned an
expiry (14 days by default, configurable with `tag_expiration_days`); retain
or promote the tag when it must survive cleanup.

## Targets and modes

The board selector resolves as follows: `board-type=nxp-s32g-vnp-rdb3` and
`board-type=renesas-rcar-s4` map to `ebbr`; `board-type=qc8775` maps to
`ride4_sa8775p_sx`; unknown selectors default to `ebbr`. Override `target`
when the provider mapping is not sufficient.

Build mode and target are independent. Package mode is the default and uses
`caib image build-dev --mode package`; it leaves a mutable `dnf` root. Bootc
mode uses `caib image build --disk` (or `build-dev --mode image`) and produces
an OSTree/rpm-ostree image. Use package mode for post-flash RPM/configuration
and bootc for production-like atomic images.

## Valid submission examples

These are CLI equivalents of the provider's commands. `--wait` returns only
after the remote build finishes; `--output-format json` makes the result
machine-readable.

```bash
# Mutable package image
caib image build-dev manifest.aib.yml --mode package --format simg \
  --target ebbr --push quay.io/example/agentic-perf --wait \
  --output-format json --timeout 60 --ttl 168h

# Bootc/OSTree image
caib image build manifest.aib.yml --disk --format simg \
  --target ride4_sa8775p_sx --push quay.io/example/agentic-perf \
  --wait --output-format json --timeout 60 --ttl 168h
```

The agent derives a short build name from target, image mode, UTC timestamp,
and ticket ID. Store the returned `diskImage` or the OCI URL in the ticket;
flash it with `j storage flash oci://<registry>/<image>:<tag>`.

## Manifest and supported customizations

The provider starts with required SSH, networking, time, diagnostics, and
Podman packages, enables `sshd`, `NetworkManager`, and `chronyd`, and adds
your customizations. A valid minimal manifest is:

```yaml
name: custom-test-image
content:
  rpms:
    - openssh-server
    - NetworkManager
    - chrony
    - iproute
  systemd:
    enabled_services:
      - sshd.service
      - NetworkManager.service
      - chronyd.service
network:
  dynamic: {}
image:
  image_size: 8 GiB
  sealed: false
```

Supported `customizations` keys are `rpms`, `repos`, `enabled_services`,
`disabled_services`, and `kernel` (a package string or AIB kernel object).
`masked_services` is accepted for compatibility but is converted to
`disabled_services`; it is not an AIB manifest key. Keep `network.dynamic: {}`
for NetworkManager DHCP. For QC8775 kernel overrides, include matching DTBs
and Qualcomm kernel modules. EBBR uses `kernel-automotive`; QC8775 nightly
images use `kernel-ivos-qualcomm` plus its matching modules/DTBs.

## Ticket lifecycle and outputs

Triage sets `custom_fields.image_build`; the orchestrator enters
`building_image`, and the image-builder emits `agent_started`, then
`build_complete` or `build_failed`, followed by `agent_finished`. The result
is stored in `custom_fields.image_build_result`:

```json
{
  "provider": "caib",
  "build_name": "ebbr-reg-202609011200-TICKET-1",
  "image_url": "quay.io/example/agentic-perf:...",
  "status": "completed",
  "error": "",
  "details": {}
}
```

Success transitions to `awaiting_hardware`; failure records the error and
transitions to `awaiting_customer_guidance`. Inspect ticket comments/events
and the CAIB build record (`caib image show <build-name> --output-format json`)
to diagnose progress.

## Recovery, cleanup, and choosing system_config

After a failure, correct the manifest, credentials, target, or registry
permissions and retry the ticket/build. Cancel abandoned remote builds with
`caib image cancel <build-name>` and remove expired local manifests; CAIB-side
build records and tags are cleaned by their TTL. Do not hold or acquire
hardware while waiting for CAIB.

Use `image_build` when the change belongs in the flashed base image or must be
reproducible before allocation. Use post-flash `system_config` for a one-off
runtime change such as installing an RPM from a lab server or changing config
after boot. The two directives are intentionally distinct; do not combine
them unless the request explicitly needs both.
