# Ticket Directives Reference

Directives are operational instructions in a ticket's
`custom_fields.directives` that control how the system runs
the investigation. They tell agents what infrastructure to
use, what image to flash, and how to execute the benchmark.

Directives you provide are stored under `custom_fields.directives` and take
precedence over triage's inferred values. Omitted directives use defaults or
are inferred from the description. The CLI has no `--directive` option; use
the API example below (or describe the requirement in the ticket text).

### Field placement and precedence

| Location | Purpose | Type/ownership |
|---|---|---|
| `custom_fields.directives` | User-controlled harness, image, hardware, and HITL settings | Object; user input wins over triage inference |
| `custom_fields.resource_provider` | Pipeline resource-provider selection | String; `jumpstarter`, `quads`, `aws`, or `user_provided` |
| `custom_fields.fleet_investigation` | Pipeline fleet state and enablement | Object; `enabled` is boolean, progress is agent-owned |
| `custom_fields.image_build` | Pipeline custom-image build request and result | Object; user values merge with triage values |
| `custom_fields.system_config` | Post-flash configuration operations | Array; deterministic provisioning input |
| `custom_fields.anomaly_context` | Observed/baseline investigation data | Object; caller-provided |
| `custom_fields.scoped_context` | Supplemental per-agent context | Object; keys are `shared`, `resource`, `provision`, `benchmark`, `review` |

Pipeline fields must not be put inside `directives`. Triage writes derived
`parsed_specs`, `hypothesis`, `benchmark_suite`, `required_hosts`,
`directives`, and (when present) `scoped_context`. User directives override
same-named inferred directives; `image_version` and `serial_capture` may
also be promoted from top-level custom fields for compatibility.

## Submitting Directives

### Via API

```json
{
  "summary": "Boot time baseline on R-Car S4",
  "description": "Measure boot time with AutoSD-10.",
  "custom_fields": {
    "directives": {
      "harness": "boot-time",
      "image_version": "AutoSD-10",
      "board_selector": "board-type=renesas-rcar-s4"
    }
  }
}
```

There is no supported CLI syntax for structured directives. Submit this JSON
to `POST /api/v1/tickets` (see the [E2E guide](e2e-testing.md) for the API
workflow), or use `python3 cli.py submit` with the directive values in the
natural-language description.

## Available Directives

### Benchmark

| Directive | Description | Examples |
|---|---|---|
| `harness` | Benchmark harness to use | `boot-time`, `arcaflow-plugins`, `crucible` |
| `endpoint_type` | Endpoint type for the harness | `remotehosts`, `kube` |

### Image Selection (Jumpstarter)

| Directive | Description | Examples |
|---|---|---|
| `image_version` | OS image stream | `AutoSD-10`, `RHIVOS-2` |
| `image_name` | Build variant | `ps`, `qa`, `fusa-minimal` |
| `image_type` | Image format | `regular`, `ostree` |
| `release` | Build release | `nightly`, `monthly` (resolves to latest) |
| `image_server` | Override image server URL | `https://autosd.sig.centos.org/` |

**Common image combinations:**

| Description | image_name | image_type |
|---|---|---|
| Bootc / OCI container image | `ps` | `ostree` |
| Package-based / traditional RPM | `qa` | `regular` |
| Functional safety minimal | `fusa-minimal` | `ostree` |

If you describe the image mode in your ticket summary or
description (e.g., "bootc image", "ostree", "package-based"),
the triage agent reads the image selection skill
(`skills/jumpstarter/image-selection.md`) and infers the
correct directive values automatically.

### Hardware Selection (Jumpstarter)

| Directive | Description | Examples |
|---|---|---|
| `board_selector` | Jumpstarter label selector for board pool | `board-type=renesas-rcar-s4` |

**Board selector values:**

| Board | Selector |
|---|---|
| Renesas R-Car S4 | `board-type=renesas-rcar-s4` |
| NXP S32G VNP RDB3 | `board-type=nxp-s32g-vnp-rdb3` |
| Qualcomm SA8775P (Ride4) | `board-type=qc8775` |
| Qualcomm SA8650P | `board-type=qc8650` |

To target a specific board instance:

```json
{
  "custom_fields": {
    "directives": {
      "board_selector": "device=nxp-s32g-vnp-rdb3-01"
    }
  }
}
```

### Resource Provider

| Directive | Description | Examples |
|---|---|---|
| `resource_provider` | Which provider to use; prefer top-level `custom_fields.resource_provider` | `jumpstarter`, `quads`, `aws`, `user_provided` |

### Harness Behavior

| Directive | Description | Examples |
|---|---|---|
| `on_existing_install` | How to handle existing harness | `reinstall`, `update`, `skip`, `ask_user` (default is provider/agent choice) |
| `user_pre_run_approval` | Require approval before benchmark | `true`, `false` |
| `host_cleanup` | Host cleanup policy | `required` (default), `skip` |
| `firewall_policy` | Firewall handling | `flush` |
| `skip_teardown` | Keep hosts after run | `true` |

### Fleet Investigation

| Field | Description | Values |
|---|---|---|
| `fleet_investigation` | Test across all boards of a type | `{"enabled": true}` |

Fleet investigation iterates across every available device matching
the `board_selector`, running the benchmark on each board individually
and comparing results. The system automatically:

- Excludes already-tested boards when acquiring hardware
- Records failures as data points (doesn't stop on one bad board)
- Converges when all available devices have been tested
  (hard exhaustion) or remaining devices are unavailable
  (soft exhaustion)

Set in `custom_fields` (not `directives`) since it controls
pipeline behavior, not harness configuration:

```json
{
  "custom_fields": {
    "fleet_investigation": {"enabled": true},
    "directives": {
      "harness": "boot-time",
      "board_selector": "board-type=nxp-s32g-vnp-rdb3"
    },
    "samples": 10
  }
}
```

### Custom Image Build

| Field | Description | Values |
|---|---|---|
| `image_build` | Build a custom OS image before provisioning | See below |

When `image_build` is set in `custom_fields`, the pipeline builds
a custom AutoSD image before acquiring hardware. The build runs
and the resulting image
replaces the default nightly for flashing.

Set in `custom_fields` (not `directives`):

```json
{
  "custom_fields": {
    "directives": {
      "harness": "boot-time",
      "board_selector": "board-type=nxp-s32g-vnp-rdb3"
    },
    "samples": 10,
    "image_build": {
      "target": "ebbr",
      "customizations": {
        "rpms": ["custom-package"],
        "repos": [{"name": "copr", "baseurl": "https://..."}],
        "masked_services": ["unwanted.service"],
        "enabled_services": ["custom.service"]
      }
    }
  }
}
```

Or describe it naturally — the triage agent will detect fleet
requests like "test boot time across all S32G boards" and set
the flag automatically.

Progress is tracked in `custom_fields.fleet_investigation`:

```json
{
  "fleet_investigation": {
    "enabled": true,
    "tested_hosts": [
      {"host_id": "board-01", "status": "completed", "kpis": {"boot_ms": 1200}},
      {"host_id": "board-02", "status": "partial", "failure_reason": "..."}
    ],
    "fleet_exhausted": {"hard": true}
  }
}
```
Or describe it naturally — the triage agent will detect custom
build requirements and set the directives.

### Boot-Time Specific

| Directive | Description | Examples |
|---|---|---|
| `jumpstarter_serial` | Enable serial capture during boot test | `true`, `false` |
| `ssh_password` | Override default SSH password | `password` |
| `system_config` | Post-flash system configuration operations | See below |

### System Configuration

The `system_config` directive applies structured configuration
changes to provisioned hosts before the benchmark starts. This
runs deterministically in code — no LLM is involved.

Supported actions:

| Action | Fields | Description |
|---|---|---|
| `write_file` | `path`, `content` | Write content to a file (creates parent dirs) |
| `run_command` | `command`, `timeout` (optional, default 30s) | Execute a shell command |

Example — delay a systemd service:

```json
{
  "custom_fields": {
    "system_config": [
    {
      "action": "write_file",
      "path": "/etc/systemd/system/podman-clean-transient.service.d/delay.conf",
      "content": "[Service]\nExecStartPre=/usr/bin/sleep 60"
    },
    {
      "action": "run_command",
      "command": "systemctl daemon-reload"
    }
    ]
  }
}
```

The provisioning agent applies these operations via SSH after
the platform agent has flashed and verified the board, but
before the benchmark starts. Results are recorded in
`system_config_applied` and `system_config_errors` on the ticket.

### Scoped context and convergence

`scoped_context` is supplemental text, not a replacement for directives or
parsed specifications. Valid keys are `shared`, `resource`, `provision`,
`benchmark`, and `review`; each agent receives `shared` plus its own section.
User-authored fenced blocks in the description (`agent:benchmark`, for
example) are preserved verbatim and take precedence over supplemental
context.

```json
{
  "custom_fields": {
    "scoped_context": {
      "shared": "Use the lab's 25 GbE network.",
      "resource": "Reserve one controller and one client.",
      "benchmark": "Run exactly ten samples.",
      "review": "Compare p99 latency to the supplied baseline."
    },
    "convergence_criteria": {
      "metric": "throughput_gbps",
      "direction": "higher_is_better",
      "threshold": 0.05
    }
  }
}
```

`convergence_criteria` is pipeline-owned evaluation input; it belongs at the
top level of `custom_fields`, not in `directives`. Agent-generated progress
and results are written back to custom fields and should not be supplied as
initial directives.

### HITL Control

| Directive | Description | Examples |
|---|---|---|
| `disable_hitl_timeout` | Don't timeout waiting for user input | `true` |
| `review_mode` | Review agent behavior | `interactive` (waits for user) |

## Anomaly Context

For investigation tickets, `custom_fields.anomaly_context`
provides the anomaly details:

```json
{
  "custom_fields": {
    "anomaly_context": {
      "metric": "boot.phase.initrd_ms",
      "observed": 2792,
      "baseline": 100,
      "board_type": "nxp_s32g",
      "os_version": "AutoSD-10"
    },
    "directives": {
      "harness": "boot-time",
      "image_version": "AutoSD-10",
      "board_selector": "board-type=nxp-s32g-vnp-rdb3"
    }
  }
}
```

## Examples

### Basic boot-time test

```json
{
  "summary": "Measure boot time on R-Car S4",
  "description": "Collect 50 boot time samples.",
  "custom_fields": {
    "directives": {
      "harness": "boot-time",
      "image_version": "AutoSD-10",
      "board_selector": "board-type=renesas-rcar-s4"
    }
  }
}
```

### Bootc image on a specific board

```json
{
  "summary": "Boot time with bootc image on NXP S32G board 01",
  "description": "Test boot time with the bootc/ostree image variant.",
  "custom_fields": {
    "directives": {
      "harness": "boot-time",
      "image_version": "AutoSD-10",
      "image_name": "ps",
      "image_type": "ostree",
      "board_selector": "device=nxp-s32g-vnp-rdb3-01"
    }
  }
}
```

### Regression investigation

```json
{
  "summary": "Investigate boot time regression on SA8775P",
  "description": "Boot time increased from 26s to 42s.",
  "custom_fields": {
    "anomaly_context": {
      "metric": "boot.phase.total_ms",
      "observed": 42000,
      "baseline": 26000,
      "board_type": "qc8775",
      "os_version": "RHIVOS-2"
    },
    "directives": {
      "harness": "boot-time",
      "image_version": "RHIVOS-2",
      "board_selector": "board-type=qc8775"
    }
  }
}
```

### Fleet investigation across all boards

```json
{
  "summary": "Fleet boot time comparison across all S32G boards",
  "description": "Run 10 boot time samples on every available S32G board and compare results.",
  "custom_fields": {
    "fleet_investigation": {"enabled": true},
    "directives": {
      "harness": "boot-time",
      "board_selector": "board-type=nxp-s32g-vnp-rdb3"
    },
    "samples": 10
  }
}
```

### Let the system decide

```json
{
  "summary": "CPU performance baseline on R-Car S4",
  "description": "Run appropriate CPU benchmarks on an R-Car S4 board with AutoSD-10."
}
```

When directives are omitted, the triage agent infers what it
can from the description. The system will ask for clarification
if the request is ambiguous.
