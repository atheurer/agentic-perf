# Configuration Reference

All configuration lives in `~/.agentic-perf/config.json`. Every field is
optional — sensible defaults are used when a field is absent.

The config path can be changed by setting the `AGENTIC_PERF_HOME`
environment variable (defaults to `~/.agentic-perf`).

### Live config updates

The orchestrator re-reads `config.json` at each agent dispatch.
Changes to the following fields take effect on the next dispatch
without a restart:

- `llm.*` (provider, model, backend, region, timeout, max_tokens,
  reasoning_effort)
- `agent_models.*` (per-agent LLM overrides)
- `agent_iterations.*` and `global_max_iterations`
- `agent_task_timeout`

All other fields (poll_interval, skills/repo-cache, secrets/vault,
telemetry, budget, max_concurrent_agents, introspection) require a
restart.

If `config.json` is malformed or unreadable at dispatch time, the
orchestrator logs a warning and continues with the last successfully
loaded configuration.

## Minimal Example

```json
{
    "llm": {
        "provider": "claude",
        "model": "claude-sonnet-4-6"
    }
}
```

## Full Example

```json
{
    "llm": {
        "provider": "claude",
        "model": "claude-sonnet-4-6",
        "backend": "vertex",
        "project_id": "my-gcp-project",
        "region": "us-east5",
        "base_url": null,
        "api": "chat_completions",
        "gemini_api_key": null,
        "timeout": 120,
        "reasoning_effort": "medium"
    },
    "agent_models": {
        "review": {
            "reasoning_effort": "high"
        },
        "introspection": {
            "model": "claude-haiku-4-5"
        }
    },
    "state_store": {
        "url": "http://localhost:8090",
        "port": 8090
    },
    "poll_interval": 3.0,
    "ssh_key": "~/.ssh/id_ed25519",
    "crucible_home": "/opt/crucible",
    "zathras_home": "/opt/zathras",
    "harness_repos": {
        "my-harness": "https://github.com/org/my-harness.git"
    },
    "agent_task_timeout": 0,
    "stale_task_timeout": 900,
    "llm_budget": {
        "session_cost_usd": 50.00
    },
    "introspection": {
        "enabled": false
    },
    "compress_closed_after_days": 7,
    "manual_purge_enabled": true,
    "telemetry": {
        "otlp_exporter": {
            "endpoint": "http://localhost:4317",
            "headers": {"Authorization": "Bearer ..."}
        }
    }
}
```

---

## Field Reference

### `llm` — Global LLM Provider

Configures the default LLM provider used by all agents unless
overridden by `agent_models`.

| Field | Type | Default | Env override | Description |
|---|---|---|---|---|
| `provider` | string | `"mock"` | `LLM_PROVIDER` | Provider name (see [Supported Providers](#supported-providers)) |
| `model` | string | **(required)** | `LLM_MODEL` | Model identifier. No default — must be set when provider is not `mock`. |
| `backend` | string | — | `LLM_BACKEND` | `"vertex"` for Vertex AI, `"direct"` for direct API |
| `project_id` | string | — | `ANTHROPIC_VERTEX_PROJECT_ID` | GCP project ID (Vertex AI backends) |
| `region` | string | — | `CLOUD_ML_REGION` | Cloud region (Vertex AI backends) |
| `base_url` | string | — | `OPENAI_BASE_URL` | Base URL for OpenAI-compatible endpoints |
| `api` | string | `"chat_completions"` | `OPENAI_API` | OpenAI API mode: `"chat_completions"` or `"responses"`. Applies only to the `openai` provider. |
| `gemini_api_key` | string | — | `GOOGLE_API_KEY` or `GEMINI_API_KEY` | API key for Gemini provider |
| `timeout` | float | `120` | `LLM_TIMEOUT` | Per-request timeout in seconds. `0` disables. |
| `reasoning_effort` | string | — | `LLM_REASONING_EFFORT` | Global reasoning effort level: `"low"`, `"medium"`, `"high"`. Provider-specific values also accepted (e.g. Claude's `"xhigh"`/`"max"`, Gemini's `"minimal"`). |

#### Supported Providers

| Provider value | LLM service | Default model |
|---|---|---|
| `"claude"` or `"anthropic"` | Anthropic Claude (direct or Vertex AI) | `claude-sonnet-4-6` |
| `"gemini"` or `"google"` | Google Gemini (direct or Vertex AI) | `gemini-2.5-flash` |
| `"openai"` | OpenAI-compatible API (OpenAI, Azure, vLLM, Ollama, etc.) | `gpt-4o` |
| `"mock"` | Canned responses for testing (no API key needed) | — |

#### Authentication

| Provider | How to authenticate |
|---|---|
| Claude (direct) | Set `ANTHROPIC_API_KEY` env var |
| Claude (Vertex) | `gcloud auth application-default login` + set `project_id` and `region` |
| Gemini (direct) | Set `GOOGLE_API_KEY` or `GEMINI_API_KEY` env var, or `llm.gemini_api_key` in config |
| Gemini (Vertex) | `gcloud auth application-default login` + set `project_id` and `region` |
| OpenAI | Set `OPENAI_API_KEY` env var |

The OpenAI provider uses `max_completion_tokens` for GPT-5 and o-series
models, which reject the legacy `max_tokens` parameter. Older
OpenAI-compatible endpoints continue to receive `max_tokens`.

---

### `agent_models` — Per-Agent LLM Overrides

Override the LLM provider and model for specific agent types. This
lets you run different agents on different models — for example, a
cheaper model for introspection.

`llm.model` is the default for **all** agents. Use `agent_models`
only when you want a specific agent to use a different model.

```json
{
    "agent_models": {
        "introspection": {
            "model": "claude-haiku-4-5"
        },
        "review": {
            "reasoning_effort": "high"
        }
    }
}
```

**Resolution order:**
1. `agent_models.<agent_type>` — per-agent override
2. `llm.*` — global default

Each override object supports `provider`, `model`, `api`, `reasoning_effort`,
and `max_tokens` keys. The `api` key is used when the override selects the
`openai` provider.

> **Breaking change:** `agent_models.default` and built-in per-agent
> model overrides have been removed. All agents now use `llm.model`
> unless explicitly overridden via `agent_models.<type>`.  If your
> config relied on `agent_models.default` or on built-in model
> assignments (e.g., triage defaulting to Sonnet), set `llm.model`
> to your desired default model.

#### Capability Defaults

Some agents have built-in output budget defaults that are always
applied (these do NOT override model or provider):

| Agent type | Capability | Value |
|---|---|---|
| `review` | `max_tokens` | `32000` |

#### Agent Type Names

These are the agent types that can be used as keys in `agent_models`:

| Agent type | Ticket status | Description |
|---|---|---|
| `triage` | `triage_pending` | Classifies and routes incoming requests |
| `resource_create` | `awaiting_hardware` | Acquires hardware resources |
| `provisioning` | `awaiting_provision` | Installs benchmark tooling |
| `benchmark` | `executing_benchmark` | Runs the benchmark |
| `review` | `awaiting_review` | Analyzes benchmark results |
| `resource_teardown` | `awaiting_teardown` | Releases hardware resources |
| `retrospective` | `retrospective_pending` | Post-mortem analysis |
| `introspection` | *(out-of-band)* | Continuous ticket observer |
| `gathering_context` | `gathering_context` | Collects investigation data |
| `planning_investigation` | `planning_investigation` | Plans investigation steps |
| `evaluating_convergence` | `evaluating_convergence` | Checks if investigation is complete |
| `synthesizing_results` | `synthesizing_results` | Produces final investigation report |

#### Per-Ticket Runtime Override

Individual tickets can override the LLM at runtime via
`custom_fields.llm_override`:

```json
{
    "llm_override": {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "reasoning_effort": "high"
    }
}
```

This override is cleared after the agent completes.

---

### `agent_iterations` — Per-Agent Iteration Limits

Override the maximum LLM iterations for specific agent types. This
lets you tune iteration budgets without changing code — for example,
raising the review agent's budget for complex analyses or lowering
provisioning's budget for quick tasks.

```json
{
    "agent_iterations": {
        "review": 75,
        "benchmark": 40,
        "provisioning": 30
    }
}
```

**Resolution order** (first match wins):
1. `agent_iterations.<agent_type>` — explicit per-agent config
2. `agent_iterations.default` — explicit catch-all
3. Built-in agent defaults (see table below)
4. Agent constructor default (20)

A value of **0 means unlimited** — termination is then driven by
convergence gates, cost guardrails, or HITL intervention rather
than an arbitrary count. Use `is not None` semantics internally
so 0 is treated as a valid value, not as missing.

#### Built-in Agent Defaults

These apply when no `agent_iterations` configuration is present:

| Agent type | Default iterations | Rationale |
|---|---|---|
| `review` | 50 | Heavy analysis with multi-metric interpretation |
| `platform` | 10 | Deterministic SDK-driven provisioning |
| `evaluating_convergence` | 0 (unlimited) | Convergence gates handle termination |
| `analyze` | 0 (unlimited) | Investigation depth varies by ticket |
| *(all others)* | 20 | Base default from `AgentBase` |

#### `global_max_iterations` — Ticket-Wide Ceiling

A hard ceiling on total LLM iterations across all agents for a
single ticket. Prevents runaway tickets from consuming unbounded
resources.

| Field | Type | Default | Env override |
|---|---|---|---|
| `global_max_iterations` | int | `100` | `GLOBAL_MAX_ITERATIONS` |

Individual tickets can override this via
`custom_fields.global_max_iterations_override`.

#### Per-Ticket Runtime Override

Individual tickets can override the per-agent limit at runtime via
`custom_fields.max_iterations_override`. This takes precedence over
both config and built-in defaults. The override is **additive**: it
grants N new iterations on top of what the agent already consumed in
prior runs, so setting `max_iterations_override=40` after a 20-iteration
run gives the agent 40 fresh iterations on re-dispatch.

```bash
curl -X PATCH .../api/v1/tickets/PERF-123/fields \
  -d '{"fields": {"max_iterations_override": 40}}'
```

The override is preserved while the ticket is paused
(`awaiting_customer_guidance`) and cleared after the agent
completes successfully.

#### Migration Note

The `jumpstarter_images.provisioning_max_iterations` config key is
superseded by `agent_iterations.provisioning`. The built-in default
for provisioning (30) matches the previous jumpstarter default.

---

### `state_store` — State Store Connection

| Field | Type | Default | Env override | Description |
|---|---|---|---|---|
| `url` | string | `"http://localhost:8090"` | `STATE_STORE_URL` | State store base URL |
| `port` | int | `8090` | — | Port for the state store server |

---

### `llm_budget` — Cost Guardrails

| Field | Type | Default | Description |
|---|---|---|---|
| `session_cost_usd` | float | `0` (disabled) | Maximum USD spend per orchestrator session. When exceeded, no new agents are started (existing ones finish). |

Per-ticket budgets are set via `custom_fields.llm_budget` on
individual tickets — see [Architecture](architecture.md) for details.

#### Per-User / Per-Group Quotas (multi-user mode)

In multi-user mode, per-user and per-group quotas prevent one
user's workload from consuming the entire deployment budget.

```json
{
    "llm_budget": {
        "session_cost_usd": 50.00,
        "default_user_quota": {
            "max_cost_usd_24h": 10.00,
            "max_cost_usd_7d": 50.00,
            "max_tokens_24h": 0,
            "max_tokens_7d": 0,
            "enforce": false
        },
        "default_group_quota": {
            "max_cost_usd_24h": 50.00,
            "enforce": false
        }
    }
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `default_user_quota` | object | none | Default quota applied to users without an explicit per-user quota. |
| `default_group_quota` | object | none | Default quota applied to groups without an explicit per-group quota. |

**Quota fields** (all optional, zero = no limit):

| Field | Type | Default | Description |
|---|---|---|---|
| `max_cost_usd_24h` | float | `0` | Max LLM cost in a rolling 24-hour window. |
| `max_cost_usd_7d` | float | `0` | Max LLM cost in a rolling 7-day window. |
| `max_tokens_24h` | int | `0` | Max total tokens in a rolling 24-hour window. |
| `max_tokens_7d` | int | `0` | Max total tokens in a rolling 7-day window. |
| `enforce` | bool | `false` | When false, violations are logged and commented but never block dispatch (warn-only mode). Set true for hard enforcement. |

Per-user quotas can also be set via the API:

```bash
curl -X PUT .../api/v1/users/alice/quota \
  -d '{"max_cost_usd_24h": 10.0, "enforce": false}'
```

**Enforcement points:**

1. **Pre-dispatch** — over-quota tickets are skipped (not blocked).
   Other users' tickets continue processing.
2. **In-loop** — secondary check bounds overshoot between dispatch
   cycles.
3. **Creation-time advisory** — warns at ticket creation if the
   user is already over quota.

**Semantics:** user AND group quotas must both pass. Multi-group
uses AND semantics (all groups must be within limits). Service
accounts are exempt by default but an explicitly-set quota is
honored.

**Usage ledger:** quota accounting uses a separate daily JSONL
ledger (`~/.agentic-perf/logs/usage-ledger-YYYY-MM-DD.jsonl`),
not the per-ticket event logs. This survives ticket archival and
avoids the event scan truncation limit.

**Known limitation:** dispatch-time checks cannot stop running
agents. Overshoot is bounded by `max_concurrent_agents ×
per-ticket budget`. Introspection spend charges the ticket
creator.

**Legacy mode:** quotas are a no-op when `multi_user` is false
(`created_by` is always empty).

---

### `context_guard` — Context-Window Guardrails

Monitors per-call context token usage against the model's
context window and pauses the agent before it hits the
provider's hard limit. Without this, a context overflow
surfaces as an opaque API error, wasting the iteration.

```json
{
    "context_guard": {
        "enabled": true,
        "warn_pct": 60,
        "pause_pct": 80,
        "default_context_window": 200000
    }
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Enable/disable the context guard globally. |
| `warn_pct` | float | `60` | Percentage of context window at which the agent receives a wrap-up warning. Set to `0` to disable. |
| `pause_pct` | float | `80` | Percentage of context window at which the agent is paused. Set to `0` to disable. |
| `default_context_window` | int | `0` | Fallback context window when the model is unknown. `0` uses the pricing.yaml fallback (128k). |

**Context window discovery:** window sizes are looked up from
`providers/cost/pricing.yaml` using the model name from the
LLM response usage. Users can also add `context_window` to
their custom `~/.agentic-perf/pricing.yaml`. If a user pricing
file lacks `context_window` for a model, the bundled default is
used (per-key fallback, not per-file).

**Per-ticket override:** individual tickets can override
`warn_pct`, `pause_pct`, and `enabled` via
`custom_fields.context_guard`:

```bash
curl -X PATCH .../api/v1/tickets/PERF-123 \
  -d '{"fields": {"context_guard": {"warn_pct": 70, "pause_pct": 90}}}'
```

**Behavior:**

- **Warn:** injects a `[SYSTEM] Context warning` message into
  the conversation (once per run). The agent can start wrapping
  up proactively.
- **Pause:** grants one grace iteration with a final-call
  message, then saves conversation state and transitions to
  `awaiting_customer_guidance`. The pause comment explicitly
  notes that raising `llm_budget` will not help — the
  conversation is too large for the model's input window.

**Check order:** context → budget → iteration. Only one grace
iteration is granted regardless of which guardrail fires first.

**Investigation agents (`max_iterations=0`):** the context
guard fires normally. This is the primary safety net for
unlimited-iteration agents that would otherwise hit the
provider's hard context limit.

---

### `introspection` — Introspection Agent

The introspection agent is a continuous passive observer that runs
alongside the pipeline agents, watching the event stream for anomalies
and writing observations to `custom_fields.introspection`.

```json
{
    "introspection": {
        "enabled": true
    }
}
```

| Field | Type | Default | Env override | Description |
|---|---|---|---|---|
| `enabled` | bool | `false` | `INTROSPECTION_ENABLED` | Enable introspection for all tickets globally |
| `llm` | bool | `true` | — | Use LLM for narrative and guidance suggestions. When `false`, introspection runs deterministic-only: anomaly detection, event counting, and guidance summary reason classification still work, but LLM-generated narratives and suggested responses are skipped. |

#### Per-Ticket Override

Individual tickets can enable or disable introspection regardless of
the global setting via `custom_fields.introspection_enabled`:

```json
{
    "custom_fields": {
        "introspection_enabled": true
    }
}
```

- `true` — enables introspection even when globally disabled
- `false` — disables introspection even when globally enabled
- absent — follows the global setting

The introspection agent is started by the orchestrator before the first
pipeline agent dispatches for a ticket, so no events are missed. It
stops automatically when the ticket reaches a terminal status. See
[Architecture](architecture.md) for details on what it detects.

---

### Timeouts

| Field | Type | Default | Env override | Description |
|---|---|---|---|---|
| `llm.timeout` | float | `120` | `LLM_TIMEOUT` | Per-request LLM API call timeout in seconds. `0` disables. |
| `agent_task_timeout` | float | `0` (disabled) | `AGENT_TASK_TIMEOUT` | Maximum wall-clock seconds for an entire agent task. Catches agents stuck in tool loops or waiting on unresponsive services. |
| `stale_task_timeout` | float | `900` | `STALE_TASK_TIMEOUT` | Cancel active tasks with no events for this many seconds. `0` disables. |

---

### Top-Level Fields

| Field | Type | Default | Env override | Description |
|---|---|---|---|---|
| `poll_interval` | float | `3.0` | `POLL_INTERVAL` | Seconds between orchestrator dispatch cycles |
| `ssh_key` | string | — | `SSH_KEY` | Path to SSH private key for remote host access |
| `ssh_key_vault_secret` | string | — | `SSH_KEY_VAULT_SECRET` | Vault secret name for SSH key fallback (see below) |
| `crucible_home` | string | `"/opt/crucible"` | `CRUCIBLE_HOME` | Path to crucible installation |
| `zathras_home` | string | `""` | `ZATHRAS_HOME` | Path to zathras installation |

---

### SSH Key Vault Fallback

When SSH keys are stored in Bitwarden Secrets Manager instead of
on the local filesystem, configure `ssh_key_vault_secret` to enable
automatic fallback:

```json
{
    "ssh_key_vault_secret": "ssh/id_ed25519"
}
```

**Resolution order:**

1. Ticket `custom_fields.ssh_key_path` — if the file exists on
   disk, it is used directly (no vault lookup).
2. Ticket `custom_fields.ssh_key_secret` — per-ticket vault secret
   name override (set by the resource agent).
3. `SSH_KEY_VAULT_SECRET` env var — deployment-level override.
4. `ssh_key_vault_secret` in config.json — global default.

The system materializes the vault secret to a temporary file (mode
0600) for the duration of the agent operation, then removes it.

If a vault secret name is configured but the secret is not found,
the operation fails with `SSHKeyResolutionError` — this fail-closed
design prevents SSH from falling back to default identity files.

See [Secrets Management](secrets.md) for vault configuration and
bootstrap token setup.

---

### `jumpstarter_images` — Jumpstarter Image Resolution

Configuration for the Jumpstarter image resolution system, which
pre-resolves OS image URLs from the build server before
provisioning.

```json
{
    "jumpstarter_images": {
        "server": "https://autosd.sig.centos.org/"
    }
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `server` | string | `"https://autosd.sig.centos.org/"` | Base URL of the OS image build server |
| `image_version` | string | — | Default OS image version (e.g., `AutoSD-10`). If not set, must be specified per-ticket via `directives.image_version`. |

> **Note:** `provisioning_max_iterations` was previously supported
> here. Use `agent_iterations.provisioning` instead (see
> [agent_iterations](#agent_iterations--per-agent-iteration-limits)).

Jumpstarter also requires:
- **Secrets:** `~/.agentic-perf/secrets/jumpstarter/config.json` with `{"client_name": "<name>"}` matching the jmp CLI client config.
- **CLI config:** `~/.config/jumpstarter/clients/<name>.yaml` with controller endpoint and token.

---

### `image_build` — Custom Image Building (CAIB)

Configuration for building custom OS images via the
[CAIB](https://gitlab.com/CentOS/automotive/infra/caib) pipeline.
When a ticket includes `image_build` directives, the image builder
agent uses these settings to build, push, and manage images.

```json
{
    "image_build": {
        "push_registry": "quay.io/redhat-performance/rhivos-agentic-perf-caib",
        "tag_expiration_days": 14
    }
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `push_registry` | string | — | Quay.io registry path for pushing built images. Required for CAIB builds. |
| `tag_expiration_days` | int | `14` | Days before Quay auto-deletes the image tag. Set via Quay API after push. |

#### Secrets

| Path | Description |
|---|---|
| `secrets/caib/token` | CAIB API authentication token |
| `secrets/caib/registry-auth.json` | Docker auth.json for pushing images to the registry (robot account) |
| `secrets/quay/api-token` | *(Optional)* Quay OAuth token with "Administer Repositories" scope |

**Tag expiration** uses the Quay REST API (Bearer auth). The token
is resolved in order:

1. **Robot account token** from `registry-auth.json` — preferred
   because it is already repo-scoped. The robot account must have
   admin access to the target repository.
2. **Dedicated OAuth token** from `secrets/quay/api-token` —
   fallback, requires "Administer Repositories" scope.

If neither is available, tag expiration is silently skipped.

#### Ticket Directives

Custom image builds are requested via `image_build` in the ticket's
`custom_fields`:

```json
{
    "custom_fields": {
        "image_build": {
            "provider": "caib",
            "target": "ebbr",
            "customizations": {
                "masked_services": ["podman-clean-transient.service"],
                "additional_rpms": ["strace", "perf"]
            }
        }
    }
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `provider` | string | `"caib"` | Image build provider name |
| `target` | string | auto-resolved | CAIB target (e.g., `ebbr` for S32G/R-Car S4). Auto-resolved from `board_selector` if omitted. |
| `customizations` | object | `{}` | Provider-specific customizations (masked services, RPMs, etc.) |
| `build_mode` | string | `build-dev` | CAIB command: `build-dev` for standalone disk images (supports `--mode package` or `--mode image`), `build` for bootc container images. Both work on all targets. Default `build-dev` produces mutable package-mode images. |
| `ttl` | string | `"168h"` | CAIB build record time-to-live |

#### OpenShift Deployment

For OCP, create the secrets and mount them:

```bash
# CAIB token
oc create secret generic agentic-perf-caib \
  --from-file=token=secrets/caib/token \
  --from-file=registry-auth.json=secrets/caib/registry-auth.json

# Quay API token (for tag expiration)
oc create secret generic agentic-perf-quay \
  --from-file=api-token=secrets/quay/api-token
```

Add volume mounts to the deployment:

```yaml
volumeMounts:
  - name: caib-secrets
    mountPath: /data/agentic-perf/secrets/caib/token
    subPath: token
    readOnly: true
  - name: caib-secrets
    mountPath: /data/agentic-perf/secrets/caib/registry-auth.json
    subPath: registry-auth.json
    readOnly: true
  - name: quay-secrets
    mountPath: /data/agentic-perf/secrets/quay/api-token
    subPath: api-token
    readOnly: true
volumes:
  - name: caib-secrets
    secret:
      secretName: agentic-perf-caib
  - name: quay-secrets
    secret:
      secretName: agentic-perf-quay
```

---

### `external_mcp_servers` — Remote MCP Servers

Connect agents to remote MCP servers via SSE or StreamableHTTP.
Each entry defines a named server with URL, transport, and
per-agent tool scoping.

```json
{
    "external_mcp_servers": [
        {
            "name": "domain-mcp",
            "url": "http://domain-mcp.lab:8080/mcp",
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
                        "compare_run_to_baseline"
                    ]
                }
            },
            "secret": "domain-mcp/token"
        }
    ]
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Display name for logging and tool routing |
| `url` | string | yes | MCP server endpoint URL |
| `transport` | string | yes | `"sse"` or `"streamable_http"` |
| `agents` | dict | yes | Maps agent type keys to their config. Only listed agents connect. |
| `secret` | string | no | Path within `~/.agentic-perf/secrets/` to a file containing the auth token |
| `trust` | bool | no | If `true`, disable SSL certificate verification (for self-signed certs) |

**Per-agent config:**

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled_tools` | `"all"` or list | `"all"` | Which tools from this server the agent's LLM can see. `"all"` exposes every tool. A list of tool names restricts visibility. Tools not listed are hidden from the LLM but remain callable by code. |

The auth token (if configured) is sent as `Authorization: Bearer <token>`
in the HTTP headers.

---

### `harness_repos` — Benchmark Harness Repositories

Override or extend the default set of harness Git repositories used
for skill documentation and remote skill resolution.

```json
{
    "harness_repos": {
        "my-harness": "https://github.com/org/my-harness.git"
    }
}
```

Entries are merged with the built-in defaults. To override a built-in
repo URL, use the same key name. Can also be set via the `HARNESS_REPOS`
environment variable as a JSON string.

Built-in repositories: `crucible`, `crucible-examples`, `zathras`,
`kube-burner`, `k8s-netperf`, `benchmark-runner`, `clusterbuster`,
`vstorm`, `ioscale`, `forge`, `boot-time-analysis-scripts`.

---

### `telemetry` — OpenTelemetry Export

```json
{
    "telemetry": {
        "otlp_exporter": {
            "endpoint": "http://localhost:4317",
            "headers": {"Authorization": "Bearer ..."}
        }
    }
}
```

Exports LLM call telemetry, tool call spans, and agent lifecycle events
to an OTLP-compatible collector (Jaeger, Grafana Loki, etc.).

---

### `secrets` — Vault-Backed Secrets

Connect the secrets cascade to a Bitwarden Secrets Manager project.
Vault layers are added behind local file layers — a file on disk
always overrides the vault within the same tier.

```json
{
    "secrets": {
        "bitwarden": {
            "organization_id": "<org-uuid>",
            "shared_project_id": "<project-uuid>",
            "group_project_ids": {
                "gpu-team": "<project-uuid>"
            },
            "server_url": "https://vault.example.com",
            "cache_ttl_seconds": 60
        }
    }
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `organization_id` | string | yes | Bitwarden organization UUID |
| `shared_project_id` | string | yes | SM project for deployment-shared secrets |
| `group_project_ids` | dict | no | Maps group names to SM project UUIDs for multi-user mode |
| `server_url` | string | no | Self-hosted server URL (omit for bitwarden.com cloud) |
| `cache_ttl_seconds` | int | no | In-memory cache TTL in seconds (default: `60`) |

Requires `pip install agentic-perf[bitwarden]` and a machine account
access token (env `AGENTIC_PERF_BWS_TOKEN` or file
`~/.agentic-perf/secrets/bitwarden/access-token`).

When this section is absent, vault layers are never constructed.
See [Secrets Management](secrets.md) for full details.

---

### `auth` — Multi-User Authentication

```json
{
    "auth": {
        "multi_user": false,
        "anonymous_read": false
    }
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `multi_user` | bool | `false` | Enable per-user authentication. When `true`, each API caller needs a personal bearer token (created via the admin API). The existing deployment token becomes the service principal used by the orchestrator and agents. When `false`, behavior is identical to a single-token deployment. |
| `token_ttl_days` | int | `0` | Maximum age (in days) for user tokens. `0` disables expiry. Applies only in multi-user mode. The deployment token is always exempt. When a token older than this value is presented, the server returns `401 Unauthorized`. Expired users must have their token rotated by an admin. |
| `anonymous_read` | bool | `false` | Allow unauthenticated read-only access to the dashboard and GET API endpoints. When `true`, GET, HEAD, and OPTIONS requests bypass bearer token authentication. All write operations (POST, PATCH, DELETE) still require a valid token. Useful for sharing dashboard links with stakeholders who need to observe but not interact. |

See [Multi-User Guide](multi-user.md) for bootstrap instructions and
the full feature walkthrough.

---

### `rate_limit` — API Rate Limiting

```json
{
    "rate_limit": {
        "enabled": true,
        "per_user_rpm": 600,
        "burst": 30,
        "exempt_service": true,
        "auth_failures_per_min": 30
    }
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Enable rate limiting. When `false`, no request or auth-failure limiting is applied. |
| `per_user_rpm` | int | `600` | Maximum requests per minute per user principal. Only enforced in multi-user mode — in legacy (single-token) mode, all callers share the service principal, so per-principal limiting would throttle the entire system. |
| `burst` | int | `30` | Token bucket burst capacity. Allows short bursts above the sustained RPM rate (e.g., dashboard page loads that fire several requests simultaneously). |
| `exempt_service` | bool | `true` | Exempt the service principal (deployment token) from rate limiting. **Strongly recommended.** A 429 to an agent triggers error paths that skip budget guardrails, and agent crash leads to paid re-dispatch loops. |
| `auth_failures_per_min` | int | `30` | Maximum authentication failures per IP per minute before returning 429 instead of 401. Also enforces a global failure ceiling (4× per-IP rate) as a backstop against distributed brute-force from localhost. Active in both legacy and multi-user modes. |

**Algorithm:** Token bucket with refill. Each principal gets an
independent bucket that refills at `per_user_rpm / 60` tokens per
second, capped at `burst` tokens. When empty, the server returns
`429 Too Many Requests` with a `Retry-After` header (seconds until
a token is available). The dashboard, CLI, and TUI all honor this
header automatically.

**Bounded state:** Per-principal buckets are evicted LRU when the
table exceeds 4096 entries, preventing memory exhaustion from
token-spray attacks.

**Health endpoint:** `/api/v1/health` is never rate-limited (it has
no auth dependency).

---

### Data Retention

| Field | Type | Default | Description |
|---|---|---|---|
| `compress_closed_after_days` | int | `7` | Days after closing before event logs are compressed |
| `manual_purge_enabled` | bool | — | Enable `agentic-perf purge` command for ticket deletion |

---

## Environment Variables Summary

All config fields can be set in the file. Some also accept environment
variable overrides, which take precedence over the file.

| Variable | Config equivalent |
|---|---|
| `AGENTIC_PERF_HOME` | Base directory (default `~/.agentic-perf`) |
| `AGENTIC_PERF_ARTIFACTS` | Benchmark artifact directory (default `$AGENTIC_PERF_HOME/artifacts`) |
| `LLM_PROVIDER` | `llm.provider` |
| `LLM_MODEL` | `llm.model` |
| `LLM_BACKEND` | `llm.backend` |
| `LLM_TIMEOUT` | `llm.timeout` |
| `LLM_REASONING_EFFORT` | `llm.reasoning_effort` |
| `ANTHROPIC_API_KEY` | API key for Claude provider |
| `ANTHROPIC_VERTEX_PROJECT_ID` | `llm.project_id` |
| `CLOUD_ML_REGION` | `llm.region` |
| `GOOGLE_API_KEY` / `GEMINI_API_KEY` | `llm.gemini_api_key` |
| `OPENAI_API_KEY` | API key for OpenAI provider |
| `OPENAI_BASE_URL` | `llm.base_url` |
| `STATE_STORE_URL` | `state_store.url` |
| `POLL_INTERVAL` | `poll_interval` |
| `SSH_KEY` | `ssh_key` |
| `SSH_KEY_VAULT_SECRET` | `ssh_key_vault_secret` |
| `CRUCIBLE_HOME` | `crucible_home` |
| `ZATHRAS_HOME` | `zathras_home` |
| `HARNESS_REPOS` | `harness_repos` (JSON string) |
| `AGENT_TASK_TIMEOUT` | `agent_task_timeout` |
| `STALE_TASK_TIMEOUT` | `stale_task_timeout` |
| `INTROSPECTION_ENABLED` | `introspection.enabled` |
| `AGENTIC_PERF_BWS_TOKEN` | Bitwarden SM access token (no config.json equivalent) |
| `SECRETS_BACKEND` | `"local"` (default; only supported value) |
| `SECRETS_PATH` | Override secrets directory path |
