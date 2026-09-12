# agentic-perf container image
#
# Build with podman (primary) or docker:
#   podman build -t agentic-perf -f Containerfile .
#   docker build -t agentic-perf -f Containerfile .
#
# Run:
#   podman run -d --name agentic-perf \
#     -p 8090:8090 \
#     -v agentic-perf-data:/data/agentic-perf \
#     -v ./config.json:/data/agentic-perf/config.json:ro \
#     -e CLAUDE_CODE_USE_VERTEX=1 \
#     -e CLOUD_ML_REGION=global \
#     -e ANTHROPIC_VERTEX_PROJECT_ID=<project-id> \
#     agentic-perf
#
# Configuration:
#   Mount config.json at $AGENTIC_PERF_HOME/config.json
#   Mount secrets at $AGENTIC_PERF_HOME/secrets/
#   Mount Jumpstarter client config at
#     ~/.config/jumpstarter/clients/
#   Set LLM credentials via environment variables

# ── Arcaflow MCP build stage ─────────────────────
ARG ARCAFLOW_MCP_REPO=https://github.com/arcalot/arcaflow-mcp.git
ARG ARCAFLOW_MCP_REF=initial-development

FROM golang:1.24-alpine3.20 AS arcaflow-mcp-builder

ARG ARCAFLOW_MCP_REPO
ARG ARCAFLOW_MCP_REF

RUN apk --no-cache add git

WORKDIR /build
RUN git clone --depth 1 --branch "${ARCAFLOW_MCP_REF}" \
    "${ARCAFLOW_MCP_REPO}" repo

WORKDIR /build/repo/server
RUN CGO_ENABLED=0 GOOS=linux go build -a -installsuffix cgo \
    -ldflags="-w -s" \
    -o /arcaflow-mcp \
    ./cmd/arcaflow-mcp

# ── Python build stage ──────────────────────────────────
FROM registry.access.redhat.com/ubi9/python-312 AS builder

USER 0

RUN dnf install -y --setopt=install_weak_deps=False \
        git \
        gcc \
        openssh-clients \
    && dnf clean all

WORKDIR /build

# Install Python dependencies first (cache layer)
COPY pyproject.toml requirements-dev.lock ./
RUN pip install --no-warn-script-location \
    -r requirements-dev.lock

# Install the application
COPY . .
RUN pip install --no-warn-script-location \
    -e ".[vertex,telemetry]" \
    --no-deps

# ── Runtime stage ────────────────────────────────
FROM registry.access.redhat.com/ubi9/python-312

USER 0

RUN dnf install -y --setopt=install_weak_deps=False \
        openssh-clients \
        git \
        jq \
        sshpass \
        iputils \
        podman \
    && dnf clean all

# Copy installed Python packages from builder
COPY --from=builder /opt/app-root/lib /opt/app-root/lib
COPY --from=builder /opt/app-root/lib64 /opt/app-root/lib64
COPY --from=builder /opt/app-root/bin /opt/app-root/bin

# Copy application source
WORKDIR /app
COPY . .

# Jumpstarter SDK: install into the image so
# jmp/j CLIs and all drivers are available.
# Runs as root for /usr/local/bin symlinks.
# HOME must be /root for the install script's
# hardcoded venv path.
# The setup script's driver verification may fail
# because system Python differs from the venv
# Python. The drivers are installed correctly in
# the venv — verification is non-blocking.
RUN HOME=/root bash scripts/setup-jumpstarter.sh || \
    echo 'WARNING: setup-jumpstarter.sh exited non-zero (driver verification may have failed)'

# Fix the .pth file: the Jumpstarter install script
# detects the venv's Python version (may differ from
# the app Python). Rewrite to match the actual venv
# site-packages layout.
RUN PTH=$(find /opt/app-root -name 'jumpstarter.pth' 2>/dev/null | head -1) && \
    if [ -n "$PTH" ]; then \
        VENV=/root/.local/jumpstarter/venv && \
        PYVER=$(ls "$VENV/lib64/" 2>/dev/null | grep python | head -1) && \
        echo "$VENV/lib64/$PYVER/site-packages" > "$PTH" && \
        echo "$VENV/lib/$PYVER/site-packages" >> "$PTH" && \
        echo "Fixed .pth to $PYVER"; \
    fi

# CAIB (Cloud Automotive Image Builder) CLI
RUN CAIB_VERSION="v0.2.0" && \
    curl -sSL "https://raw.githubusercontent.com/centos-automotive-suite/automotive-dev-operator/${CAIB_VERSION}/hack/install-caib.sh" \
    | bash -s -- "${CAIB_VERSION}" || \
    echo 'WARNING: CAIB install failed (custom image builds will be unavailable)'

# Arcaflow MCP server binary (built in arcaflow-mcp-builder stage)
COPY --from=arcaflow-mcp-builder /arcaflow-mcp /usr/local/bin/arcaflow-mcp

# Runtime configuration
ENV AGENTIC_PERF_HOME=/data/agentic-perf
ENV PYTHONUNBUFFERED=1

# State store port
EXPOSE 8090

# Data directory — mount a volume here for
# persistence across restarts
RUN mkdir -p /data/agentic-perf && \
    chown -R 1001:0 /data/agentic-perf

VOLUME ["/data/agentic-perf"]

# Use the default UBI non-root user (1001)
# Generate SSH keypair for board access.
# Create as root so OpenShift's arbitrary UID
# (which shares group 0) can read the key.
RUN mkdir -p /opt/app-root/src/.ssh && \
    ssh-keygen -t ed25519 -f /opt/app-root/src/.ssh/id_ed25519 -N "" -q && \
    chmod 770 /opt/app-root/src/.ssh && \
    chmod 660 /opt/app-root/src/.ssh/* && \
    chown -R 1001:0 /opt/app-root/src/.ssh && \
    # Also install the key at /root/.ssh so it's accessible
    # when HOME=/root (OpenShift arbitrary UID runs as root).
    # The ticket's ssh_key_path (~/.ssh/id_ed25519) resolves
    # to /root/.ssh/id_ed25519.
    mkdir -p /root/.ssh && \
    cp /opt/app-root/src/.ssh/id_ed25519 /root/.ssh/id_ed25519 && \
    cp /opt/app-root/src/.ssh/id_ed25519.pub /root/.ssh/id_ed25519.pub && \
    chmod 770 /root/.ssh && \
    chmod 660 /root/.ssh/id_ed25519 && \
    chmod 660 /root/.ssh/id_ed25519.pub && \
    chown -R 0:0 /root/.ssh

USER 1001

ENTRYPOINT ["/app/start.sh"]
