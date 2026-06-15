# Clusterbuster Workloads

Clusterbuster orchestrates distributed benchmark workloads on
OpenShift/Kubernetes clusters. It deploys pods (or VMs with
OpenShift Virtualization), synchronizes startup across all
instances, and collects results.

## Available Workloads

### cb-cpusoaker
CPU stress — runs Python loops across multiple pods.
Total processes = namespaces × deps-per-namespace × processes.

| Parameter | Default | Description |
|-----------|---------|-------------|
| workloadruntime | 10 | Duration (seconds) |
| namespaces | 1 | Namespaces to create |
| deps_per_namespace | 8 | Deployments per namespace |
| processes | 3 | Processes per deployment |

### cb-fio
Storage I/O using fio in pods. Tests read/write throughput
and IOPS on pod-local ephemeral storage.

| Parameter | Default | Description |
|-----------|---------|-------------|
| workloadruntime | 10 | Duration (seconds) |
| replicas | 4 | Pod replicas |

### cb-uperf
Network performance using uperf client-server pods.

| Parameter | Default | Description |
|-----------|---------|-------------|
| workloadruntime | 30 | Duration (seconds) |
| replicas | 4 | Client-server pairs |
| uperf_msg_size | 8192 | Message size (bytes) |
| uperf_test_type | stream | Traffic: stream or rr |
| uperf_proto | tcp | Protocol: tcp or udp |

### cb-sysbench
Multi-mode system benchmark. Sub-workloads: cpu, memory,
fileio, mutex, threads.

| Parameter | Default | Description |
|-----------|---------|-------------|
| workloadruntime | 10 | Duration (seconds) |
| sysbench_workload | cpu | Sub-workload type |

### cb-memory
Memory allocation stress — allocates, frees, and uses
large chunks of memory across pods.

| Parameter | Default | Description |
|-----------|---------|-------------|
| workloadruntime | 10 | Duration (seconds) |
| replicas | 8 | Pod replicas |
| processes | 3 | Processes per pod |
| memory_size | 512Mi | Allocation per process |

### cb-files
Filesystem metadata stress — creates, reads, and deletes
large numbers of files.

| Parameter | Default | Description |
|-----------|---------|-------------|
| workloadruntime | 10 | Duration (seconds) |
| replicas | 4 | Pod replicas |

### cb-hammerdb
Database benchmark using HammerDB TPC-C. Client and database
run colocated per pod. Not supported on arm64.

| Parameter | Default | Description |
|-----------|---------|-------------|
| workloadruntime | 180 | Duration (seconds) |
| hammerdb_driver | pg | Database: pg or maria |
| hammerdb_benchmark | tpcc | Benchmark type |
| hammerdb_virtual_users | 4 | Virtual users |
| replicas | 2 | Database pod replicas |

### cb-server
Client-server message exchange — measures message passing
throughput between pods.

| Parameter | Default | Description |
|-----------|---------|-------------|
| workloadruntime | 10 | Duration (seconds) |
| replicas | 4 | Client-server pairs |

## Scale Testing

Clusterbuster is designed for scale. Key scaling parameters:

- `namespaces` × `deps_per_namespace` × `processes` = total
  concurrent processes (cpusoaker, memory)
- `replicas` = total pod instances (fio, uperf, server)
- `antiaffinity: true` distributes pods across nodes

For 500+ pod tests, increase namespaces and
deps-per-namespace (see examples/500-pods-per-node-30-nodes).

## Requirements

- OpenShift 4 cluster (uses openshift Python client)
- KUBECONFIG set to valid cluster kubeconfig
- Admin/cluster-admin privileges for namespace management
- Optional: OpenShift Virtualization for VM workloads
