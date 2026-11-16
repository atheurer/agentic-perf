# State-store REST API reference

Status: current route inventory. Base URL is `http://HOST:8090/api/v1` (the
port is configurable). FastAPI is the executable schema: `/openapi.json`,
`/docs`, and `/redoc` are served by the state store.

## Authentication and authorization

Send `Authorization: Bearer TOKEN` to `/api/v1/*`. The deployment token is
`$AGENTIC_PERF_API_TOKEN` or `AGENTIC_PERF_SECRETS/api-token` (default
`~/.agentic-perf/secrets/api-token`). In multi-user mode user tokens are
managed by the user endpoints. The deployment principal is an admin/service.
With `auth.anonymous_read=true`, unauthenticated GET/HEAD/OPTIONS requests are
read-only; a supplied token is still authenticated. Failed authentication is
401 (or 429 when the auth-failure limiter blocks the client); rate-limited
authenticated requests are 429. Ownership and admin checks may return 403.

## Health, identity, tickets, and transitions

| Method | Path | Body/query | Purpose |
|---|---|---|---|
| GET | `/health` | — | health and ticket counts; no bearer required |
| GET | `/whoami` | — | authenticated principal |
| POST | `/tickets` | `CreateTicketRequest`: `summary`, `description`, optional `custom_fields`, `owners` | create; 200/201 or validation/auth/quota error |
| GET | `/tickets` | optional `status` | list visible tickets |
| GET | `/tickets/{ticket_id}` | — | ticket detail |
| PATCH | `/tickets/{ticket_id}/fields` | `{ "fields": { ... } }` | update custom fields |
| DELETE | `/tickets/{ticket_id}` | — | archive a closed ticket; it is not purge |
| GET | `/tickets/{ticket_id}/transitions` | — | valid next statuses |
| POST | `/tickets/{ticket_id}/transition` | `{ "status": "...", "comment": "..." }` | perform a valid transition |
| GET | `/tickets/since/{seq}` | — | tickets changed after sequence |

Example:

```bash
TOKEN=$(cat "${AGENTIC_PERF_SECRETS:-$HOME/.agentic-perf/secrets}/api-token")
curl -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/v1/tickets
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  http://localhost:8090/api/v1/tickets \
  -d '{"summary":"smoke","description":"mock pipeline"}'
```

## Events, usage, artifacts, and controls

| Method | Path | Body/query | Purpose |
|---|---|---|---|
| GET | `/tickets/{id}/events` | `since`, `limit` | ticket event records after a sequence |
| GET | `/tickets/{id}/transcript` | `agent` | transcript view, optionally filtered by agent |
| GET | `/tickets/{id}/usage` | — | ticket usage |
| GET | `/usage/summary` | — | aggregate usage |
| GET | `/usage/by-user` | query filters | usage by principal |
| GET | `/tickets/{id}/artifacts` | — | artifact metadata |
| GET | `/tickets/{id}/artifacts/download/{file_path}` | — | download one artifact |
| GET | `/tickets/{id}/artifacts/archive` | — | download artifact tarball |
| GET | `/events/stream` | optional filters | SSE event stream |
| POST | `/tickets/{id}/stop` | `{ "mode": "graceful"\|"hard" }` | request agent stop |
| POST | `/tickets/{id}/abort` | `{ "reason": "..." }` | abort into cleanup |
| POST | `/stop-all` | stop body | stop all active agents |
| POST | `/tickets/{id}/force-close` | — | admin emergency closure |
| POST | `/tickets/{id}/interject` | interjection body | inject user guidance |

## Claims, ownership, users, groups, and webhooks

| Method | Path | Body/query |
|---|---|---|
| POST | `/tickets/{id}/claim` | `ClaimRequest` (`owner`, `duration_seconds`) |
| DELETE | `/tickets/{id}/claim` | `ClaimRequest` |
| POST | `/tickets/{id}/claim/renew` | `ClaimRequest` |
| GET | `/tickets/claim-reconciliation` | `after`, `limit` |
| GET | `/tickets/{id}/owners` | — |
| PUT/DELETE | `/tickets/{id}/owners/{username}` | — |
| POST | `/tickets/{id}/comments` | `AddCommentRequest` |
| GET | `/tickets/{id}/comments` | — |
| GET | `/audit` | audit query parameters |
| GET | `/webhooks` | — |
| POST | `/webhooks/{source}` | webhook payload |
| POST | `/users` | `CreateUserRequest` |
| GET | `/users` | — |
| PATCH | `/users/{username}` | `UpdateUserRequest`: `allowed_sources`, `max_requests_per_hour` (partial; omitted fields unchanged; `allowed_sources` is full replacement) |
| POST | `/users/{username}/disable` | — |
| POST | `/users/{username}/enable` | — |
| POST | `/users/{username}/rotate-token` | — |
| POST/DELETE | `/users/{username}/admin` | — |
| PUT/DELETE | `/users/{username}/quota` | `UserQuota` |
| POST | `/groups` | `CreateGroupRequest` |
| GET | `/groups` | — |
| DELETE | `/groups/{name}` | — |
| PUT/DELETE | `/groups/{name}/members/{username}` | — |
| PUT/DELETE | `/groups/{name}/quota` | `UserQuota` |

### Closed-ticket claim reconciliation

`GET /tickets/claim-reconciliation` is an authenticated claim-control endpoint
for orchestrator and administrative operators. It accepts `after`, an opaque
ticket-ID cursor from a prior page, and `limit` from 1 through 500. The state
store scans at most 1000 ticket records and bounds both individual records and
the retained page, so a response may have `next_cursor` before the requested
limit is reached.

The response is:

```json
{
  "tickets": [
    {
      "id": "PERF-1",
      "status": "closed",
      "custom_fields": {
        "claim": {
          "claim_id": "claim-id",
          "owner": "owner",
          "session_id": "session-id",
          "epoch": 1,
          "expires": "2026-01-01T00:00:00Z"
        },
        "deposed_claim_reconciliation": {
          "claim_id": "claim-id"
        }
      }
    }
  ],
  "next_cursor": "PERF-1",
  "read_errors": false
}
```

Only closed tickets that still carry a claim are returned. `next_cursor` is
`null` after a complete scan. `read_errors` is `true` when one or more closed
records could not be inspected or projected; clients must restart that
reconciliation cycle from the returned cursor rather than treating the page as
complete. The optional `deposed_claim_reconciliation` marker exposes only the
previously reconciled `claim_id`.

### Orchestrator lease control

All `/control/orchestrator-lease` routes require a service principal or an
administrator. Lease operations are fenced by a durable, state-store-owned
lease and run on a bounded control-plane executor.

`GET /control/orchestrator-lease` returns `200` with `{"lease": null}` when no
lease is held, or:

```json
{
  "lease": {
    "session_id": "01943c5e-6c14-7a54-9d31-3db1cd5061a5",
    "instance_name": "orchestrator-1",
    "host": "host",
    "pid": 123,
    "process_start_id": "process-start-id",
    "epoch": 1,
    "acquired_at": "2026-01-01T00:00:00Z",
    "renewed_at": "2026-01-01T00:00:10Z",
    "expires_at": "2026-01-01T00:01:00Z"
  }
}
```

`POST /control/orchestrator-lease/acquire` accepts the lease identity
`session_id` (UUID), `instance_name`, `host`, `pid`, and `process_start_id`,
plus `ttl_seconds` from greater than 0 through 3600. It returns the same lease
object as inspection. If another holder is active, it returns `409` with
`detail.error: "lease_held"`, redacted `detail.holder`, and
`detail.remaining_seconds`; the holder's `session_id` is never returned.

`POST /control/orchestrator-lease/renew` accepts `session_id`, the current
positive `epoch`, and `ttl_seconds` with the same bounds. It returns the
renewed lease object, or `409` if the lease is not owned by that session, has
expired, or its epoch does not match.

`POST /control/orchestrator-lease/release` accepts `session_id` and `epoch`. It
returns `200` with `{"released": true}` when the lease is removed and
`{"released": false}` when no matching lease exists.

The final route behavior, Pydantic schemas, and exact status codes are always
the live OpenAPI document. Routes used only by orchestrator/admin control are
marked by their purpose above; clients should prefer ticket/list/detail,
events, artifacts, transitions, comments, and stop APIs.

## Trace query and export

Trace projections are available at `/traces/query`, `/traces/export`,
`/traces/tickets/{id}`, `/traces/invocations/{id}`, and
`/traces/actions/{id}`. Common selectors are `ticket_id`, `trace_id`,
`invocation_id`, `action_id`, `parent_action_id`, `action_type`,
`lifecycle_state`, `outcome`, `producer_component`, `retry_kind`,
`idempotency_outcome`, `since`, `until`, `causal`, `limit`, `cursor`, and
`include_payloads`. Query and export continuation responses expose
`has_more` and opaque `next_cursor` values.

Export `format` may be `json`, `jsonl`, or `csv`. Every export body carries the
complete manifest, including continuation metadata and `event_content_digest`:
SHA-256 over the canonical event body excluding the manifest wrapper. The
`X-Trace-Manifest` header is bounded to 64 KiB; for larger manifests it carries
continuation metadata, the content digest, and a SHA-256 digest of the complete
manifest. That `full_manifest_digest` is the UTF-8 SHA-256 of the complete
manifest serialized as compact JSON with `json.dumps(..., separators=(",", ":"))`,
default JSON encoder settings (`ensure_ascii=True`), and insertion-order keys.
Trace reads require authentication plus service/admin or ticket ownership
authorization. Owner responses are redacted and omit blob
references/digests; only authorized detailed principals may retrieve an
existing, verified `sha256:<64 lowercase hex>` payload blob.
If terminal audit admission is exhausted, query and export requests fail closed
with HTTP 503. The state store first attempts one strict failure audit on a
dedicated no-queue fallback executor.
Response delivery is bounded to 5 seconds. If it has not started, a timed-out
query/export response is replaced with HTTP 503 and records a failed terminal
audit. If the ASGI response has already started, delivery cannot be replaced,
so the connection is closed after recording an indeterminate terminal audit.
Success audits after a delivered response are best-effort and do not change the
delivered HTTP 200 response if auditing fails.

Durable fenced operations are service-only. `GET /traces/operations`
requires the `operation_key` query parameter and returns that operation's
state, request hash, result descriptor, and external IDs for internal
reconciliation. It returns `404` when the operation is unknown.
