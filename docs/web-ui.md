# Web Dashboard

The agentic-perf web dashboard provides a browser-based interface for monitoring tickets and following agent execution in real time. It is served from the same FastAPI process as the state store API (port 8090).

## Architecture

**Single-page app** — one HTML file (`state_store/static/index.html`) with inline CSS and JavaScript. No build step, no dependencies. The app talks to the existing `/api/v1/*` REST endpoints.

**Static file serving** — FastAPI mounts `state_store/static/` and serves `index.html` at `/`. CORS middleware is enabled for future separation of UI and API if needed. All configuration is in `state_store/main.py`.

**Event data** — the `EventBus` (`providers/events.py`) keeps events in memory during the current process lifetime and persists them to `~/.agentic-perf/logs/{ticket_id}.jsonl`. The API falls back to reading JSONL files when the in-memory store is empty (e.g., after a server restart).

## Views

### Ticket List (`#/`)

- Table of all tickets sorted by creation time (newest first)
- Columns: ticket ID, summary, status badge, created, updated
- Status filter dropdown
- Auto-refreshes every 5 seconds
- Click a row to open the ticket detail view

### Ticket Detail (`#/ticket/{id}`)

Split-panel layout with three vertical zones. The ticket header spans
full width at the top. Below it, a two-column layout pairs the main
content column with a sidebar. The sidebar is top-aligned with the
hypothesis card, not the event stream, so it stays visually connected
to the investigation context.

**Ticket header (fixed, full width):**
- Breadcrumb navigation back to dashboard
- Ticket ID, summary, and status badge

**Main content column (left):**
- Hypothesis card — extracted from `custom_fields.hypothesis`, always
  visible when present (styled as a prominent card, not buried in the
  generic fields grid)
- Collapsible description and custom fields sections (closed by default
  to keep the context area compact)
- Event stream (scrollable, fills remaining viewport height) — full
  transaction log rendered as events arrive; only this area scrolls

**Sidebar (right, aligned with hypothesis card):**
- Current ticket status (updates live on transitions)
- Controls: Live/Pause polling, Auto-scroll toggle
- Navigation: Jump to top/bottom of event stream, Collapse/Expand all
  agent sections
- Agent navigator: lists each agent phase with status dot (pulsing
  blue = active, green = done, red = error); click to scroll to that
  agent's section in the event stream
- Event counter
- LLM usage summary with per-agent cost breakdown
- Introspection card (when enabled) — see below

**Transaction log event types** (introspection agent events are
filtered out — they appear in the dedicated introspection card instead):
- `agent_started` — collapsible section header with agent name; contains collapsible system prompt and initial messages
- `llm_response` — shows response text and tool calls; long content auto-collapsed
- `tool_called` / `tool_result` — shows tool name, input, output with collapsible long content
- `transition` — highlighted banner showing the new status
- `comment` — comment body with author
- `agent_finished` / `agent_error` — completion or error markers

**Introspection card:** Appears below the LLM Usage section when
introspection is enabled for the ticket (globally or per-ticket).
Displays the introspection agent's continuous observations:
- Anomaly count with severity breakdown (high/medium/low, color-coded)
- Individual anomaly descriptions with severity badges
- Collapsible narrative log with two entry types:
  - **Mechanical entries** — lightweight event descriptions (agent
    started, tool called, transitioned to status)
  - **LLM observations** — purple-highlighted entries with the
    introspection agent's interpretation of what's happening
    (patterns, root causes, operational concerns), triggered on
    new anomalies or agent transitions
- Status summary (event count, LLM calls, tool errors)
- Narrative auto-scrolls to bottom following the toolbar's Scroll
  toggle; scrolls to bottom when first expanded

The card is hidden until the introspection agent writes its first
observation to `custom_fields.introspection`, then updates on every
poll cycle. When the ticket closes, the card switches to the **final
summary** (`custom_fields.introspection_summary`) with verdict badge,
LLM-generated observations, actionable recommendations by area, and
collapsible anomaly/per-agent detail. Both fields are excluded from
the generic Custom Fields section.

Introspection agent token usage appears in the LLM Usage card
alongside pipeline agents.

**Live polling:** fetches `/api/v1/tickets/{id}/events?since={lastSeq}` every 2 seconds. New events are appended to the event stream container. Auto-scroll keeps the event stream at the bottom unless the user disables it. The context header remains fixed regardless of scroll position.

## API Endpoints Used

| Endpoint | Used by |
|----------|---------|
| `GET /api/v1/tickets` | Ticket list (with optional `?status=` filter) |
| `GET /api/v1/tickets/{id}` | Ticket detail header, description, custom fields |
| `GET /api/v1/tickets/{id}/events?since=N&limit=500` | Live event polling |
| `GET /api/v1/health` | Header health stats (total/active ticket counts) |

## Files

| File | Purpose |
|------|---------|
| `state_store/static/index.html` | The entire dashboard (HTML + CSS + JS) |
| `state_store/main.py` | Static file mount, CORS middleware, `/` route |
| `providers/events.py` | EventBus with in-memory + JSONL file fallback |
| `start.sh` | Prints dashboard URL on startup |

## Running

The dashboard starts automatically with the state store:

```bash
./start.sh
# Dashboard: http://localhost:8090/
```

Port 8090 must be open in the firewall (`firewall-cmd --add-port=8090/tcp --permanent`).

## Design Decisions

- **Vanilla HTML/JS/CSS** — zero build step, easy to iterate. Structured so migration to React is straightforward later (data fetching separated from rendering).
- **Polling over SSE/WebSockets** — simpler, works with existing API, adequate refresh rate for a dashboard.
- **Served from FastAPI** — single port, no CORS issues, no extra process. CORS middleware added anyway for future flexibility.
- **Collapsible everything** — `<details>` elements let users minimize system prompts, tool I/O, and agent sections to control information density.
- **Fixed context, scrollable events** — the ticket detail view pins metadata (ID, hypothesis, description, custom fields) at the top so investigation context stays visible while the event stream scrolls independently below. Uses `body:has(.detail-view)` to disable page scroll in detail mode; only the event stream container scrolls.
- **Dark theme** — CSS variables make re-theming easy.
