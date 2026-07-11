# Configuration Reference

All configuration lives in `~/.agentic-perf/config.json`. Every field is
optional — sensible defaults are used when a field is absent.

The config path can be changed by setting the `AGENTIC_PERF_HOME`
environment variable (defaults to `~/.agentic-perf`).

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
        "gemini_api_key": null,
        "timeout": 120
    },
    "agent_models": {
        "review": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6"
        },
        "benchmark": {
            "provider": "gemini",
            "model": "gemini-2.5-flash"
        },
        "default": {
            "provider": "claude",
            "model": "claude-sonnet-4-6"
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
| `model` | string | `"claude-sonnet-4-6"` | `LLM_MODEL` | Model identifier |
| `backend` | string | — | `LLM_BACKEND` | `"vertex"` for Vertex AI, `"direct"` for direct API |
| `project_id` | string | — | `ANTHROPIC_VERTEX_PROJECT_ID` | GCP project ID (Vertex AI backends) |
| `region` | string | — | `CLOUD_ML_REGION` | Cloud region (Vertex AI backends) |
| `base_url` | string | — | `OPENAI_BASE_URL` | Base URL for OpenAI-compatible endpoints |
| `gemini_api_key` | string | — | `GOOGLE_API_KEY` or `GEMINI_API_KEY` | API key for Gemini provider |
| `timeout` | float | `120` | `LLM_TIMEOUT` | Per-request timeout in seconds. `0` disables. |

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

---

### `agent_models` — Per-Agent LLM Overrides

Override the LLM provider and model for specific agent types. This
lets you run different agents on different models — for example, a
cheaper model for triage and a more capable one for review.

```json
{
    "agent_models": {
        "review": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6"
        },
        "default": {
            "provider": "gemini",
            "model": "gemini-2.5-flash"
        }
    }
}
```

**Resolution order:**
1. `agent_models.<agent_type>` — exact match for the agent
2. `agent_models.default` — fallback if no exact match
3. Top-level `llm.provider` / `llm.model` — global default

Each override object supports `provider` and `model` keys.

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
        "model": "claude-opus-4-8"
    }
}
```

This override is cleared after the agent completes.

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
| `crucible_home` | string | `"/opt/crucible"` | `CRUCIBLE_HOME` | Path to crucible installation |
| `zathras_home` | string | `""` | `ZATHRAS_HOME` | Path to zathras installation |

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
| `LLM_PROVIDER` | `llm.provider` |
| `LLM_MODEL` | `llm.model` |
| `LLM_BACKEND` | `llm.backend` |
| `LLM_TIMEOUT` | `llm.timeout` |
| `ANTHROPIC_API_KEY` | API key for Claude provider |
| `ANTHROPIC_VERTEX_PROJECT_ID` | `llm.project_id` |
| `CLOUD_ML_REGION` | `llm.region` |
| `GOOGLE_API_KEY` / `GEMINI_API_KEY` | `llm.gemini_api_key` |
| `OPENAI_API_KEY` | API key for OpenAI provider |
| `OPENAI_BASE_URL` | `llm.base_url` |
| `STATE_STORE_URL` | `state_store.url` |
| `POLL_INTERVAL` | `poll_interval` |
| `SSH_KEY` | `ssh_key` |
| `CRUCIBLE_HOME` | `crucible_home` |
| `ZATHRAS_HOME` | `zathras_home` |
| `HARNESS_REPOS` | `harness_repos` (JSON string) |
| `AGENT_TASK_TIMEOUT` | `agent_task_timeout` |
| `STALE_TASK_TIMEOUT` | `stale_task_timeout` |
