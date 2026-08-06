# Container Deployment

## Building the image

The `Containerfile` supports both podman (primary) and docker:

```bash
# Podman
podman build -t agentic-perf -f Containerfile .

# Docker
docker build -t agentic-perf -f Containerfile .
```

### Jumpstarter SDK

The Jumpstarter SDK can be baked into the image at build time
or installed at runtime. To include it in the image, uncomment
the `setup-jumpstarter.sh` lines in the Containerfile before
building. This creates a larger image (~2GB) but avoids
runtime setup delays.

## Running

### Minimal (no Jumpstarter)

```bash
podman run -d --name agentic-perf \
  -p 8090:8090 \
  -v agentic-perf-data:/data/agentic-perf \
  -v ./config.json:/data/agentic-perf/config.json:ro \
  -e CLAUDE_CODE_USE_VERTEX=1 \
  -e CLOUD_ML_REGION=global \
  -e ANTHROPIC_VERTEX_PROJECT_ID=<project-id> \
  agentic-perf
```

### With Jumpstarter

```bash
podman run -d --name agentic-perf \
  -p 8090:8090 \
  -v agentic-perf-data:/data/agentic-perf \
  -v ./config.json:/data/agentic-perf/config.json:ro \
  -v ./secrets:/data/agentic-perf/secrets:ro \
  -v ./jumpstarter-client.yaml:/home/agentic-perf/.config/jumpstarter/clients/perf-ci.yaml:ro \
  -e CLAUDE_CODE_USE_VERTEX=1 \
  -e CLOUD_ML_REGION=global \
  -e ANTHROPIC_VERTEX_PROJECT_ID=<project-id> \
  agentic-perf
```

## Configuration

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `CLAUDE_CODE_USE_VERTEX` | Yes (Vertex) | Set to `1` for Vertex AI |
| `CLOUD_ML_REGION` | Yes (Vertex) | Vertex AI region (e.g., `global`) |
| `ANTHROPIC_VERTEX_PROJECT_ID` | Yes (Vertex) | GCP project ID |
| `AGENTIC_PERF_HOME` | No | Data directory (default: `/data/agentic-perf`) |
| `OPENAI_API_KEY` | Yes (OpenAI) | API key for OpenAI provider |

### Mounted files

| Path in container | Purpose |
|---|---|
| `/data/agentic-perf/config.json` | Main configuration |
| `/data/agentic-perf/secrets/` | Auth tokens (API, Domain MCP, etc.) |
| `/home/agentic-perf/.config/jumpstarter/clients/*.yaml` | Jumpstarter client config |

### Persistent data

Mount a volume at `/data/agentic-perf` for:
- `tickets/` — ticket state (survives restarts)
- `logs/` — event logs (JSONL per ticket)
- `investigation-records/` — investigation memory
- `secrets/api-token` — generated on first run

## OpenShift deployment

For a comprehensive OpenShift deployment guide covering secrets
management, init containers, troubleshooting, and lessons learned
from production deployment, see
[OpenShift Deployment Guide](openshift-deployment.md).

### Quick start via web UI

1. **Create project:** e.g., `agentic-perf`
2. **Add to project → Import from Git:**
   - Git URL: `https://github.com/atheurer/agentic-perf.git`
   - Dockerfile path: `Containerfile`
3. **Create secrets** (Workloads → Secrets):
   - LLM credentials (env vars)
   - Jumpstarter client config (file)
   - Domain MCP token (file)
4. **Create ConfigMap** with `config.json`
5. **Create PVC** (10Gi) for persistent data
6. **Edit deployment** to mount secrets, ConfigMap, and PVC
7. **Create Route** to expose port 8090

### Required network egress

| Destination | Purpose |
|---|---|
| Vertex AI API | LLM inference |
| Jumpstarter controller | Board lease management (gRPC) |
| AutoSD build server | OS image downloads |
| Domain MCP server | Historical performance data |
| Lab board IPs | SSH for benchmark execution |

### Health check

The state store exposes `/api/v1/health` for liveness probes:

```yaml
livenessProbe:
  httpGet:
    path: /api/v1/health
    port: 8090
  initialDelaySeconds: 10
  periodSeconds: 30
```
