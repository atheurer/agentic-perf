# OpenShift Deployment Guide

Detailed guide for deploying agentic-perf on OpenShift, based on
production deployment experience. For basic container usage, see
[container-deployment.md](container-deployment.md).

## Prerequisites

- OpenShift cluster with a namespace for the deployment
- Container image pushed to a registry (e.g., Quay.io)
- LLM provider credentials (Vertex AI, OpenAI, etc.)
- Jumpstarter client config (if using lab hardware)

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
          image: <registry>/agentic-perf:latest
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
          image: <registry>/agentic-perf:latest
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

Benchmark artifacts are written to `/tmp/` and are
**not persistent** — they are lost on pod restart.
See [#461](https://github.com/atheurer/agentic-perf/issues/461)
for planned artifact archival.

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
