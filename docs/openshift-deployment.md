# OpenShift Deployment Guide

Detailed guide for deploying agentic-perf on OpenShift, based on
production deployment experience. For basic container usage, see
[container-deployment.md](container-deployment.md).

## Prerequisites

- OpenShift cluster with a namespace for the deployment
- Container image pushed to a registry (e.g., Quay.io)
- LLM provider credentials (Vertex AI, OpenAI, etc.)
- Jumpstarter client config (if using lab hardware)
- Registry push credentials and a CAIB token if custom images are required

Build and verify the image before creating the Deployment:

```bash
podman build -t 'quay.io/<org>/agentic-perf:<tag>' -f Containerfile .
podman push 'quay.io/<org>/agentic-perf:<tag>'
podman run --rm --entrypoint bash quay.io/<org>/agentic-perf:<tag> -lc \
  'j --help >/dev/null && jmp --help >/dev/null && caib --help >/dev/null'
```

The Containerfile installs Jumpstarter and CAIB unconditionally (CAIB failure
is warning-only during the build), the Vertex/Anthropic and telemetry Python
extras, and the runtime tools listed in [container-deployment.md](container-deployment.md).
OpenAI requires the optional `openai` extra in a derived image.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  OpenShift Namespace                            │
│                                                 │
│  ┌─────────────┐    ┌──────────────────────┐   │
│  │ Init        │───>│ agentic-perf pod      │   │
│  │ Container   │    │  ├─ state store :8090 │   │
│  │ (copy       │    │  ├─ orchestrator      │   │
│  │  secrets)   │    │  └─ dashboard UI      │   │
│  └─────────────┘    └──────────┬───────────┘   │
│                                │               │
│  Volumes:                      │               │
│  ├─ PVC (data)                 │               │
│  ├─ ConfigMap (config.json)    │               │
│  ├─ Secret (LLM creds)        │               │
│  ├─ Secret (Jumpstarter)       │               │
│  ├─ Secret (Domain MCP)        │               │
│  └─ Secret (Horreum API key)   │               │
│                                │               │
│  Route ─────────────────────>──┘               │
└─────────────────────────────────────────────────┘
```

## Secrets

Secrets are mounted as read-only volumes on the init container,
which copies them to the writable PVC. The main container reads
from the PVC. This pattern is necessary because OpenShift mounts
Secret volumes as root-owned, but the container runs as a
non-root arbitrary UID.

### LLM Provider (Vertex AI)

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: agentic-perf-vertex
type: Opaque
stringData:
  CLAUDE_CODE_USE_VERTEX: "1"
  CLOUD_ML_REGION: "global"
  ANTHROPIC_VERTEX_PROJECT_ID: "<project-id>"
```

If using ADC (Application Default Credentials):

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: agentic-perf-gcp-adc
type: Opaque
data:
  adc.json: <base64-encoded ADC file>
```

### Jumpstarter Client Config

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: agentic-perf-jumpstarter
type: Opaque
stringData:
  perf-ci.yaml: |
    apiVersion: jumpstarter.dev/v1alpha1
    kind: ClientConfig
    metadata:
      namespace: jumpstarter-lab
      name: perf-ci
    endpoint: grpc.jumpstarter-lab.apps.example.com:443
    tls:
      ca: ''
      insecure: true
    token: <jumpstarter-token>
    grpcOptions: {}
    drivers:
      allow: []
      unsafe: true
    shell:
      use_profiles: false
    leases:
      acquisition_timeout: 7200
  config.yaml: |
    apiVersion: jumpstarter.dev/v1alpha1
    kind: UserConfig
    config:
      current-client: perf-ci
```

### Domain MCP Token

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: agentic-perf-domain-mcp
type: Opaque
stringData:
  token: <domain-mcp-token>
```

### Horreum API Key (for investigation records)

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: agentic-perf-horreum
type: Opaque
stringData:
  api-key: <HUSR_...>
```

### Webhook Service Account Token

Webhook service account tokens are stored in the state store's
user database, not as Kubernetes secrets. Create them via the
API after deployment (see [Webhook Ingestion](webhook-ingestion.md)).

## ConfigMap

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: agentic-perf-config
data:
  config.json: |
    {
      "auth": {
        "multi_user": true
      },
      "llm": {
        "provider": "claude",
        "model": "claude-sonnet-4-6",
        "backend": "vertex",
        "project_id": "<project-id>",
        "region": "global",
        "timeout": 120
      },
      "jumpstarter_images": {
        "server": "https://autosd.sig.centos.org/"
      },
      "introspection": {
        "enabled": true
      },
      "investigation_records": {
        "backend": "horreum",
        "url": "https://horreum.example.com",
        "secret": "horreum/api-key",
        "test_id": 426,
        "tls_verify": false
      },
      "external_mcp_servers": [
        {
          "name": "domain-mcp",
          "url": "https://domain-mcp.example.com/mcp/http",
          "transport": "streamable_http",
          "agents": {
            "gathering_context": {
              "enabled_tools": "all"
            },
            "review": {
              "enabled_tools": [
                "get_baseline_stats",
                "compare_run_to_baseline"
              ]
            },
            "evaluating_convergence": {
              "enabled_tools": [
                "get_baseline_stats",
                "compare_run_to_baseline",
                "find_similar_anomalies",
                "get_distribution"
              ]
            }
          },
          "secret": "domain-mcp/token",
          "trust": true
        }
      ]
    }
```

## PVC

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: agentic-perf-data
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
```

## Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agentic-perf
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: agentic-perf
  template:
    metadata:
      labels:
        app: agentic-perf
    spec:
      initContainers:
        - name: init-secrets
          image: <registry>/agentic-perf:<immutable-tag>
          command:
            - bash
            - -xc
            - |
              # Domain MCP token
              mkdir -p /data/agentic-perf/secrets/domain-mcp
              cp /ext-secrets/domain-mcp/token /data/agentic-perf/secrets/domain-mcp/token

              # Horreum API key
              mkdir -p /data/agentic-perf/secrets/horreum
              cp /ext-secrets/horreum/api-key /data/agentic-perf/secrets/horreum/api-key

              # Jumpstarter client config
              mkdir -p /opt/app-root/src/.config/jumpstarter/clients
              cp /ext-secrets/jumpstarter/perf-ci.yaml /opt/app-root/src/.config/jumpstarter/clients/perf-ci.yaml
              cp /ext-secrets/jumpstarter/config.yaml /opt/app-root/src/.config/jumpstarter/config.yaml

              # The main container runs with an arbitrary UID in group 0.
              chmod -R g+rX /data/agentic-perf/secrets
          volumeMounts:
            - name: data
              mountPath: /data/agentic-perf
            - name: jumpstarter-home
              mountPath: /opt/app-root/src/.config/jumpstarter
            - name: domain-mcp-token
              mountPath: /ext-secrets/domain-mcp/token
              subPath: token
              readOnly: true
            - name: jumpstarter-client
              mountPath: /ext-secrets/jumpstarter
              readOnly: true
            - name: horreum-api-key
              mountPath: /ext-secrets/horreum/api-key
              subPath: api-key
              readOnly: true
      containers:
        - name: agentic-perf
          image: <registry>/agentic-perf:<immutable-tag>
          ports:
            - containerPort: 8090
          envFrom:
            - secretRef:
                name: agentic-perf-vertex
          env:
            - name: AGENTIC_PERF_HOME
              value: /data/agentic-perf
            - name: PYTHONUNBUFFERED
              value: "1"
            - name: GOOGLE_APPLICATION_CREDENTIALS
              value: /data/gcp/adc.json
            - name: JMP_DRIVERS_UNSAFE
              value: "true"
          volumeMounts:
            - name: data
              mountPath: /data/agentic-perf
            - name: config
              mountPath: /data/agentic-perf/config.json
              subPath: config.json
              readOnly: true
            - name: jumpstarter-home
              mountPath: /opt/app-root/src/.config/jumpstarter
            - name: gcp-adc
              mountPath: /data/gcp/adc.json
              subPath: adc.json
              readOnly: true
          livenessProbe:
            httpGet:
              path: /api/v1/health
              port: 8090
            initialDelaySeconds: 15
            periodSeconds: 30
          resources:
            requests:
              memory: 512Mi
              cpu: 500m
            limits:
              memory: 2Gi
              cpu: "2"
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: agentic-perf-data
        - name: config
          configMap:
            name: agentic-perf-config
        - name: domain-mcp-token
          secret:
            secretName: agentic-perf-domain-mcp
        - name: jumpstarter-client
          secret:
            secretName: agentic-perf-jumpstarter
        - name: jumpstarter-home
          emptyDir: {}
        - name: gcp-adc
          secret:
            secretName: agentic-perf-gcp-adc
        - name: horreum-api-key
          secret:
            secretName: agentic-perf-horreum
```

## Service and Route

```yaml
apiVersion: v1
kind: Service
metadata:
  name: agentic-perf
spec:
  selector:
    app: agentic-perf
  ports:
    - port: 8090
      targetPort: 8090
---
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: agentic-perf
spec:
  to:
    kind: Service
    name: agentic-perf
  port:
    targetPort: 8090
  tls:
    termination: edge
```

## Post-Deployment Setup

### Create admin user

```bash
export AP_URL="https://<route-hostname>"
export AP_TOKEN=$(oc exec deployment/agentic-perf -- \
  cat /data/agentic-perf/secrets/api-token)

curl -X POST -H "Authorization: Bearer $AP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "is_admin": true}' \
  $AP_URL/api/v1/users
```

### Create additional users

```bash
curl -X POST -H "Authorization: Bearer $AP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"username": "engineer1", "is_admin": false}' \
  $AP_URL/api/v1/users
```

### Create webhook service account

For receiving Horreum alerts:

```bash
curl -X POST -H "Authorization: Bearer $AP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "webhook-horreum",
    "service_account": true,
    "allowed_sources": ["<horreum-ip>"],
    "max_requests_per_hour": 60
  }' \
  $AP_URL/api/v1/users
```

Configure the webhook URL in Horreum's HTTP action with the
returned token. See [Webhook Ingestion](webhook-ingestion.md).

## Troubleshooting

### Init container fails

Check init container logs:

```bash
oc logs deployment/agentic-perf -c init-secrets
```

Common causes:
- Secret not created or wrong key name
- Volume mount path mismatch with `subPath`

### Pod starts but orchestrator doesn't dispatch

Check orchestrator logs:

```bash
oc logs deployment/agentic-perf --tail=100 | grep -i error
```

Common causes:
- ConfigMap not mounted (config.json missing)
- LLM credentials invalid (Vertex project/region wrong)
- `AGENTIC_PERF_HOME` not set

### Jumpstarter boards unreachable

Verify from the pod:

```bash
oc exec deployment/agentic-perf -- jmp get exporters
```

Common causes:
- Jumpstarter client config not copied by init container
- gRPC endpoint unreachable from pod network
- `JMP_DRIVERS_UNSAFE` not set (SNMP power control fails)

### Domain MCP auth failure

The token is resolved from `AGENTIC_PERF_HOME/secrets/`.
Verify:

```bash
oc exec deployment/agentic-perf -- \
  cat /data/agentic-perf/secrets/domain-mcp/token
```

If using an internal CA, set `"trust": true` in the
external MCP server config.

### Image resolution failures

Check the orchestrator logs for image resolution details:

```bash
oc logs deployment/agentic-perf --tail=500 | \
  grep "jumpstarter-images"
```

Common causes:
- Internal image server with self-signed certificate
  (set `trust_server` or verify the server is reachable)
- Release path mismatch (Horreum label doesn't match
  server directory — the code has datestamp fallback)
- Nightly image corruption (try a monthly build via
  `release` directive)

### SSH unreachable after flash

The platform agent validates SSH connectivity before
declaring the platform ready. If SSH fails:
- Check if the board actually booted (serial output)
- Verify the IP is routable from the pod network
- Check if the image's root password matches the
  harness default ("password")

### OpenShift arbitrary UID

OpenShift runs pods with a random UID in group 0. The
Containerfile creates SSH keys and directories with group
read permissions to accommodate this. If you see permission
errors, ensure files are owned by `1001:0` with group
read/write.

## Persistent Data

All persistent data lives on the PVC at
`/data/agentic-perf/`:

| Path | Contents |
|---|---|
| `tickets/` | Ticket state (JSON per ticket) |
| `logs/` | Event logs (JSONL per ticket) |
| `investigation-records/` | Local investigation records (if using file backend) |
| `secrets/api-token` | Auto-generated deployment token |
| `secrets/domain-mcp/token` | Domain MCP auth token |
| `secrets/horreum/api-key` | Horreum API key |
| `users.json` | User accounts and token hashes |
| `artifacts/<ticket>/<run>/` | Persistent benchmark artifacts |
| `tickets/<ticket>/workspace/` | Workspace files and generated charts |
| `skill-cache/`, `plugin-schema-cache/` | Cached skill/schema data |
| `secrets/caib/` | CAIB token and registry auth (if enabled) |

Some third-party harnesses still use `/tmp` during execution; copy anything
needed after the run into `AGENTIC_PERF_ARTIFACTS` or the ticket workspace.
The PVC is mounted at `/data/agentic-perf`, so the default artifact location
is persistent.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `AGENTIC_PERF_HOME` | Yes | Data directory (`/data/agentic-perf`) |
| `PYTHONUNBUFFERED` | Recommended | Ensures log output is immediate |
| `GOOGLE_APPLICATION_CREDENTIALS` | Vertex AI | Path to ADC JSON file |
| `CLAUDE_CODE_USE_VERTEX` | Vertex AI | Set to `1` |
| `CLOUD_ML_REGION` | Vertex AI | Region (e.g., `global`) |
| `ANTHROPIC_VERTEX_PROJECT_ID` | Vertex AI | GCP project ID |
| `JMP_DRIVERS_UNSAFE` | Jumpstarter | Set to `true` for SNMP power control |
| `OPENAI_API_KEY` | OpenAI | API key for OpenAI provider |

For CAIB, mount `caib/token` and (when pushing) `caib/registry-auth.json`
under `/data/agentic-perf/secrets/caib/`. Allow egress to the CAIB build
service and the OCI registry; allow Jumpstarter exporters to pull the image.

## CAIB custom image builds

Set `image_build` in the ticket/configuration only when the flashed image must
contain the requested change. The image-builder runs in `building_image`,
before a hardware lease, and stores `image_build_result`; success advances to
`awaiting_hardware`, while failure advances to `awaiting_customer_guidance`.
See [CAIB image building](../skills/caib/image-building.md) for target
resolution, package/bootc modes, manifests, token/registry setup, and
recovery. Use `system_config` for post-flash runtime changes instead.

## Arcaflow MCP workflow execution

The Arcaflow MCP server enables multi-plugin workflow execution
(e.g., fio + PCP metrics collection). The engine resolves plugin
schemas by running plugin containers via ATP, which requires
access to a container runtime.

### Deployer options

Choose the deployer that fits your environment:

#### Kubernetes deployer (recommended for OCP)

Plugin containers run as ephemeral pods in the same cluster.
No privileged security contexts needed.

**Setup:**

1. Create a ServiceAccount with pod management permissions:

```bash
oc create sa arcaflow-engine -n <namespace>

oc apply -n <namespace> -f - <<YAML
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: arcaflow-engine-role
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["create", "get", "list", "watch", "delete"]
- apiGroups: [""]
  resources: ["pods/log"]
  verbs: ["get", "list", "watch"]
YAML

oc create rolebinding arcaflow-engine-binding \
  --role=arcaflow-engine-role \
  --serviceaccount=<namespace>:arcaflow-engine -n <namespace>
```

2. Set the deployment to use the ServiceAccount:

```bash
oc patch deployment agentic-perf -n <namespace> --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/serviceAccountName","value":"arcaflow-engine"}]'
```

3. Create the MCP config ConfigMap:

```bash
oc apply -n <namespace> -f - <<YAML
apiVersion: v1
kind: ConfigMap
metadata:
  name: arcaflow-mcp-config
data:
  config.yaml: |
    engine:
      deployer: kubernetes
      deployment:
        metadata:
          namespace: <namespace>
YAML
```

4. Mount the ConfigMap in the deployment:

```bash
oc patch deployment agentic-perf -n <namespace> --type=json -p='[
  {"op":"add","path":"/spec/template/spec/volumes/-",
   "value":{"name":"arcaflow-mcp-config","configMap":{"name":"arcaflow-mcp-config"}}},
  {"op":"add","path":"/spec/template/spec/containers/0/volumeMounts/-",
   "value":{"name":"arcaflow-mcp-config","mountPath":"/etc/arcaflow-mcp","readOnly":true}}
]'
```

5. Update `external_mcp_servers` in agentic-perf config:

```json
{
    "name": "arcaflow",
    "command": ["arcaflow-mcp", "--enable-execution", "--config", "/etc/arcaflow-mcp/config.yaml"],
    "transport": "stdio",
    "agents": { ... }
}
```

#### Podman with remote connection

Use a named podman connection to a remote host with podman.
The engine runs containers on the remote host via SSH.

**Requirements:**
- A host with podman accessible via SSH
- SSH key access from the orchestrator pod
- Podman connection configured

**Setup:**

```bash
# On the orchestrator pod:
podman system connection add schema-host ssh://root@<host> --identity /path/to/key
```

MCP config (`/etc/arcaflow-mcp/config.yaml`):

```yaml
engine:
  deployer: podman
  deployment:
    connectionName: "schema-host"
```

**Best for:** Environments with a dedicated VM or lab host
with podman available.

#### Podman local (privileged pod)

Run podman directly in the orchestrator pod.

**Requirements:**
- `privileged: true` security context, or
- Namespace labeled: `pod-security.kubernetes.io/enforce=privileged`
- podman installed in the container image (already included)

MCP config:

```yaml
engine:
  deployer: podman
```

**Best for:** Development/testing with relaxed security.

#### Without a deployer

If no container runtime is available, the MCP can still serve:
- `plugin_list` / `plugin_describe` (Quay API + metadata)
- `workflow_load` / `workflow_list` (YAML parsing)

But `workflow_input_validate` and `workflow_execute` will
fail because they require plugin schema resolution via ATP.
