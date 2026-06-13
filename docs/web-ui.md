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

Two-column layout: main content area on the left, sticky sidebar on the right.

**Main content:**
- Breadcrumb navigation back to dashboard
- Ticket header with ID, summary, and status badge
- Collapsible description and custom fields sections
- Transaction log — full event stream rendered as it arrives

**Sidebar (sticky, stays visible while scrolling):**
- Current ticket status (updates live on transitions)
- Controls: Live/Pause polling, Auto-scroll toggle
- Navigation: Jump to top/bottom, Collapse/Expand all agent sections
- Agent navigator: lists each agent phase with status dot (pulsing blue = active, green = done, red = error); click to scroll to that agent's section
- Event counter

**Transaction log event types:**
- `agent_started` — collapsible section header with agent name; contains collapsible system prompt and initial messages
- `llm_response` — shows response text and tool calls; long content auto-collapsed
- `tool_called` / `tool_result` — shows tool name, input, output with collapsible long content
- `transition` — highlighted banner showing the new status
- `comment` — comment body with author
- `agent_finished` / `agent_error` — completion or error markers

**Live polling:** fetches `/api/v1/tickets/{id}/events?since={lastSeq}` every 2 seconds. New events are appended to the DOM. Auto-scroll keeps the view at the bottom unless the user disables it.

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
- **Dark theme** — CSS variables make re-theming easy.
