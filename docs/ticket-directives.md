# Ticket Directives Reference

Directives are operational instructions in a ticket's
`custom_fields.directives` that control how the system runs
the investigation. They tell agents what infrastructure to
use, what image to flash, and how to execute the benchmark.

Directives you provide are passed through to agents as-is.
Omitted directives use defaults — the system infers what
it can from the ticket description.

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

### Via CLI

```bash
agentic-perf submit "Boot time baseline on R-Car S4" \
  --directive harness=boot-time \
  --directive image_version=AutoSD-10 \
  --directive board_selector=board-type=renesas-rcar-s4
```

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
"board_selector": "device=nxp-s32g-vnp-rdb3-01"
```

### Resource Provider

| Directive | Description | Examples |
|---|---|---|
| `resource_provider` | Which provider to use | `jumpstarter`, `quads`, `aws` |

### Harness Behavior

| Directive | Description | Examples |
|---|---|---|
| `on_existing_install` | How to handle existing harness | `reinstall`, `skip` |
| `user_pre_run_approval` | Require approval before benchmark | `true`, `false` |
| `host_cleanup` | Host cleanup policy | `skip` |
| `firewall_policy` | Firewall handling | `flush` |
| `skip_teardown` | Keep hosts after run | `true` |

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
```

The provisioning agent applies these operations via SSH after
the platform agent has flashed and verified the board, but
before the benchmark starts. Results are recorded in
`system_config_applied` and `system_config_errors` on the ticket.

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
