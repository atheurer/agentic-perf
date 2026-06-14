# k8s-netperf Config Construction Guide

k8s-netperf uses a YAML config file plus CLI flags. The
run-file bundle for agentic-perf wraps both into a single
JSON structure.

## Run-File Bundle Format

```json
{
  "harness": "k8s-netperf",
  "driver": "netperf",
  "cli_flags": ["--netperf"],
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

### config section

The `config.tests` array maps directly to k8s-netperf's
YAML config (v2 format). Each entry is a single-key dict
where the key is the test name and the value contains:

| Field | Type | Description |
|-------|------|-------------|
| parallelism | int | Concurrent streams |
| profile | string | TCP_STREAM, UDP_STREAM, TCP_RR, etc. |
| duration | int | Test duration in seconds |
| samples | int | Number of repetitions |
| messagesize | int | Datagram size in bytes |
| service | bool | Route through K8s Service (optional) |

### cli_flags section

CLI flags that control the driver and network scenario:

- `--netperf` / `--iperf3` / `--uperf` — driver selection
- `--hostNet` — host network mode
- `--local` — same-node placement
- `--across` — cross-AZ placement

### driver field

String indicating which driver is selected. Used for
validation (each driver supports a subset of profiles).

## Example: Multi-Profile TCP Test

```json
{
  "harness": "k8s-netperf",
  "driver": "netperf",
  "cli_flags": ["--netperf"],
  "config": {
    "tests": [
      {
        "TCP_STREAM_1024": {
          "parallelism": 1,
          "profile": "TCP_STREAM",
          "duration": 30,
          "samples": 5,
          "messagesize": 1024
        }
      },
      {
        "TCP_RR_1024": {
          "parallelism": 1,
          "profile": "TCP_RR",
          "duration": 30,
          "samples": 5,
          "messagesize": 1024,
          "burst": 1
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

## Execution

The benchmark agent:
1. Installs k8s-netperf binary on the controller host
2. Writes the config YAML from `config.tests`
3. Creates the `netperf` namespace and service account
4. Runs: `k8s-netperf --config /tmp/netperf-<uuid>.yml <cli_flags>`
5. Collects CSV/stdout output

## Installation

```bash
curl -Ls https://raw.githubusercontent.com/cloud-bulldozer/k8s-netperf/main/hack/install.sh | sh
```

Installs to `~/.local/bin/k8s-netperf`. Container image
also available at `quay.io/cloud-bulldozer/k8s-netperf`.

## K8s Setup

```bash
kubectl create ns netperf
kubectl create sa netperf -n netperf
```

For host network tests (OpenShift):
```bash
oc adm policy add-scc-to-user hostnetwork -z netperf
```

For vanilla K8s, create equivalent RBAC bindings.

## Driver-Profile Compatibility

| Profile | netperf | iperf3 | uperf |
|---------|---------|--------|-------|
| TCP_STREAM | yes | yes | yes |
| UDP_STREAM | yes | yes | yes |
| TCP_RR | yes | no | yes |
| UDP_RR | yes | no | yes |
| TCP_CRR | yes | no | no |
