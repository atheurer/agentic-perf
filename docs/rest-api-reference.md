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
| GET | `/tickets/{id}/owners` | — |
| PUT/DELETE | `/tickets/{id}/owners/{username}` | — |
| POST | `/tickets/{id}/comments` | `AddCommentRequest` |
| GET | `/tickets/{id}/comments` | — |
| GET | `/audit` | audit query parameters |
| GET | `/webhooks` | — |
| POST | `/webhooks/{source}` | webhook payload |
| POST | `/users` | `CreateUserRequest` |
| GET | `/users` | — |
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

The final route behavior, Pydantic schemas, and exact status codes are always
the live OpenAPI document. Routes used only by orchestrator/admin control are
marked by their purpose above; clients should prefer ticket/list/detail,
events, artifacts, transitions, comments, and stop APIs.
