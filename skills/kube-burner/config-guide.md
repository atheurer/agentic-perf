# kube-burner Config Construction Guide

kube-burner uses a YAML config file plus separate object
template YAML files. The run-file bundle for agentic-perf
wraps both into a single JSON structure.

## Run-File Bundle Format

```json
{
  "harness": "kube-burner",
  "config": {
    "global": { ... },
    "jobs": [ ... ]
  },
  "templates": {
    "pod.yml": "apiVersion: v1\nkind: Pod\n...",
    "deployment.yml": "..."
  }
}
```

### config section

The `config` dict maps directly to kube-burner's YAML config:

```yaml
global:
  gc: true
  timeout: 30m
  measurements:
  - name: podLatency
    thresholds: []

jobs:
- name: node-density
  namespace: node-density
  jobType: create
  jobIterations: 50
  namespacedIterations: true
  cleanup: true
  waitWhenFinished: true
  qps: 20
  burst: 20
  objects:
  - objectTemplate: pod.yml
    replicas: 1
```

**IMPORTANT**: `jobType` values must be lowercase in
kube-burner v2.7.0+: `create`, `delete`, `read`, `patch`.
Capitalized values (e.g., `Create`) cause fatal errors.
Use `waitWhenFinished: true` (not `podWait`) to wait for
pods to reach Running state.

### templates section

Each key is a filename referenced by `objectTemplate` in
the jobs. Values are the raw YAML content of the K8s
resource template.

Templates support Go template variables:
- `{{ .Iteration }}` — current iteration number
- `{{ .Replica }}` — current replica number
- `{{ .JobName }}` — job name
- `{{ .UUID }}` — benchmark run UUID

## Example: node-density

```json
{
  "harness": "kube-burner",
  "config": {
    "global": {
      "gc": true,
      "timeout": "30m",
      "measurements": [{"name": "podLatency", "thresholds": []}]
    },
    "jobs": [{
      "name": "node-density",
      "jobType": "Create",
      "jobIterations": 50,
      "namespacedIterations": true,
      "cleanup": true,
      "qps": 20,
      "burst": 20,
      "objects": [{"objectTemplate": "pod.yml", "replicas": 1, "wait": true}]
    }]
  },
  "templates": {
    "pod.yml": "apiVersion: v1\nkind: Pod\nmetadata:\n  name: \"pause-{{ .Iteration }}-{{ .Replica }}\"\n  labels:\n    app: node-density\nspec:\n  containers:\n  - name: pause\n    image: registry.k8s.io/pause:3.9\n    resources:\n      requests:\n        cpu: \"10m\"\n        memory: \"16Mi\"\n"
  }
}
```

## Execution

The benchmark agent writes the config and templates to the
controller host, then runs:

```
kube-burner init -c /tmp/kb-config-<uuid>.yml --uuid <uuid>
```

kube-burner reads KUBECONFIG from the environment or
~/.kube/config (set up by the K3s installer).

## Important Notes

- kube-burner talks directly to the K8s API via kubeconfig —
  no SSH to a kube host like crucible does
- The controller host must have both the kube-burner binary
  and a valid kubeconfig
- For K3s, kubeconfig is at ~/.kube/config (symlinked from
  /etc/rancher/k3s/k3s.yaml by the K3s installer)
- Object template files must be in the same directory as the
  config file, or use absolute paths
- gc=true cleans up all created namespaces after the run
