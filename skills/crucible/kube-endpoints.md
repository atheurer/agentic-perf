# Crucible Kube Endpoint Construction

Crucible supports running benchmarks in Kubernetes pods using
the `kube` endpoint type. The cluster is managed via kubectl
on a host that crucible SSHes to.

## Endpoint Structure

```json
{
  "type": "kube",
  "host": "<kube-host-ip>",
  "user": "root",
  "engines": { "client": "1" }
}
```

### Required fields

- `type`: must be `"kube"`
- `host`: IP of the host where kubectl runs. For single-node
  K8s (K3s), this is the controller's private IP
- `user`: SSH user for kubectl access (typically `"root"`)
- `engines`: object mapping roles to engine IDs. Format:
  `{"client": "1"}` for client-only (fio), or
  `{"client": "1", "server": "1"}` for client-server (uperf)

### Optional fields

- `controller-ip-address`: IP where pods reach the rickshaw
  controller. For single-node K8s, use the controller's
  private IP (same as `host`). Auto-detected if omitted,
  but set it explicitly to avoid detection issues
- `kubeconfig`: path to kubeconfig file on the kube host.
  Defaults to `~/.kube/config` (kubectl standard). Only set
  if the K8s distro uses a non-standard location
- `config`: array of config blocks for engine settings
  (userenv, resources, securityContext, etc.)

## Single-Node K8s (K3s on AWS)

For kube endpoints, agentic-perf provisions a single host
that serves as both the crucible controller and the K8s
cluster node. There are no separate endpoint/target hosts.

- `assigned_hardware_ips.targets` is empty `[]`
- `host` in the endpoint = controller's private IP
- `controller-ip-address` = same as `host`
- The K8s installer sets up self-SSH so crucible can SSH
  to the kube host (which is itself)

## SSH Key Setup

For kube, `setup_passwordless_ssh` must ensure the
controller can SSH to the kube host. For single-node K8s,
pass the controller's SSH address as the source and
the controller's private IP as the target:

```
setup_passwordless_ssh(
  source=<ssh_hardware_ips.controller>,
  targets=[<assigned_hardware_ips.controller>],
  target_ssh_hosts=[<ssh_hardware_ips.controller>]
)
```

## Example: fio on kube

```json
{
  "benchmarks": [{
    "name": "fio",
    "ids": "1",
    "mv-params": {
      "global-options": [{
        "name": "required",
        "params": [
          {"arg": "bs", "vals": ["4K"]},
          {"arg": "rw", "vals": ["randread"]},
          {"arg": "ioengine", "vals": ["sync"]},
          {"arg": "runtime", "vals": ["30s"]},
          {"arg": "time_based", "vals": ["1"]},
          {"arg": "size", "vals": ["10M"]},
          {"arg": "filename", "vals": ["/tmp/fio.foo"]},
          {"arg": "clocksource", "vals": ["gettimeofday"]},
          {"arg": "ramp_time", "vals": ["5s"]},
          {"arg": "unlink", "vals": ["1"]},
          {"arg": "norandommap", "vals": ["ON"]}
        ]
      }],
      "sets": [{"include": "required"}]
    }
  }],
  "tool-params": [
    {"tool": "sysstat"},
    {"tool": "procstat"}
  ],
  "tags": {"run-type": "fio-kube"},
  "endpoints": [{
    "type": "kube",
    "controller-ip-address": "CONTROLLER_PRIVATE_IP",
    "host": "CONTROLLER_PRIVATE_IP",
    "user": "root",
    "engines": {"client": "1"}
  }],
  "run-params": {
    "num-samples": 1,
    "test-order": "s"
  }
}
```

## tool-params is required

Always include at least `sysstat` and `procstat` in
tool-params. Crucible crashes on missing tool-params.

## Key differences from remotehosts

| Aspect          | remotehosts          | kube                 |
|-----------------|----------------------|----------------------|
| Endpoint type   | `"remotehosts"`      | `"kube"`             |
| Structure       | `remotes[]` array    | flat, no `remotes`   |
| Host field      | in remote `config`   | top-level `host`     |
| engines         | per-remote `engines` | top-level `engines`  |
| userenv         | in `settings`        | in `config[]` block  |
| Target hosts    | separate machines    | pods on K8s cluster  |
