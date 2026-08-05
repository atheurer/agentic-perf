# Image Selection for Jumpstarter Boards

When a user submits a ticket, they may specify image parameters
explicitly via directives or describe them in natural language.
Use this guide to resolve image directives when the user's
intent is clear but the directives are incomplete.

## Available Directives

| Directive | Description | Examples |
|---|---|---|
| `image_version` | OS image stream | `AutoSD-10`, `RHIVOS-2` |
| `image_name` | Build variant | `ps`, `qa`, `fusa-minimal` |
| `image_type` | Image format | `regular`, `ostree` |
| `board_selector` | Jumpstarter board label selector | `board-type=nxp-s32g-vnp-rdb3` |
| `release` | Build release | `nightly`, `monthly/autosd10-202608010205` |

## Image Mode Mapping

Users may describe the image mode in natural language. Map
these terms to the correct `image_name` and `image_type`:

| User says | image_name | image_type | Notes |
|---|---|---|---|
| "bootc", "bootc image" | `ps` | `ostree` | OCI bootc container image |
| "ostree", "ostree image" | `ps` | `ostree` | Same as bootc |
| "package", "rpm", "regular" | `qa` | `regular` | Traditional package-based |
| "qa image", "qa build" | `qa` | `regular` | QA test variant |
| "fusa", "fusa-minimal" | `fusa-minimal` | `ostree` | Functional safety minimal |

## Defaults

When the user does not specify an image mode:
- Default `image_name`: `ps`
- Default `image_type`: `regular`

These can be overridden via `jumpstarter_images` config.

## Board Type Mapping

Users may refer to boards by common names. The `board_selector`
directive uses Jumpstarter label syntax:

| User says | board_selector |
|---|---|
| "R-Car S4", "Renesas S4" | `board-type=renesas-rcar-s4` |
| "S32G", "NXP S32G" | `board-type=nxp-s32g-vnp-rdb3` |
| "SA8775P", "Qualcomm Ride4", "8775" | `board-type=qc8775` |
| "SA8650P", "8650" | `board-type=qc8650` |

To target a specific board instance, use `device=<name>`:
- `device=nxp-s32g-vnp-rdb3-01`
- `device=qti-snapdragon-ride4-sa8775p-23`

## OS Image Servers

| OS | Server |
|---|---|
| AutoSD | `https://autosd.sig.centos.org/` |
| RHIVOS | `https://rhivos.auto-toolchain.redhat.com/in-vehicle-os` |

The image resolution code selects the correct server
automatically when `run_metadata` is available (webhook
tickets). For manual tickets, the server is determined by
`image_version` — `AutoSD-*` uses the AutoSD server,
`RHIVOS-*` uses the RHIVOS server.
