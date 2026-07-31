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

# ── Build stage ──────────────────────────────────
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
USER 1001

# Generate SSH key if not mounted
RUN mkdir -p ~/.ssh && \
    chmod 700 ~/.ssh && \
    ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N "" -q

ENTRYPOINT ["/app/start.sh"]
