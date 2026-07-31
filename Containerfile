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
#     /home/agentic-perf/.config/jumpstarter/clients/
#   Set LLM credentials via environment variables

# ── Build stage ──────────────────────────────────
FROM registry.fedoraproject.org/fedora:42 AS builder

RUN dnf install -y --setopt=install_weak_deps=False \
        python3.12 \
        python3.12-pip \
        python3.12-devel \
        git \
        gcc \
        openssh-clients \
    && dnf clean all

WORKDIR /build

# Install Python dependencies first (cache layer)
COPY pyproject.toml requirements-dev.lock ./
RUN python3.12 -m pip install --prefix=/install \
    --no-warn-script-location \
    -r requirements-dev.lock

# Install the application
COPY . .
RUN python3.12 -m pip install --prefix=/install \
    --no-warn-script-location \
    -e ".[vertex,telemetry]" \
    --no-deps

# ── Runtime stage ────────────────────────────────
FROM registry.fedoraproject.org/fedora:42

RUN dnf install -y --setopt=install_weak_deps=False \
        python3.12 \
        openssh-clients \
        git \
        nmap-ncat \
    && dnf clean all

# Create non-root user
RUN useradd -m -s /bin/bash agentic-perf

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Copy application source
WORKDIR /app
COPY --chown=agentic-perf:agentic-perf . .

# Jumpstarter SDK setup script (run at build time
# if desired, or at runtime via entrypoint)
# The setup script installs Jumpstarter into a venv
# at ~/.local/jumpstarter/venv/ — this can be done
# at build time for a self-contained image, or at
# runtime for flexibility.
#
# Uncomment to bake Jumpstarter into the image:
# USER agentic-perf
# RUN bash scripts/setup-jumpstarter.sh
# USER root

# Runtime configuration
ENV AGENTIC_PERF_HOME=/data/agentic-perf
ENV PYTHONUNBUFFERED=1

# State store port
EXPOSE 8090

# Data directory — mount a volume here for
# persistence across restarts
RUN mkdir -p /data/agentic-perf && \
    chown -R agentic-perf:agentic-perf /data/agentic-perf

VOLUME ["/data/agentic-perf"]

USER agentic-perf

# Generate SSH key if not mounted
RUN mkdir -p /home/agentic-perf/.ssh && \
    chmod 700 /home/agentic-perf/.ssh && \
    ssh-keygen -t ed25519 -f /home/agentic-perf/.ssh/id_ed25519 -N "" -q

ENTRYPOINT ["/app/start.sh"]
