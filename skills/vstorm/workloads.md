# vstorm Workloads

vstorm batch-spawns virtual machines on OpenShift Virtualization
(KubeVirt). It tests VM lifecycle at scale and optionally runs
workloads inside the VMs via cloud-init.

## Available Workloads

### vstorm-containerdisk
Boot VMs from a container disk image. No storage backend
needed. Tests VM provisioning speed and scheduling.

| Parameter | Default | Description |
|-----------|---------|-------------|
| vms | 4 | Number of VMs to create |
| namespaces | 1 | Namespaces to distribute across |
| cores | 1 | vCPU cores per VM |
| memory | 1Gi | Memory per VM |
| wait | true | Wait for VMs to reach Running |

### vstorm-stress-ng
Boot VMs with stress-ng running inside via cloud-init.
Tests VM lifecycle under CPU/memory stress.

| Parameter | Default | Description |
|-----------|---------|-------------|
| vms | 4 | Number of VMs |
| namespaces | 1 | Namespaces |
| cores | 1 | vCPU per VM |
| memory | 1Gi | Memory per VM |
| wait | true | Wait for Running |
| workload_type | memory-heavy | Preset: memory-heavy, cpu-heavy, balanced |

### vstorm-dirty-pages
Boot VMs that continuously dirty a fraction of guest
memory. Tests live migration readiness and memory write
patterns.

| Parameter | Default | Description |
|-----------|---------|-------------|
| vms | 4 | Number of VMs |
| namespaces | 1 | Namespaces |
| cores | 1 | vCPU per VM |
| memory | 1Gi | Memory per VM |
| wait | true | Wait for Running |
| dirty_rate_fraction | 0.5 | Fraction of RAM to dirty (0.1-0.9) |

## Requirements

- OpenShift 4 cluster with OpenShift Virtualization (CNV)
- `oc` CLI authenticated to the cluster (KUBECONFIG)
- For containerdisk mode: no storage needed
- For storage-backed VMs: block-capable StorageClass
- `xxd` command (from vim-common) for batch ID generation

## Scale Testing

vstorm is designed for scale. Key patterns:
- Single node: `--vms=10 --namespaces=1`
- Multi-node: `--vms=100 --namespaces=10 --antiaffinity`
- High density: `--vms=500 --namespaces=50`

VMs are distributed evenly across namespaces.

## Cleanup

Each run generates a unique 6-character hex batch ID.
To clean up: `vstorm --delete=<BATCH_ID>`
To clean all: `vstorm --delete-all`
