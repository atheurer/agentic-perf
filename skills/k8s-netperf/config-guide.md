# k8s-netperf Config Construction Guide

k8s-netperf uses a YAML config file plus CLI flags. The
run-file bundle for agentic-perf wraps both into a single
JSON structure. The execution handler converts this to the
correct YAML format automatically.

## Run-File Bundle Format

```json
{
  "harness": "k8s-netperf",
  "driver": "netperf",
  "cli_flags": ["--netperf", "--local"],
  "config": {
    "tests": [
      {
        "TCP_STREAM_1024": {
          "parallelism": 1,
          "profile": "TCP_STREAM",
          "duration": 30,
          "samples": 3,
          "messagesize": 1024
        }
      }
    ]
  }
}
```

## CRITICAL: YAML Format

k8s-netperf uses Go's yaml.v3 parser. The execution handler
converts the run-file to **v1 flat-dict YAML** — the only
format that reliably works:

```yaml
---
TCP_STREAM_1024:
  parallelism: 1
  profile: "TCP_STREAM"
  duration: 30
  samples: 3
  messagesize: 1024
```

**DO NOT** use v2 `tests:` array format — it causes
"unknown netperf profile" errors due to YAML indentation
parsing differences between the Go parser and Python's
yaml.dump.

The execution handler does this conversion automatically.
You only need to provide the JSON run-file bundle above.

## config.tests entries

Each entry in `config.tests` is a single-key dict:
- Key: test name (arbitrary, e.g., `TCP_STREAM_1024`)
- Value: dict with test parameters

| Field | Type | Description |
|-------|------|-------------|
| parallelism | int | Concurrent streams |
| profile | string | TCP_STREAM, UDP_STREAM, TCP_RR, etc. |
| duration | int | Test duration in seconds |
| samples | int | Number of repetitions |
| messagesize | int | Datagram size in bytes |
| service | bool | Route through K8s Service (optional) |

## cli_flags

CLI flags that control the driver and network scenario.
**Always include the driver flag** (`--netperf`, `--iperf3`,
or `--uperf`).

- `--netperf` / `--iperf3` / `--uperf` — driver selection
- `--local` — same-node placement (required for single-node)
- `--hostNet` — host network mode
- `--across` — cross-AZ placement
- `--json` — JSON output (added automatically by handler)

## K8s Setup (done automatically by handler)

The execution handler performs these steps before running:

1. Labels all nodes with `node-role.kubernetes.io/worker`
   (K3s nodes lack this label by default; k8s-netperf
   requires it to schedule pods)
2. Cleans up any existing `netperf` namespace
3. Creates fresh `netperf` namespace and service account

## Example: Multi-Profile Test

```json
{
  "harness": "k8s-netperf",
  "driver": "netperf",
  "cli_flags": ["--netperf", "--local"],
  "config": {
    "tests": [
      {
        "TCP_STREAM_1024": {
          "parallelism": 1,
          "profile": "TCP_STREAM",
          "duration": 30,
          "samples": 3,
          "messagesize": 1024
        }
      },
      {
        "TCP_RR_1024": {
          "parallelism": 1,
          "profile": "TCP_RR",
          "duration": 30,
          "samples": 3,
          "messagesize": 1024
        }
      }
    ]
  }
}
```

## Example: iperf3 with Host Network

```json
{
  "harness": "k8s-netperf",
  "driver": "iperf3",
  "cli_flags": ["--iperf3", "--hostNet"],
  "config": {
    "tests": [
      {
        "TCP_STREAM_8192": {
          "parallelism": 2,
          "profile": "TCP_STREAM",
          "duration": 60,
          "samples": 3,
          "messagesize": 8192
        }
      }
    ]
  }
}
```

## Installation

```bash
curl -Ls https://raw.githubusercontent.com/cloud-bulldozer/k8s-netperf/main/hack/install.sh | sh
```

Installs to `~/.local/bin/k8s-netperf`. Container image
also available at `quay.io/cloud-bulldozer/k8s-netperf`.

## Driver-Profile Compatibility

| Profile | netperf | iperf3 | uperf |
|---------|---------|--------|-------|
| TCP_STREAM | yes | yes | yes |
| UDP_STREAM | yes | yes | yes |
| TCP_RR | yes | no | yes |
| UDP_RR | yes | no | yes |
| TCP_CRR | yes | no | no |
