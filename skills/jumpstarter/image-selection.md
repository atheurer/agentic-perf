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
| `release` | Build release | `nightly`, `monthly`, `monthly/autosd10-202608010205` |

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

## EBBR (Embedded Base Boot Requirements)

EBBR is a boot specification, not an image name or type. It
describes how the board's firmware boots the OS — using UEFI
standards for embedded systems. Multiple board families use
EBBR, including NXP S32G and Renesas R-Car S4; others will
in the future.

When a user mentions "EBBR" (in directives, description, or
conversation), they are indicating they want an EBBR-compatible
image for their target board. This is valid intent but does
not map directly to `image_name` or `image_type`:

- The resolver selects a manifest target for the board, then
  matches the requested `image_name` and `image_type` there
- Available variants depend on the target and release; use the
  resolver's manifest results instead of assuming every
  combination is published

Users may reference EBBR anywhere — in directives (e.g.,
`image_name: "ebbr"`), in the ticket description, or in
conversation. However it appears, "ebbr" is not a catalog
variant and must not be passed literally to the image
resolver. Instead:

1. Recognize the user's intent to use an EBBR-compatible
   image for their target board
2. Use the appropriate `image_name` for the requested mode
   (e.g., `ps` for ostree, `qa` for regular/package)
3. Preserve whatever `image_type` the user specified
4. If no `image_type` is specified, use the existing defaults
   and run metadata for the ticket

If the user explicitly asks for "EBBR ostree" or "EBBR
package mode", map accordingly:

| User says | image_name | image_type |
|---|---|---|
| "EBBR", "EBBR image" | *(default for board)* | *(default for board)* |
| "EBBR ostree" | `ps` | `ostree` |
| "EBBR package", "EBBR regular" | `qa` | `regular` |

## Release Path Formats

The `release` directive controls which build the image resolver fetches.
Bare values resolve to the latest available build in that stream;
qualified paths pin to a specific dated build.

| Value | Resolves to |
|---|---|
| `nightly` | Latest nightly build |
| `monthly` | Latest monthly build (any month) |
| `monthly/autosd10-202608010205` | Specific August 2026 monthly build |
| `latest-RHIVOS-2` | Latest RHIVOS 2.x release |
| `latest-RHIVOS-2.1-202607240103` | Specific RHIVOS 2.1 build |

### Extracting release dates from natural language

When the user references a specific month or date, the `release`
directive MUST include the date qualifier — otherwise the image
resolver picks the latest build, ignoring the user's intent.

| User says | release directive |
|---|---|
| "latest monthly" / "monthly image" | `monthly` |
| "August monthly" / "monthly from August" | `monthly/autosd10-202608` (partial match — resolver finds the closest build) |
| "June 2026 nightly" / "nightly from June 15" | `nightly` with a note that specific nightly dating is not supported |
| "the 202608010205 build" | `monthly/autosd10-202608010205` |
| "monthly build 202607" | `monthly/autosd10-202607` |

The image resolver supports partial datestamp matching: if the
exact path 404s, it searches the directory listing for entries
containing the datestamp. So `monthly/autosd10-202608` will
match `monthly/autosd10-202608010205`.

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

To target a specific board instance, use `name=<exporter>`:
- `name=nxp-s32g-vnp-rdb3-01`
- `name=qti-snapdragon-ride4-sa8775p-23`

When a specific board is requested but unavailable, the
system reports why (leased, offline, disabled) and suggests
alternative boards of the same type. Do not retry
indefinitely — escalate to the user if the board is in use.

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
