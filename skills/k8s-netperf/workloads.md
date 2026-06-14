# k8s-netperf Workloads

k8s-netperf measures network performance between Kubernetes
pods using multiple benchmark drivers. It deploys client and
server pods, runs tests, and reports throughput, latency, and
transaction rates.

## Supported Drivers

| Driver | Profiles | Notes |
|--------|----------|-------|
| netperf | TCP_STREAM, UDP_STREAM, TCP_RR, UDP_RR, TCP_CRR | Default driver, most profiles |
| iperf3 | TCP_STREAM, UDP_STREAM | Simpler, widely available |
| uperf | TCP_STREAM, UDP_STREAM, TCP_RR, UDP_RR | Also used by crucible directly |

## Test Profiles

### Stream tests (throughput)

- **TCP_STREAM** — Unidirectional TCP throughput (Mb/s)
- **UDP_STREAM** — Unidirectional UDP throughput (Mb/s)

### Request-response tests (latency)

- **TCP_RR** — TCP request-response transactions per second
- **UDP_RR** — UDP request-response transactions per second
- **TCP_CRR** — TCP connect + request-response (measures
  connection setup overhead)

## Network Scenarios

Controlled via CLI flags, not the config file:

| Scenario | Flag | Description |
|----------|------|-------------|
| Pod network | (default) | Standard pod-to-pod, cross-node |
| Host network | `--hostNet` | Pods use host network namespace |
| Same node | `--local` | Force client/server co-location |
| Cross-AZ | `--across` | Force different availability zones |
| Service | `service: true` in config | Route through K8s Service ClusterIP |

## Key Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| driver | string | netperf | Benchmark tool: netperf, iperf3, uperf |
| profiles | list | [TCP_STREAM] | Test profiles to run |
| duration | int | 30 | Seconds per test |
| samples | int | 3 | Repetitions per profile |
| messagesize | int | 1024 | Datagram size in bytes |
| parallelism | int | 1 | Concurrent streams |
| service | bool | false | Route through K8s Service |
| hostNet | bool | false | Use host networking |
| local | bool | false | Same-node placement |
| across | bool | false | Cross-AZ placement |

## Output

Results are printed as ASCII tables to stdout:

- **Stream Results** — Throughput in Mb/s
- **RR Results** — Transaction rate in OP/s
- **Latency** — 99th percentile in usec
- **Loss** — TCP retransmissions or UDP loss percent

Also writes a CSV file with all results. Pass `--json` for
JSON output or `--search <url>` for OpenSearch indexing.

## K8s Requirements

- Namespace `netperf` with service account `netperf`
- Host network tests need additional RBAC/SCC
- Works on vanilla K8s (K3s, KinD) and OpenShift
- Binary reads kubeconfig from `~/.kube/config` or `KUBECONFIG`
