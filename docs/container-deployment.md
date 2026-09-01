# Container Deployment

This guide describes the image built by `Containerfile`. It runs the state
store and orchestrator from `start.sh` as UBI's non-root user (UID 1001 in a
local container; OpenShift may substitute an arbitrary UID in group 0).

## Build and verify

Podman is the primary workflow; Docker uses the same Containerfile. The build
installs Python dependencies from `requirements-dev.lock`, the application
with the `vertex` and `telemetry` extras, runtime tools (`openssh-clients`,
`git`, `jq`, `sshpass`, `iputils`), Jumpstarter CLI/drivers, and CAIB. CAIB
installation is warning-only at image build time, so verify it explicitly.

```bash
podman build -t agentic-perf:local -f Containerfile .
podman run --rm --entrypoint bash agentic-perf:local -lc \
  'python3 --version && j --help >/dev/null && jmp --help >/dev/null && caib --help >/dev/null'

docker build -t agentic-perf:local -f Containerfile .
docker run --rm --entrypoint bash agentic-perf:local -lc \
  'python3 --version && j --help >/dev/null && jmp --help >/dev/null && caib --help >/dev/null'
```

The image includes the Vertex/Anthropic client and telemetry dependencies.
OpenAI support is optional: install the `openai` extra in a derived image.
Credentials and provider-specific network access are never baked in.

## Run locally

`start.sh` requires `$AGENTIC_PERF_HOME/config.json` and listens on port 8090
by default.

```bash
podman run -d --name agentic-perf -p 8090:8090 \
  -v agentic-perf-data:/data/agentic-perf \
  -v "$PWD/config.json:/data/agentic-perf/config.json:ro" \
  -e CLAUDE_CODE_USE_VERTEX=1 -e CLOUD_ML_REGION=global \
  -e 'ANTHROPIC_VERTEX_PROJECT_ID=<project-id>' agentic-perf:local
curl -fsS http://localhost:8090/api/v1/health
```

For Docker, replace `podman` with `docker`. Use a named volume or a host
directory writable by the container UID.

## Jumpstarter and CAIB

Jumpstarter is already installed; nothing must be uncommented. The application
uses the runtime user's UBI home, normally `/opt/app-root/src`:

```bash
podman run -d --name agentic-perf -p 8090:8090 \
  -v agentic-perf-data:/data/agentic-perf \
  -v "$PWD/config.json:/data/agentic-perf/config.json:ro" \
  -v "$PWD/jumpstarter-client.yaml:/opt/app-root/src/.config/jumpstarter/clients/perf-ci.yaml:ro" \
  -v "$PWD/jumpstarter-config.yaml:/opt/app-root/src/.config/jumpstarter/config.yaml:ro" \
  -e CLAUDE_CODE_USE_VERTEX=1 -e CLOUD_ML_REGION=global \
  -e 'ANTHROPIC_VERTEX_PROJECT_ID=<project-id>' agentic-perf:local
```

CAIB reads its service token from `$AGENTIC_PERF_HOME/secrets/caib/token` and
optional registry credentials from
`$AGENTIC_PERF_HOME/secrets/caib/registry-auth.json`. Set
`image_build.push_registry` when Jumpstarter must pull the resulting OCI
image. Verify both clients:

```bash
podman exec agentic-perf caib --help
podman exec agentic-perf jmp get exporters
```

CAIB needs build-service egress and push permission. Quay tags receive a
configurable expiry; retain or promote images needed longer.

## Configuration and persistence

Mount `/data/agentic-perf` for application state. `AGENTIC_PERF_HOME` defaults
there; change the config and mount together if you override it. The optional
`AGENTIC_PERF_ARTIFACTS` and `AGENTIC_PERF_SECRETS` variables select separate
locations.

| Path | Contents |
|---|---|
| `config.json` | Runtime configuration |
| `tickets/` | Ticket state and per-ticket `workspace/` |
| `logs/` | Audit/event JSONL logs |
| `artifacts/<ticket>/<run>/` | Benchmark artifacts |
| `skill-cache/`, `plugin-schema-cache/` | Skill/schema caches |
| `investigation-records/` | File-backed records |
| `secrets/` | API and provider tokens |
| `private-skills/` | Private skill overrides |

Keep secrets out of the image. Harnesses may still use `/tmp`; archive files
needed after restart into the persistent artifact directory.

## Non-root troubleshooting

The image prepares `/data/agentic-perf` and creates SSH material as `1001:0`
with group access for OpenShift arbitrary UIDs. For host bind mounts, grant
the effective UID or group 0 read/write access; on SELinux hosts use Podman
`:Z` labeling. Check:

```bash
podman logs agentic-perf
podman exec agentic-perf id
podman exec agentic-perf sh -c 'test -r "$AGENTIC_PERF_HOME/config.json" && test -w /data/agentic-perf'
```

An immediate exit usually means config is missing/unreadable. A CAIB failure
usually means its token, registry auth, target, or build-service egress is
wrong. Use `/api/v1/health` for liveness.

## OpenShift

Use the [OpenShift deployment guide](openshift-deployment.md). It uses a PVC
for `/data/agentic-perf`, mounts Jumpstarter files at
`/opt/app-root/src/.config/jumpstarter`, and handles arbitrary UIDs.
