# Jira Cloud Polling Integration

## Context

The agentic-perf prototype currently uses a local FastAPI state store (`state_store/`) as a mock Jira substitute. The orchestrator polls it via REST, agents read/write tickets through the same API. This document specifies how to replace the local state store with Jira Cloud as the real backend, using **outbound polling** (no webhooks, no inbound ports).

### Why polling instead of webhooks

- We don't have Jira global admin (only YOUR_PROJECT project admin)
- Jira Cloud can't reach private IPs inside Red Hat's network
- Webhooks would require a public relay endpoint + message queue
- Polling is zero-infrastructure and works today with existing API credentials

### Jira instance details

- **URL**: `https://your-org.atlassian.net`
- **Project**: `YOUR_PROJECT` (ID: XXXXX)
- **Auth**: Basic auth — `user@example.com` + API token
- **API token**: stored in env var `JIRA_API_TOKEN`
- **API base**: `https://your-org.atlassian.net/rest/api/2`

---

## Architecture: State Store Adapter Pattern

The orchestrator and agents currently talk to the state store via HTTP REST calls. Rather than rewriting all call sites, we introduce a **Jira adapter** — a new state store backend that implements the same FastAPI API but proxies all operations to Jira Cloud.

```
Orchestrator ──poll──► State Store API (port 8080) ──► Jira Cloud REST API
Agents ──────────────► State Store API (port 8080) ──► Jira Cloud REST API
```

This means:
- The FastAPI server stays, same endpoints, same port
- `state_store/store.py` gets a `JiraStore` class alongside the existing `TicketStore`
- A config flag (`--backend jira` or env `STATE_STORE_BACKEND=jira`) picks which one
- The in-memory store remains for local dev/testing

### Alternative: direct Jira calls

A simpler alternative is to skip the adapter and have `orchestrator/poller.py` and `agents/base.py` call Jira directly (switchable by config). This avoids running the FastAPI server entirely when using Jira. Either approach works — the adapter is cleaner for incremental migration; direct calls are simpler if you're confident in the switch.

---

## Jira Project Setup Required

### Workflow statuses

Create a custom workflow for YOUR_PROJECT with these statuses (matching `state_store/models.py`):

| Internal status | Jira status name | Category |
|---|---|---|
| `new` | `New` | To Do |
| `triage_pending` | `Triage Pending` | In Progress |
| `awaiting_hardware` | `Awaiting Hardware` | In Progress |
| `awaiting_provision` | `Awaiting Provision` | In Progress |
| `executing_benchmark` | `Executing Benchmark` | In Progress |
| `awaiting_review` | `Awaiting Review` | In Progress |
| `awaiting_teardown` | `Awaiting Teardown` | In Progress |
| `awaiting_customer_guidance` | `Awaiting Customer Guidance` | In Progress |
| `closed` | `Closed` | Done |

As a project admin, you can create a custom workflow in project settings. The transitions should match `VALID_TRANSITIONS` from `state_store/store.py`.

**Shortcut for prototyping**: Skip the full custom workflow. Use a single Jira text field (e.g., `Agent Status`) to track the internal status, and leave the Jira workflow simple (To Do → In Progress → Done). The poller reads `Agent Status` instead of the Jira workflow status. This avoids workflow admin entirely.

### Custom fields

These are the custom fields agents write to. In Jira Cloud, project admins can create custom fields scoped to the project.

| Field name | Jira field type | Used by |
|---|---|---|
| `Agent Status` | Short text | All (internal state machine) |
| `Parsed Specs` | Paragraph (stores JSON) | Triage agent |
| `Hypothesis` | Paragraph | Triage agent |
| `Benchmark Suite` | Short text | Triage agent |
| `Absent Suite` | Checkbox | Triage agent |
| `Assigned Hardware IPs` | Paragraph (stores JSON) | Resource agent |
| `SSH User` | Short text | Resource agent |
| `SSH Key Path` | Short text | Resource agent |
| `Lease Expiration` | Date Time Picker | Resource agent |
| `Run ID` | Short text | Benchmark agent |
| `Benchmark Status` | Short text | Benchmark agent |
| `Run File Used` | Paragraph (stores JSON) | Benchmark agent |
| `Benchmark Duration` | Number | Benchmark agent |
| `Review Summary` | Paragraph | Review agent |
| `Verdict` | Short text | Review agent |
| `Detailed Analysis` | Paragraph | Review agent |
| `Key Metrics` | Paragraph (stores JSON) | Review agent |
| `Recommendations` | Paragraph (stores JSON) | Review agent |
| `Follow Up Needed` | Checkbox | Review agent |
| `Previous Status` | Short text | System (HITL resume) |

**Simpler alternative**: Use just TWO custom fields:
- `Agent Status` (short text) — the state machine status
- `Agent Data` (paragraph) — a single JSON blob containing all custom_fields

This reduces Jira setup to 2 fields instead of 18. Agents serialize/deserialize from one JSON field. Trade-off: less visibility in Jira's UI (no filtering by individual fields), but massively simpler setup.

---

## Jira REST API Mapping

### Field ID discovery

Custom field IDs in Jira Cloud are like `customfield_12345`. You need to discover them once:

```bash
# List all fields, find your custom ones
curl -s -u "$JIRA_USER:$JIRA_TOKEN" \
  "https://your-org.atlassian.net/rest/api/2/field" | \
  jq '.[] | select(.custom == true) | {id, name}'
```

Store the mapping in config:

```python
JIRA_FIELD_MAP = {
    "agent_status": "customfield_XXXXX",
    "agent_data": "customfield_YYYYY",
}
```

### Polling for tickets (replaces `GET /api/v1/tickets?status=X`)

```python
import httpx
from datetime import datetime, timedelta

JIRA_BASE = "https://your-org.atlassian.net/rest/api/2"
JIRA_AUTH = ("user@example.com", os.environ["JIRA_API_TOKEN"])
AGENT_STATUS_FIELD = "customfield_XXXXX"  # discovered above
AGENT_DATA_FIELD = "customfield_YYYYY"

async def fetch_tickets_by_status(status: str) -> list[dict]:
    """Poll Jira for YOUR_PROJECT tickets in a given agent status."""
    jql = f'project = YOUR_PROJECT AND cf[{AGENT_STATUS_FIELD_NUM}] = "{status}"'
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{JIRA_BASE}/search",
            auth=JIRA_AUTH,
            params={
                "jql": jql,
                "fields": f"summary,description,comment,{AGENT_STATUS_FIELD},{AGENT_DATA_FIELD},created,updated",
                "maxResults": 50,
            },
        )
        r.raise_for_status()
        return [_jira_to_internal(issue) for issue in r.json()["issues"]]


def _jira_to_internal(issue: dict) -> dict:
    """Convert Jira issue JSON to internal ticket format."""
    fields = issue["fields"]
    agent_data = {}
    raw = fields.get(AGENT_DATA_FIELD)
    if raw:
        try:
            agent_data = json.loads(raw)
        except json.JSONDecodeError:
            pass

    comments = []
    for c in fields.get("comment", {}).get("comments", []):
        comments.append({
            "id": c["id"],
            "author": c["author"]["displayName"],
            "body": c["body"],
            "created_at": c["created"],
        })

    return {
        "id": issue["key"],                          # e.g. "YOUR_PROJECT-42"
        "summary": fields.get("summary", ""),
        "description": fields.get("description", ""),
        "status": fields.get(AGENT_STATUS_FIELD, "new"),
        "custom_fields": agent_data,
        "comments": comments,
        "created_at": fields.get("created"),
        "updated_at": fields.get("updated"),
        "previous_status": agent_data.get("_previous_status"),
        "transition_seq": 0,                         # not used with Jira
    }
```

### Polling for changes (replaces `GET /api/v1/tickets/since/{seq}`)

```python
async def fetch_changed_tickets(since_minutes: int = 5) -> list[dict]:
    """Poll for recently updated YOUR_PROJECT tickets."""
    jql = f'project = YOUR_PROJECT AND updated >= "-{since_minutes}m" ORDER BY updated DESC'
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{JIRA_BASE}/search",
            auth=JIRA_AUTH,
            params={
                "jql": jql,
                "fields": f"summary,description,comment,{AGENT_STATUS_FIELD},{AGENT_DATA_FIELD},created,updated",
                "maxResults": 50,
            },
        )
        r.raise_for_status()
        return [_jira_to_internal(issue) for issue in r.json()["issues"]]
```

### Get single ticket (replaces `GET /api/v1/tickets/{ticket_id}`)

```python
async def get_ticket(ticket_id: str) -> dict:
    """Fetch a single Jira issue by key (e.g., YOUR_PROJECT-42)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{JIRA_BASE}/issue/{ticket_id}",
            auth=JIRA_AUTH,
            params={
                "fields": f"summary,description,comment,{AGENT_STATUS_FIELD},{AGENT_DATA_FIELD},created,updated",
            },
        )
        r.raise_for_status()
        return _jira_to_internal(r.json())
```

### Transition status (replaces `POST /api/v1/tickets/{id}/transition`)

```python
async def transition_ticket(
    ticket_id: str, new_status: str, comment: str | None = None
) -> dict:
    """Update the agent status field and optionally add a comment."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        # Read current agent_data to preserve it and update _previous_status
        current = await get_ticket(ticket_id)
        agent_data = current["custom_fields"].copy()
        agent_data["_previous_status"] = current["status"]

        # Update agent_status field + agent_data
        r = await client.put(
            f"{JIRA_BASE}/issue/{ticket_id}",
            auth=JIRA_AUTH,
            json={
                "fields": {
                    AGENT_STATUS_FIELD: new_status,
                    AGENT_DATA_FIELD: json.dumps(agent_data),
                }
            },
        )
        r.raise_for_status()

        # Add transition comment if provided
        if comment:
            await add_comment(ticket_id, "system", comment)

        return await get_ticket(ticket_id)
```

### Update custom fields (replaces `PATCH /api/v1/tickets/{id}/fields`)

```python
async def update_fields(ticket_id: str, fields: dict) -> dict:
    """Merge fields into the agent_data JSON blob."""
    current = await get_ticket(ticket_id)
    agent_data = current["custom_fields"].copy()
    agent_data.update(fields)

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.put(
            f"{JIRA_BASE}/issue/{ticket_id}",
            auth=JIRA_AUTH,
            json={
                "fields": {
                    AGENT_DATA_FIELD: json.dumps(agent_data),
                }
            },
        )
        r.raise_for_status()
        return await get_ticket(ticket_id)
```

### Add comment (replaces `POST /api/v1/tickets/{id}/comments`)

```python
BOT_DISPLAY_NAME = "perf-agent"  # or a service account name

async def add_comment(ticket_id: str, author: str, body: str) -> dict:
    """Add a comment to a Jira issue. Author is prepended to the body
    since Jira comments are always posted as the authenticated user."""
    prefixed_body = f"**[{author}]** {body}"

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            f"{JIRA_BASE}/issue/{ticket_id}/comment",
            auth=JIRA_AUTH,
            json={"body": prefixed_body},
        )
        r.raise_for_status()
        data = r.json()
        return {
            "id": data["id"],
            "author": author,
            "body": body,
            "created_at": data["created"],
        }
```

---

## Orchestrator Changes

### Modified poll loop

The main change in `orchestrator/main.py` is swapping the HTTP call to the local store with a direct Jira call (or proxied through the adapter):

```python
# orchestrator/main.py — Jira-aware version

from orchestrator.jira_client import fetch_tickets_by_status  # new module

STATUS_AGENT_MAP = {
    "triage_pending":       "triage",
    "awaiting_hardware":    "resource_create",
    "awaiting_provision":   "provisioning",
    "executing_benchmark":  "benchmark",
    "awaiting_review":      "review",
    "awaiting_teardown":    "resource_teardown",
}

async def poll_loop(config):
    dispatcher = Dispatcher(config, llm, skills)

    while True:
        for status in STATUS_AGENT_MAP:
            try:
                tickets = await fetch_tickets_by_status(status)
            except Exception:
                logger.exception(f"Jira poll failed for status={status}")
                continue

            for ticket in tickets:
                tid = ticket["id"]
                if dispatcher.is_active(tid):
                    continue
                dispatcher.mark_active(tid)
                asyncio.create_task(run_agent_task(dispatcher, status, tid))

        await asyncio.sleep(config.poll_interval)  # default 3s
```

### Jira rate limiting

Jira Cloud rate limits are generous but real. With 7 statuses polled every 3 seconds, that's ~140 requests/minute. Jira Cloud's documented limit is ~100 requests/minute for basic auth.

**Mitigations:**
1. Increase poll interval to 5-10 seconds (still responsive enough)
2. Batch into one JQL query: `project = YOUR_PROJECT AND cf[XXXXX] IN ("triage_pending", "awaiting_hardware", ...)` — one call instead of 7
3. Use the `updated >= "-Nm"` approach to only fetch recently changed tickets

**Recommended: single-query approach**

```python
async def poll_all_actionable_tickets() -> dict[str, list[dict]]:
    """Single JQL query for all actionable tickets, grouped by status."""
    actionable = list(STATUS_AGENT_MAP.keys())
    status_list = ", ".join(f'"{s}"' for s in actionable)
    jql = f'project = YOUR_PROJECT AND cf[{FIELD_NUM}] IN ({status_list})'

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{JIRA_BASE}/search",
            auth=JIRA_AUTH,
            params={"jql": jql, "fields": "...", "maxResults": 100},
        )
        r.raise_for_status()

    by_status: dict[str, list[dict]] = {s: [] for s in actionable}
    for issue in r.json()["issues"]:
        ticket = _jira_to_internal(issue)
        status = ticket["status"]
        if status in by_status:
            by_status[status].append(ticket)

    return by_status
```

This brings polling down to **1 Jira API call per poll cycle** — ~6-12/minute at 5-10s interval.

---

## Infinite Loop Prevention

The architecture doc (Section 6.1) warns about agents reading their own comments and re-triggering. With polling, the risk is different: the orchestrator might see a ticket in `triage_pending` status, dispatch the triage agent, and on the next poll cycle (before the agent finishes), see it again.

The existing `dispatcher.is_active(tid)` check handles this. But also:

1. **Comment filtering**: When agents read comments to detect human input (HITL resume), they must filter out their own comments. Since all agent comments go through one Jira user, use the `**[agent-name]**` prefix to identify bot comments:

```python
def is_human_comment(comment: dict) -> bool:
    """Returns True if the comment was NOT posted by an agent."""
    return not comment["body"].startswith("**[")
```

2. **Status guard**: Before an agent starts processing, it should re-read the ticket to confirm the status hasn't changed (another agent might have grabbed it):

```python
async def run(self, ticket_id: str) -> None:
    ticket = await self._get_ticket(ticket_id)
    if ticket["status"] != self.expected_status:
        logger.info(f"Ticket {ticket_id} status changed, skipping")
        return
    # ... proceed with agent logic
```

---

## HITL (Human-in-the-Loop) with Jira

This is where Jira really shines vs. the mock store. When an agent needs human input:

1. Agent sets `Agent Status` = `awaiting_customer_guidance` and adds a comment with the question
2. The human sees the comment in Jira's UI (they get email notifications automatically)
3. The human replies with a comment
4. The human changes `Agent Status` back to the appropriate status (or a Jira automation rule does this when a non-bot comment is added)

**Detecting human response** (for automatic resume):

```python
async def check_hitl_resume(ticket: dict) -> str | None:
    """If ticket is in HITL pause and has a new human comment, return
    the previous status to resume to."""
    if ticket["status"] != "awaiting_customer_guidance":
        return None

    # Find the most recent human comment after the last bot comment
    comments = ticket["comments"]
    last_bot_idx = -1
    for i, c in enumerate(comments):
        if c["body"].startswith("**["):
            last_bot_idx = i

    has_human_reply = any(
        not c["body"].startswith("**[")
        for c in comments[last_bot_idx + 1:]
    )

    if has_human_reply:
        return ticket.get("previous_status") or ticket["custom_fields"].get("_previous_status")
    return None
```

Then in the poll loop, add a check for HITL tickets that have received a human reply:

```python
# In poll_loop, after the main status scan:
hitl_tickets = await fetch_tickets_by_status("awaiting_customer_guidance")
for ticket in hitl_tickets:
    resume_to = await check_hitl_resume(ticket)
    if resume_to:
        await transition_ticket(ticket["id"], resume_to, "Resuming after human input")
```

---

## Configuration

Add to `orchestrator/config.py`:

```python
@dataclass
class OrchestratorConfig:
    # Existing fields...
    state_store_url: str = "http://localhost:8080"
    poll_interval: float = 3.0
    llm_provider: str = "mock"
    anthropic_api_key: str | None = None
    crucible_home: str = "/opt/crucible"

    # New Jira fields
    backend: str = "local"                          # "local" or "jira"
    jira_url: str = "https://your-org.atlassian.net"
    jira_user: str = "user@example.com"
    jira_project: str = "YOUR_PROJECT"
    jira_agent_status_field: str = ""               # customfield_XXXXX
    jira_agent_data_field: str = ""                  # customfield_YYYYY
    jira_bot_name: str = "perf-agent"               # prefix for bot comments
```

Environment variables:
```bash
export JIRA_API_TOKEN="..."           # API token (never in code)
export STATE_STORE_BACKEND="jira"     # or "local"
export JIRA_AGENT_STATUS_FIELD="customfield_XXXXX"
export JIRA_AGENT_DATA_FIELD="customfield_YYYYY"
```

---

## Implementation Order

1. **Create custom fields in YOUR_PROJECT** — Just `Agent Status` (text) and `Agent Data` (paragraph). Note down the field IDs.
2. **Create `orchestrator/jira_client.py`** — The functions above: `fetch_tickets_by_status`, `get_ticket`, `transition_ticket`, `update_fields`, `add_comment`.
3. **Modify `agents/base.py`** — Add a backend switch so `_get_ticket`, `_transition_ticket`, `_update_fields`, `_add_comment` route to either the local store or Jira client.
4. **Modify `orchestrator/main.py`** — Use the single-query poller when backend is Jira.
5. **Add HITL resume logic** — The `check_hitl_resume` function in the poll loop.
6. **Test with a real YOUR_PROJECT ticket** — Create a test issue, set Agent Status manually, verify the orchestrator picks it up.

---

## Service Account (Future)

Right now everything runs under `user@example.com`. For production:
- Request a Jira service account (e.g., `perfagent-bot@example.com`)
- Give it project access to YOUR_PROJECT
- Use its API token
- This makes bot comments visually distinct from human comments (different author)
- Eliminates the need for the `**[agent-name]**` prefix hack

---

## Jira Automation Rules (Optional Enhancement)

As a YOUR_PROJECT project admin, you can create automation rules that enhance the polling approach:

1. **Auto-resume on human comment**: When a non-bot user comments on a ticket in `Awaiting Customer Guidance`, automatically copy `Previous Status` back to `Agent Status`. This eliminates the need for the human to manually change the status field.

2. **New ticket auto-triage**: When a new ticket is created in YOUR_PROJECT, automatically set `Agent Status` = `triage_pending`.

These are optional — the polling approach works without them — but they improve the human experience.
