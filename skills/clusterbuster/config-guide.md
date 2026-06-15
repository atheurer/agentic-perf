# Clusterbuster Config Construction Guide

Clusterbuster uses YAML job files with an `options:` dict.
The run-file bundle for agentic-perf wraps this into JSON.
The execution handler writes the YAML and runs clusterbuster.

## Run-File Bundle Format

```json
{
  "harness": "clusterbuster",
  "kubeconfig": "/root/sno/sno-3d/kubeconfig",
  "job_file": {
    "options": {
      "cleanup": true,
      "precleanup": true,
      "workload": "cpusoaker",
      "workloadruntime": 10,
      "exit-at-end": true,
      "report-object-creation": false,
      "deps-per-namespace": 8,
      "processes": 3,
      "namespaces": 1
    }
  }
}
```

### kubeconfig field

Path to the kubeconfig on the controller host. This is
critical for user-provided clusters where kubeconfig is
not at the default `~/.kube/config` location.

For user-provided OpenShift clusters, get the kubeconfig
path from the ticket (e.g., `/root/sno/sno-3d/kubeconfig`).

### job_file.options

Maps directly to clusterbuster's YAML format. Key rules:

- Option names use **dashes** not underscores:
  `deps-per-namespace`, `uperf-msg-size`, etc.
- `cleanup: true` and `precleanup: true` ensure clean state
- `exit-at-end: true` makes clusterbuster exit after the run
- `report-object-creation: false` reduces noise in output
- Workload-specific options are prefixed:
  `uperf-msg-size`, `sysbench-workload`, `hammerdb-driver`

## Example: cpusoaker (CPU stress)

```json
{
  "harness": "clusterbuster",
  "kubeconfig": "/root/sno/sno-3d/kubeconfig",
  "job_file": {
    "options": {
      "cleanup": true,
      "precleanup": true,
      "workload": "cpusoaker",
      "workloadruntime": 10,
      "exit-at-end": true,
      "deps-per-namespace": 4,
      "processes": 2,
      "namespaces": 1
    }
  }
}
```

## Example: uperf (network)

```json
{
  "harness": "clusterbuster",
  "kubeconfig": "/root/sno/sno-3d/kubeconfig",
  "job_file": {
    "options": {
      "cleanup": true,
      "precleanup": true,
      "workload": "uperf",
      "workloadruntime": 30,
      "uperf-msg-size": 8192,
      "uperf-test-type": "stream",
      "uperf-proto": "tcp",
      "replicas": 4,
      "antiaffinity": true
    }
  }
}
```

## Example: sysbench (multi-mode)

```json
{
  "harness": "clusterbuster",
  "kubeconfig": "/root/sno/sno-3d/kubeconfig",
  "job_file": {
    "options": {
      "cleanup": true,
      "precleanup": true,
      "workload": "sysbench",
      "sysbench-workload": "cpu",
      "workloadruntime": 10,
      "exit-at-end": true
    }
  }
}
```

## Installation

Clusterbuster is installed via git clone + pip:

```bash
git clone https://github.com/redhat-performance/clusterbuster.git /opt/clusterbuster
cd /opt/clusterbuster && pip install -e .
```

Requires Python 3.9+, PyYAML, kubernetes, and openshift
Python packages. The provisioning agent handles this
automatically via the git_clone install method.

## Execution

The handler runs:
```bash
KUBECONFIG=<path> clusterbuster -f /tmp/clusterbuster-<uuid>/job.yaml
```

Clusterbuster uses Python's standard YAML parser, so
yaml.dump output works correctly (no Go yaml.v3 quirks).

## RBAC Requirements

Clusterbuster needs admin/cluster-admin privileges to:
- Create and delete namespaces
- Create pods, deployments, replicasets, configmaps
- Optionally create VirtualMachines (with CNV)
- Access Prometheus for metrics collection

## VM Mode

With OpenShift Virtualization installed, add to options:
```json
"vm": true,
"vm-cores": 4,
"vm-memory": "8Gi"
```

This runs workloads inside KubeVirt VMs instead of pods.
