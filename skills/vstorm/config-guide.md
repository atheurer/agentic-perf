# vstorm Config Construction Guide

vstorm is purely CLI-driven — no config file. The run-file
bundle contains CLI args that the execution handler passes
directly to the vstorm command.

## Run-File Bundle Format

```json
{
  "harness": "vstorm",
  "kubeconfig": "/root/sno/sno-3d/kubeconfig",
  "cli_args": [
    "--containerdisk",
    "--vms=4",
    "--namespaces=1",
    "--cores=1",
    "--memory=1Gi",
    "--wait"
  ]
}
```

### kubeconfig field

Path to the kubeconfig on the controller host. Critical
for user-provided clusters where kubeconfig is not at the
default `~/.kube/config`.

### cli_args

Direct CLI flags for vstorm. Key flags:

| Flag | Description |
|------|-------------|
| `--containerdisk` | Boot from container image (no storage) |
| `--vms=N` | Number of VMs to create |
| `--namespaces=N` | Namespaces to distribute across |
| `--cores=N` | vCPU cores per VM |
| `--memory=SIZE` | Memory per VM (e.g., 1Gi, 512Mi) |
| `--wait` | Wait for VMs to reach Running state |
| `--cloudinit=FILE` | Cloud-init user-data YAML |
| `--env=KEY=VALUE` | Environment variable for cloud-init workload |

## Example: Containerdisk (no storage)

```json
{
  "harness": "vstorm",
  "kubeconfig": "/root/sno/sno-3d/kubeconfig",
  "cli_args": [
    "--containerdisk",
    "--vms=4",
    "--namespaces=1",
    "--cores=1",
    "--memory=512Mi",
    "--wait"
  ]
}
```

## Example: stress-ng Workload

```json
{
  "harness": "vstorm",
  "kubeconfig": "/root/sno/sno-3d/kubeconfig",
  "cli_args": [
    "--containerdisk",
    "--vms=4",
    "--namespaces=1",
    "--cores=2",
    "--memory=2Gi",
    "--wait",
    "--cloudinit=/opt/vstorm/workload/cloudinit-stress-ng-workload.yaml",
    "--env=WORKLOAD_TYPE=cpu-heavy"
  ]
}
```

## Example: Dirty Memory Pages

```json
{
  "harness": "vstorm",
  "kubeconfig": "/root/sno/sno-3d/kubeconfig",
  "cli_args": [
    "--containerdisk",
    "--vms=2",
    "--namespaces=1",
    "--cores=1",
    "--memory=4Gi",
    "--wait",
    "--cloudinit=/opt/vstorm/workload/cloudinit-dirty-mem-pages.yaml",
    "--env=DIRTY_RATE_FRACTION=0.3"
  ]
}
```

## Installation

```bash
git clone https://github.com/gqlo/vstorm.git /opt/vstorm
```

vstorm is a Bash script — no compilation or pip install
needed. Requires `xxd` (from vim-common) for batch ID
generation.

## Execution

The handler runs:
```bash
KUBECONFIG=<path> /opt/vstorm/vstorm <cli_args> 2>&1
```

## Batch ID and Cleanup

Each run generates a 6-character hex batch ID embedded in
all resource names (namespaces, VMs, PVCs). The batch ID
is returned in the run result for cleanup:

```bash
vstorm --delete=<BATCH_ID>
```

## Prerequisites

- OpenShift Virtualization (CNV) operator must be installed
- For containerdisk mode: no storage class needed
- For storage-backed VMs: block-capable StorageClass with
  RWX or RWO access mode
- The `oc` CLI must be available and KUBECONFIG set
