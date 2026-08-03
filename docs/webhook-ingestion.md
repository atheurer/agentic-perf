# Webhook Ingestion

Accept HTTP POST webhooks from external systems to automatically
create investigation tickets. Supports any system that can send
JSON payloads — Horreum anomaly detection, CI pipelines,
Prometheus alertmanager, custom monitoring, etc.

## How It Works

```
External system detects anomaly
  → POST /api/v1/webhooks/{source}?token=xxx
    → translator maps payload to ticket fields
      → dedup check against open tickets
        → ticket created → triage_pending
          → autonomous investigation begins
```

Each **source** has a pluggable translator module that maps the
external payload to agentic-perf ticket fields. The raw payload
is always preserved on the ticket for agent access.

## Endpoints

### List sources

```
GET /api/v1/webhooks
Authorization: Bearer <token>
```

Returns the list of registered translator sources:

```json
{"sources": ["generic", "horreum"]}
```

### Receive webhook

```
POST /api/v1/webhooks/{source}?token=<service-account-token>
Content-Type: application/json

{ ... source-specific payload ... }
```

Auth via query string `?token=` or `Authorization: Bearer` header.
Returns:

```json
{"status": "created", "ticket_id": "PERF-AB12CD34"}
```

Or if a duplicate is detected:

```json
{"status": "duplicate", "ticket_id": "PERF-EXISTING", "dedup_key": "..."}
```

## Service Accounts

Webhook sources authenticate using **service accounts** — special
user accounts with source IP validation.

### Create a service account

```bash
curl -X POST \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "webhook-horreum",
    "service_account": true,
    "allowed_sources": ["10.128.0.0/14"]
  }' \
  $AP_URL/api/v1/users
```

The response includes a one-time token. Configure this token in the
external system's webhook URL.

### Service account properties

| Field | Required | Description |
|---|---|---|
| `service_account` | Yes | Must be `true` |
| `allowed_sources` | Yes | List of IP addresses or CIDR ranges |
| `max_requests_per_hour` | No | Rate limit (default: unlimited) |

### Invariants

- Service accounts **cannot be admins** (enforced in code)
- Service accounts **require at least one** `allowed_sources` entry
- Every request validates the source IP against `allowed_sources`
- `X-Forwarded-For` is respected behind reverse proxies

### Regular user access

Regular users can POST to webhook endpoints using standard bearer
auth (no source IP check). This enables testing translators without
configuring a service account.

## Built-in Translators

### `generic`

Passthrough translator. Stores the raw payload on the ticket:

```json
POST /api/v1/webhooks/generic

{
  "summary": "CPU regression on board-05",
  "description": "Detailed context here..."
}
```

Creates a ticket with:
- `summary` and `description` from payload (or defaults)
- `custom_fields.trigger_source`: `"generic"`
- `custom_fields.raw_payload`: the full payload

Dedup key: `payload.id` if present.

### `horreum`

Maps Horreum `change/new` webhook payloads. Configure a Horreum
HTTP action with event type `change/new` pointing at:

```
https://<host>/api/v1/webhooks/horreum?token=<service-account-token>
```

The Horreum admin must add the URL prefix to the allowed sites list.

#### Payload mapping

Horreum sends a `Change.Event` JSON:

```json
{
  "testName": "boot-time",
  "testId": 3,
  "change": {
    "id": 42,
    "variable": {"name": "total_boot_s", "group": "timing"},
    "dataset": {"id": 101, "runId": 55, "ordinal": 1},
    "description": "Relative difference of means exceeded threshold",
    "timestamp": "2024-03-15T10:30:00Z"
  },
  "notify": true
}
```

The translator creates a ticket with:

| Ticket field | Source |
|---|---|
| `summary` | `"Horreum change detected: {testName} / {variable.name}"` |
| `anomaly_context.source` | `"horreum"` |
| `anomaly_context.test_name` | `testName` |
| `anomaly_context.variable_name` | `change.variable.name` |
| `anomaly_context.variable_group` | `change.variable.group` |
| `anomaly_context.run_id` | `change.dataset.runId` |
| `anomaly_context.dataset_id` | `change.dataset.id` |
| `anomaly_context.change_id` | `change.id` |
| `anomaly_context.change_description` | `change.description` |
| `anomaly_context.timestamp` | `change.timestamp` |
| `trigger_payload` | Full raw payload |

Dedup key: `horreum:change:{change.id}`

The gathering_context agent uses the `run_id` and `test_name` from
`anomaly_context` to query the Domain MCP for full context — baseline
stats, metric values, and historical comparisons. See
[Agent Grounding](#agent-grounding) below.

## Adding a New Translator

1. Create `state_store/webhooks/<source>.py` with two functions:

```python
def translate(payload: dict) -> dict:
    """Map payload to ticket fields.

    Must return:
      summary: str
      description: str
      custom_fields: dict (include trigger_source)
    """
    ...

def dedup_key(payload: dict) -> str | None:
    """Return a dedup key, or None to skip dedup."""
    ...
```

2. Register in `state_store/webhooks/registry.py`:

```python
_TRANSLATORS: dict[str, str] = {
    "generic": "state_store.webhooks.generic",
    "horreum": "state_store.webhooks.horreum",
    "my_source": "state_store.webhooks.my_source",  # add here
}
```

3. Add tests in `tests/test_webhooks.py`.

## Agent Grounding

Webhook tickets arrive without hardware directives (`board_selector`,
`image_version`, `harness`). Unlike manually submitted tickets where
the user provides these, the gathering_context agent must resolve
them from the alert data.

### How it works

1. The gathering_context agent detects webhook tickets via
   `trigger_source` or `anomaly_context.source` on the ticket.
2. If the `get_run_info` Domain MCP tool is available, the agent
   calls it with the `run_id` or `dataset_id` from the anomaly
   context to get the target/board type, OS version, and labels.
3. The agent maps the metadata to directives:
   - `target` → `board_selector` (e.g., `board-type=renesas-rcar-s4`)
   - `os_id` or labels → `image_version` (e.g., `AutoSD-10`)
   - `test_name` / description → `harness` (e.g., `boot-time`)
4. The resolved directives are included in the
   `submit_gathering_context_result` call and written to the ticket
   for downstream agents (resource, platform, benchmark).

### Fallback

If `get_run_info` is not available or returns no data, the agent
infers what it can from the anomaly context (e.g., `test_name` may
indicate the harness) and notes the missing fields. The
investigation will request human guidance for unresolved directives.

### Prerequisite

The Domain MCP server must expose a `get_run_info` tool that returns
metadata for a Horreum run or dataset by ID. Add `get_run_info` to
the `enabled_tools` list for `gathering_context` in `config.json`.

## Dedup

Before creating a ticket, the endpoint checks all open tickets
for a matching `trigger_source` + `dedup_key`. If a match is found,
the webhook returns the existing ticket ID with `status: "duplicate"`
instead of creating a new one.

A `duplicate_suppressed` event is emitted on the existing ticket so
the dedup is visible in the dashboard timeline.

This prevents alert storms from creating duplicate investigations
when the same anomaly triggers multiple webhooks.

## Rate Limiting

Service accounts can set `max_requests_per_hour` to limit webhook
frequency. Exceeding the limit returns `429 Too Many Requests`.

The rate limit counter is in-memory and resets on service restart.

## Security

- **Token + IP validation**: Service accounts require both a valid
  token and a matching source IP. A compromised token alone is
  insufficient without network access from an allowed source.
- **No token in logs**: The token is in the query string, which
  can appear in reverse proxy logs. For internal deployments,
  use a cluster-internal service (no external Route) to avoid
  exposure. The token provides defense-in-depth, not sole auth.
- **Service accounts cannot be admins**: Enforced at three levels:
  creation (rejects `is_admin=True`), promotion (`set_admin` raises
  error), and runtime (auth middleware strips admin even if
  `users.json` is tampered).
