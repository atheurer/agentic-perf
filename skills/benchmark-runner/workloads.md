# benchmark-runner Workloads

benchmark-runner runs application benchmarks inside Kubernetes
pods. It executes as a container (podman) on the controller
host, connecting to the cluster via kubeconfig.

## Available Pod-Based Workloads

### stressng_pod
CPU/memory/kernel stress test using stress-ng.
- **Metrics**: bogo ops/second, CPU utilization
- **Use case**: "How does the CPU perform under stress in a pod?"

### fio_pod
Storage I/O benchmark using fio.
- **Metrics**: IOPS, throughput (MB/s), latency
- **Use case**: "What's the storage performance in pods?"

### uperf_pod
Network throughput/latency using uperf (client-server).
- **Metrics**: throughput (Gb/s), latency (usec)
- **Use case**: "What's the pod-to-pod network performance?"

### sysbench_pod
System performance using sysbench.
- **Metrics**: CPU events/sec, memory throughput
- **Use case**: "General system benchmark in a pod"

### hammerdb_pod_mariadb / hammerdb_pod_postgresql
Database OLTP benchmark using HammerDB.
- **Metrics**: transactions per second (TPS)
- **Use case**: "How fast is the database in a pod?"

### vdbench_pod
Storage I/O using vdbench.
- **Metrics**: IOPS, throughput, latency
- **Use case**: "Storage performance with vdbench patterns"

## Configuration

benchmark-runner uses environment variables, not config files.
The run-file bundle is:

```json
{
  "harness": "benchmark-runner",
  "container_image": "quay.io/benchmark-runner/benchmark-runner:latest",
  "env_vars": {
    "WORKLOAD": "stressng_pod",
    "CLUSTER": "kubernetes",
    "RUN_TYPE": "func_ci",
    "SAVE_ARTIFACTS_LOCAL": "True",
    "DELETE_ALL": "True"
  },
  "artifacts_dir": "/tmp/benchmark-runner-run-artifacts"
}
```

## Key Environment Variables

| Variable | Values | Description |
|----------|--------|-------------|
| WORKLOAD | stressng_pod, fio_pod, etc. | Benchmark to run |
| CLUSTER | kubernetes, openshift | Cluster type |
| RUN_TYPE | func_ci, perf_ci | Quick functional or full perf |
| TIMEOUT | seconds | Override default timeout |
| SCALE | integer | Parallel pod instances |
| SAVE_ARTIFACTS_LOCAL | True/False | Save results locally |
| DELETE_ALL | True/False | Clean up pods after run |

## Execution

The benchmark agent runs benchmark-runner as a podman
container on the controller host:

```bash
podman run --rm \
  -e WORKLOAD="stressng_pod" \
  -e CLUSTER="kubernetes" \
  -e RUN_TYPE="func_ci" \
  -e SAVE_ARTIFACTS_LOCAL="True" \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/benchmark-runner/benchmark-runner:latest
```

Results are saved to `/tmp/benchmark-runner-run-artifacts/`.

## Important Notes

- Set CLUSTER=kubernetes for K3s (not openshift)
- func_ci is a quick functional test; perf_ci is longer
- The container needs --privileged and kubeconfig mounted
- podman must be installed on the controller host
- For K3s, kubeconfig is at ~/.kube/config (set up by
  the K3s installer)
