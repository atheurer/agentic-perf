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

## VM-Based Workloads (require CNV/OpenShift Virtualization)

These run benchmarks inside KubeVirt virtual machines instead
of pods. Require the OpenShift Virtualization (CNV) operator.

### stressng_vm
Same stress-ng workload as stressng_pod but inside a VM.
- **Requires**: CNV operator installed

### fio_vm
Same fio workload as fio_pod but inside a VM.
- **Requires**: CNV operator installed

### uperf_vm
Same uperf workload as uperf_pod but inside VMs.
- **Requires**: CNV operator installed

### bootstorm_vm
Rapidly provisions multiple VMs to measure boot time.
- **Metrics**: VM boot time, scheduling latency
- **Requires**: CNV operator installed

## Configuration

benchmark-runner uses environment variables, not config files.
The run-file bundle is:

```json
{
  "harness": "benchmark-runner",
  "container_image": "quay.io/benchmark-runner/benchmark-runner:latest",
  "env_vars": {
    "WORKLOAD": "stressng_pod",
    "CLUSTER": "openshift",
    "RUN_TYPE": "func_ci",
    "SAVE_ARTIFACTS_LOCAL": "True",
    "DELETE_ALL": "True"
  },
  "artifacts_dir": "/tmp/benchmark-runner-run-artifacts",
  "kubeconfig_path": "/root/.kube/config",
  "kubeadmin_password_path": "/root/sno/sno-3c/kubeadmin-password"
}
```

The `kubeconfig_path` and `kubeadmin_password_path` point to
files on the controller host. The execute handler reads the
password from the remote file and injects it as an env var.

Get default paths from `get_execution_config("benchmark-runner")`.
If the user specifies a cluster name (e.g., "use sno-3d"), the
paths follow the pattern `/root/sno/<cluster>/kubeconfig` and
`/root/sno/<cluster>/kubeadmin-password`. Override the defaults
in the run-file with the user's cluster paths.
```

## Key Environment Variables

| Variable | Values | Description |
|----------|--------|-------------|
| WORKLOAD | stressng_pod, fio_pod, etc. | Benchmark to run |
| CLUSTER | kubernetes, openshift | Cluster type |
| RUN_TYPE | func_ci, perf_ci | func_ci for SNO/small clusters (low resources); perf_ci needs large clusters (60+ CPUs, 75+ GB RAM per pod) |
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

- Storage workloads (fio_pod, vdbench_pod, hammerdb_pod_*) default
  to ODF PVCs. On clusters without ODF, use the `_ephemeral`
  variant of the workload name instead (e.g.,
  `hammerdb_pod_postgres_ephemeral` instead of
  `hammerdb_pod_postgres`). Alternatively set ODF_PVC=False.
- HammerDB postgres name is `hammerdb_pod_postgres` (not
  `postgresql`). Valid variants: `hammerdb_pod_postgres`,
  `hammerdb_pod_postgres_lso`, `hammerdb_pod_postgres_ephemeral`
- benchmark-runner REQUIRES OpenShift — set CLUSTER=openshift.
  The CLUSTER=kubernetes mode does not actually work (the code
  always calls `oc login` which requires KUBEADMIN_PASSWORD)
- func_ci is for SNO and small clusters — pods request minimal
  resources. perf_ci requests 60 CPUs and 75 GB RAM per pod,
  which will fail to schedule on single-node clusters. Default
  to func_ci unless the user explicitly asks for perf_ci on a
  large cluster
- The container needs --privileged and kubeconfig mounted
- podman must be installed on the controller host
- KUBEADMIN_PASSWORD is read from a file on the controller —
  the path comes from get_execution_config. Do NOT put the
  password in the run-file env_vars directly
- The controller host must have network access to the OCP
  API server (e.g., api.sno-3c.example.com:6443)
