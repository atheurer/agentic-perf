# agentic-perf TUI — Implementation Plan

**Status:** Ready for implementation
**Source concept:** `docs/tui-concept.md`
**Decisions locked by the project owner:**
- Language/stack: **Go + bubbletea** (charmbracelet: bubbletea, bubbles, lipgloss)
- Location: **`tui/` subdirectory** of the `agentic-perf` repo (own Go module)
- Scope: **server-side Python changes (SSE, interject) and Go client in this one plan**, as parallel workstreams

This document is written to be executed by Claude Code. Section 9 ("Orchestration
guide") tells the coordinating agent how to fan tasks out to subagents, what the
verification gates are, and which files must be serialized. Everything in Section 3
("Ground truth") was verified against the repo at plan-writing time — re-verify in
Phase 0 before building on it.

---

## 1. Goal

Ship a standalone, cross-platform terminal UI (`aptui`) that becomes the primary
interactive client for an agentic-perf deployment, replacing the current
curl/tail/CLI workflow with:

1. Conversation-style live view of agent activity (tool calls, transitions, progress)
2. Inline HITL: respond to `request_clarification` prompts like a chat, with bell/visual notification
3. Inline **command approval** (approve/deny `pending_approval` requests — exists in the repo today, missing from the concept doc, and is the highest-frequency HITL event)
4. Esc-to-interject guidance into a running agent
5. Slash commands for ticket lifecycle: `/submit`, `/tickets`, `/ticket`, `/follow`, `/abort`, `/retry`, `/stop`, `/logs`, `/approve`, `/deny`, `/usage`, `/connect`, `/config`
6. Real-time transport via a new SSE endpoint, with transparent polling fallback

## 2. Non-goals (v1)

- Split-pane simultaneous multi-ticket view (v1: one followed ticket + fast selector; split panes are a stretch goal, Phase 6)
- WebSocket transport (SSE only, per concept doc's high-latency-link priority)
- Multi-user auth (single bearer token, matching `state_store/auth.py` today)
- Replacing the web UI or the existing `cli.py` (both remain; `cli.py` stays for scripting)
- Mouse support beyond what bubbletea gives for free

---

## 3. Ground truth (verified against the repo — re-verify in Phase 0)

### 3.1 Existing REST API (all under `/api/v1`, bearer token, port 8090)

| Endpoint | Notes |
|---|---|
| `POST /tickets` | body `{summary, description, custom_fields}` → Ticket. New tickets start `new`; client must transition to `triage_pending` |
| `GET /tickets?status=` | list, optional status filter |
| `GET /tickets/{id}` | full ticket incl. `custom_fields`, `comments`, `previous_status` |
| `PATCH /tickets/{id}/fields` | body `{fields: {...}}` — merge into `custom_fields` |
| `POST /tickets/{id}/transition` | body `{status, comment?}`; 400 on invalid transition |
| `POST /tickets/{id}/comments` | body `{author, body}` |
| `GET /tickets/{id}/comments` | list |
| `GET /tickets/{id}/events?since=N&limit=200` | **seq-cursor polling** — this is the existing near-real-time mechanism the web UI uses; the polling fallback reuses it verbatim |
| `GET /tickets/{id}/transcript?agent=` | full event list + ticket summary |
| `GET /tickets/{id}/usage` / `GET /usage/summary` | token/cost accounting |
| `POST /tickets/{id}/stop` | body `{mode: graceful\|hard}`; 409 if terminal/paused |
| `POST /tickets/{id}/claim` (+ renew/release) | agent-side; TUI displays claim owner only |
| `GET /health` (health_router) | unauthenticated |

Source: `state_store/api/{tickets,transitions,comments,events,stop,health}.py`, `state_store/main.py`.

### 3.2 Ticket model & state machine

`state_store/models.py`: `TicketStatus` enum (linear pipeline `new → triage_pending →
awaiting_hardware → awaiting_provision → executing_benchmark → awaiting_review →
awaiting_teardown → retrospective_pending → closed` plus investigation-loop statuses
`gathering_context / planning_investigation / evaluating_convergence /
synthesizing_results`), `VALID_TRANSITIONS` map, `PAUSED_STATUSES =
{awaiting_customer_guidance}`, `TERMINAL_STATUSES = {closed}`. Ticket IDs look like
`PERF-XXXXXX`. Ticket has `previous_status` and `transition_seq`.

**Do not modify `VALID_TRANSITIONS`** — the TUI adapts to it (see task S3).

### 3.3 HITL protocol (agent-initiated) — reuse as-is

From `agents/base.py::_request_human_input` (~lines 947–1002) and `cli.py::cmd_reply`:

1. Agent posts comment `**Input needed:** <question>`, transitions ticket to `awaiting_customer_guidance`, then polls the ticket every 5 s (timeout 1800 s).
2. User replies by: `POST comments {author: "user", body: <answer>}`, then `POST transition {status: <previous_status>}`. Optional per-reply overrides via `PATCH fields`: `llm_override {provider, model}`, `max_iterations_override`, `remember_previous`.
3. Agent detects status left `awaiting_customer_guidance`, collects comments added since it asked (author not in `{"system", <agent_name>}`), joins them as the reply.
4. Abort-from-HITL = comment + transition to `awaiting_teardown`.

The TUI implements exactly this client-side sequence. No server change needed for HITL replies.

### 3.4 Command approval protocol — reuse as-is

From `cli.py::cmd_approve/cmd_deny` and `agents/infra/server.py`:
`custom_fields.pending_approval = {agent, host, binary, command, status:
"pending"|"approved_once"|"approved_ticket"|"denied"}`. Approve-for-ticket also
appends to `custom_fields.command_approvals[]`. All via `PATCH fields`. The agent
polls the field. TUI surfaces pending approvals as a prompt with keys
`[a]pprove once / [t]icket-wide / [d]eny`.

### 3.5 Event bus & the cross-process seq constraint

`providers/events.py::EventBus` — JSONL file per ticket (dir from `paths.LOG_DIR`),
monotonic per-ticket `seq` recovered from line count on restart. `EVENT_TYPES =
{agent_started, agent_finished, agent_error, llm_request, llm_response, tool_called,
tool_result, tool_skipped, transition, comment, tool_progress, llm_usage,
agent_stopped}`.

**Critical constraint** (comment in `state_store/api/transitions.py`): the state
store and orchestrator are **separate processes with independent EventBus seq
counters writing the same JSONL files**. The state store must **never emit events**
— doing so causes seq collisions that drop agent events. Therefore:
- The SSE endpoint (S1) is **read-only** over the bus.
- The interject endpoint (S2) does **not** emit; the agent emits on pickup.

### 3.6 Auth & config conventions

`state_store/auth.py`: token from env `AGENTIC_PERF_API_TOKEN` or
`~/.agentic-perf/secrets/api-token` (0600). Health endpoint is unauthenticated. The
TUI reads token from (in order): `--token` flag → env → client.toml → the local
secrets file (same-host convenience).

### 3.7 Repo conventions

- `AGENTS.md` is the contributor guide for AI agents — read it first; note the
  warning about backgrounding services (`./scripts/start-bg.sh`, never bare `&`).
- Python: 3.12/3.13, ruff via `./scripts/lint.sh`, pytest via `./scripts/test.sh`,
  tests in `tests/test_*.py` with fixtures in `tests/conftest.py`.
- CI: `.github/workflows/ci.yml` — lint job + test matrix. TUI adds Go jobs (P1).
- Existing reference implementations to mine: `cli.py` (`cmd_watch`, `cmd_reply`,
  `cmd_approve`, `_format_event`, `_render_transcript`) — the TUI's rendering and
  reply logic should start from these semantics.

---

## 4. Architecture

```
┌──────────────────────────────┐          ┌─────────────────────────────┐
│ tui/ (Go, module: aptui)     │  HTTPS   │ state_store (FastAPI :8090) │
│                              │ ───────► │  existing REST  /api/v1/... │
│  cmd/aptui        binary     │          │  NEW  GET /events/stream    │  S1 (SSE)
│  internal/config  TOML+env   │          │  NEW  POST .../interject    │  S2
│  internal/api     REST client│          │  NEW  GET .../transitions   │  S3
│  internal/stream  SSE+poll   │          └──────────────┬──────────────┘
│  internal/events  normalize  │                         │ JSONL (shared dir)
│  internal/ui      bubbletea  │          ┌──────────────┴──────────────┐
│  internal/mockserver (tests) │          │ orchestrator + agents        │
└──────────────────────────────┘          │  agents/base.py: NEW         │  S2b
                                          │  per-iteration interject     │
                                          │  pickup                      │
                                          └─────────────────────────────┘
```

### 4.1 SSE endpoint spec (S1)

`GET /api/v1/events/stream` — auth: standard Bearer header (the Go client sets
headers directly; browser EventSource support is a non-goal).

Query params:
- `ticket_id` — optional, comma-separated. Absent ⇒ "all active": server polls
  `store.list_tickets()` every 2 s, follows every non-`closed` ticket, and picks up
  newly created tickets automatically.
- `event_type` — optional, comma-separated filter (values from `EVENT_TYPES`).
- `since` — optional int; only honored for a single `ticket_id` (seq is per-ticket).

Wire format (`text/event-stream`):
```
id: PERF-ABC123:47
event: tool_called
data: {"seq":47,"timestamp":"...","ticket_id":"PERF-ABC123","agent":"review-agent","event_type":"tool_called","data":{...}}
```
- `id` is `<ticket_id>:<seq>` so `Last-Event-ID` reconnection resumes per-ticket
  (multi-ticket streams resume only the ticket named in the header; others restart
  from the client's own cursors — the Go client tracks per-ticket cursors and passes
  them by reconnecting per the fallback path if needed; see C3).
- Heartbeat comment line `: keepalive` every 15 s (VPN/proxy survival).
- Implementation: `async def` generator in new `state_store/api/stream.py`; per-ticket
  cursor dict; every 1.0 s call `event_bus.get_events(t, since=cursor[t], limit=200)`
  via `asyncio.to_thread` (bus reads are blocking file I/O); check
  `await request.is_disconnected()` each cycle; register router in
  `state_store/api/router.py`.
- **Read-only**: never `emit()` from this process (§3.5).

Degradation contract: if the endpoint is missing (older server), it returns 404 and
the client falls back to seq-cursor polling of `GET /tickets/{id}/events` — same
JSON event objects, so everything downstream of `internal/stream` is transport-agnostic.

### 4.2 Interject spec (S2 server + S2b agent)

`POST /api/v1/tickets/{id}/interject`, body `{"message": "<text>"}`.

Server behavior (`state_store/api/interject.py`):
- 404 unknown ticket; 409 if status is terminal.
- If status is `awaiting_customer_guidance`: reject with 409 and
  `detail: "ticket is waiting for a reply — use the HITL reply flow"` (the TUI routes
  that case through §3.3 instead).
- Otherwise: `store.add_comment(author="user", body=message)` **and**
  `store.update_fields({"pending_interject": {"message": ..., "created_at": iso}})`.
  No transition, no event emission (§3.5).

Agent pickup (`agents/base.py`): between LLM iterations — at the same point the
agent already re-fetches the ticket for stop/status checks (verify exact hook in
Phase 0, task V1; `tests/test_stop.py` demonstrates the pattern) — check
`custom_fields.pending_interject`. If present: inject a user-role message
`"Operator interjection: <message>"` into the conversation, clear the field via
PATCH, and `emit(ticket_id, agent, "user_interjection", {...})`. Add
`"user_interjection"` to `EVENT_TYPES` in `providers/events.py`.

Rationale vs. the concept doc's transition-based sketch: transitioning a ticket to
`awaiting_customer_guidance` while an agent is mid-LLM-loop conflicts with the
dispatcher's status→agent mapping and the agent's own HITL polling; the field-based
queue delivers the doc's stated semantics ("queued and delivered when the tool call
completes") without touching the state machine. If Phase 0 recon contradicts this
(V1), the coordinator stops and re-plans task S2b before any Phase 1 interject work.

### 4.3 Valid-transitions helper (S3, small)

`GET /api/v1/tickets/{id}/transitions` → `{"current": "...", "valid": [...]}`
computed from `VALID_TRANSITIONS`; for `awaiting_customer_guidance` return
`[previous_status]` if set. Powers `/retry` and `/abort` UX so the Go side never
hardcodes the state machine (no drift). If the endpoint 404s (old server), the TUI
disables `/retry` stage-picking and offers only the reply/abort flows.

### 4.4 Go module layout

```
tui/
├── go.mod                     # module github.com/atheurer/agentic-perf/tui ; go 1.23
├── Makefile                   # build, check (fmt+vet+staticcheck+test), cross, e2e
├── README.md
├── cmd/aptui/main.go          # flag parsing, config load, tea.NewProgram
└── internal/
    ├── config/                # TOML at ~/.config/agentic-perf/client.toml + env + flags
    ├── api/                   # typed REST client (types.go mirrors models.py), retry/backoff
    ├── stream/                # Source interface; sse.go, poll.go, reconnect w/ jittered backoff
    ├── events/                # Event normalization → renderable Line{ticket, agent, kind, text, ts}
    ├── ui/
    │   ├── app.go             # root tea.Model: mode state machine (Normal|HITL|Interject|Approval|Selector)
    │   ├── commands.go        # slash-command registry + completion
    │   ├── render.go          # event_type → styled line (lipgloss; NO_COLOR/dumb-term fallback)
    │   ├── statusbar.go       # connection state, followed ticket, ⏳/🔔 badges, transport (sse|poll)
    │   ├── input.go           # bubbles/textarea; Enter submits, Alt+Enter newline (Shift+Enter is not reliably distinguishable in terminals — do not promise it)
    │   └── notify.go          # terminal BEL + statusbar flash on input-needed
    └── mockserver/            # httptest server: REST subset + SSE; scriptable event scenarios (shared by unit, teatest, demos)
```

Dependencies (keep minimal, per concept doc): bubbletea, bubbles, lipgloss,
`github.com/BurntSushi/toml`. SSE parsing is ~100 lines — implement in
`internal/stream/sse.go`, no third-party SSE lib. Static binary:
`CGO_ENABLED=0`; cross targets linux/amd64, linux/arm64, darwin/amd64, darwin/arm64.

### 4.5 TUI interaction model (bubbletea)

Root model = Elm state machine. Msgs: `stream.EventMsg`, `tickMsg`, `tea.KeyMsg`,
API result msgs. Modes:

- **Normal**: viewport scrollback (vim j/k + PgUp/PgDn, `G` to tail) + one-line input.
  Typing `/` opens command completion. Free text with no active prompt → hint.
- **HITL**: entered when a followed ticket transitions to
  `awaiting_customer_guidance` or a comment starting `**Input needed:**` arrives.
  Bell + 🔔 badge. Multi-line textarea; Enter submits (Alt+Enter newline); executes
  §3.3 reply sequence; `done`/`submit` are passed through verbatim (agents treat
  them as loop-enders).
- **Approval**: entered when `pending_approval.status == "pending"` is observed
  (poll ticket fields on `tool_called`/`transition` events + a 5 s ticker for the
  followed ticket). Renders agent/host/binary/command; keys a/t/d per §3.4.
- **Interject**: Esc from Normal while followed ticket is active → textarea →
  `POST /interject`; on 409-guidance, switch to HITL mode instead.
- **Selector**: `/follow` with no arg and >1 active ticket, or `Tab` — pick from
  a list (id, status, summary, age, badges). Default follow: most recently updated
  active ticket.

Rendering: port `cli.py::_format_event` semantics — `[agent]` colored tag,
`→ tool(args…)` for `tool_called` (truncate args at ~120 cols), `✓/✗` for results,
`⏳ sample x/y, elapsed` from `tool_progress`, dim for `llm_request/llm_response`
(hidden by default; `/verbose` toggles), transitions as `── status → status ──`
dividers. Graceful degradation: honor `NO_COLOR`, `TERM=dumb` ⇒ ASCII-only glyphs.

---
## 5. Workstreams & dependency graph

```
Phase 0 (serial, coordinator): V1 V2 V3 recon ── G0 gate
        │
Phase 1 (FAN-OUT, 4 parallel subagents):
        ├─ WS-A  S1 SSE endpoint              (Python; files: state_store/api/stream.py, router.py*, tests/test_event_stream.py)
        ├─ WS-B  S2+S2b+S3 interject & trans. (Python; files: state_store/api/interject.py, transitions_info.py, router.py*, agents/base.py, providers/events.py, tests/test_interject.py)
        ├─ WS-C  C1+C2 config + REST client   (Go;     files: tui/internal/{config,api}/...)
        └─ WS-D  C3+C4 mockserver + stream    (Go;     files: tui/internal/{mockserver,stream}/...)
                 * router.py touched by both WS-A and WS-B → coordinator merges (one-line includes); everything else is disjoint.
        ── G1 gate
Phase 2 (serial or 2 agents): T1 app shell, T2 rendering pipeline        ── G2 gate
Phase 3 (FAN-OUT, 3 parallel subagents on disjoint files):
        ├─ F1 ticket lifecycle commands (/submit /tickets /ticket /abort /retry /stop /logs /usage)
        ├─ F2 HITL + Approval modes
        └─ F3 follow engine + interject + selector + notifications
        ── G3 gate
Phase 4: polish (P0 first-run, /connect, /config, degradation, reconnect UX)  ── G4 gate
Phase 5: P1 CI, P2 packaging/docs, P3 E2E smoke                               ── G5 gate
Phase 6 (stretch, optional): split panes, transcript export, /diff runs
```

---

## 6. Phases & tasks

Every task below states: **Files** (ownership boundary for the subagent — do not
touch files outside it without coordinator approval), **Steps**, **Acceptance
criteria (AC)**. A task is done only when its AC pass locally.

### Phase 0 — Recon & scaffolding (coordinator, serial, ~small)

**V1 — Verify interject hook point.** Read `agents/base.py` in full (esp. the
iteration loop, stop handling around the lines referenced by
`tests/test_stop.py`) and `orchestrator/dispatcher.py`. Confirm: (a) the agent
re-fetches the ticket between LLM iterations; (b) writing
`custom_fields.pending_interject` is invisible to the dispatcher; (c) the exact
method where pickup belongs. Record findings in `tui/docs/recon-notes.md`. If (a)
is false, propose a revised S2b design and stop for review.

**V2 — Verify event-bus read path.** Confirm the state store process's
`EventBus.get_events` reads/merges from the JSONL files written by the
orchestrator process (look at `_read_from_file` in `providers/events.py`), and
confirm `paths.LOG_DIR` is shared between processes. This is the load-bearing
assumption for S1. Note the polling latency floor it implies.

**V3 — Scaffold `tui/`.** `go mod init github.com/atheurer/agentic-perf/tui`;
Makefile with `build`, `check` (gofmt -l → fail, go vet, staticcheck, go test
./...), `cross`; empty package skeletons per §4.4 that compile; `tui/README.md`
stub; add `tui/aptui` + `dist/` to `.gitignore`.

**Gate G0:** `cd tui && make check` green (trivially); recon notes written; no
contradictions found (or plan amended).

### Phase 1 — Transport foundations (fan-out ×4)

**S1 — SSE stream endpoint** (WS-A, Python)
Files: `state_store/api/stream.py` (new), `state_store/api/router.py` (include line
only — coordinator applies), `tests/test_event_stream.py` (new).
Steps: implement §4.1 exactly; reuse patterns from `state_store/api/events.py`.
AC:
- Streams existing + newly appended JSONL events for one ticket within ≤2 s of write.
- Multi-ticket ("all active") mode picks up a ticket created after connect.
- `Last-Event-ID: PERF-X:N` resumes at seq N+1; `event_type` filter works.
- Heartbeats emitted; client disconnect terminates the generator (no task leak).
- Zero calls to `EventBus.emit` (assert via grep in test or code review) — §3.5.
- `./scripts/lint.sh && ./scripts/test.sh` green.

**S2/S2b/S3 — Interject + agent pickup + transitions helper** (WS-B, Python)
Files: `state_store/api/interject.py`, `state_store/api/transitions_info.py` (new),
`state_store/api/router.py` (include lines — coordinator), `agents/base.py`
(pickup only), `providers/events.py` (EVENT_TYPES += `user_interjection`),
`tests/test_interject.py`, `tests/test_transitions_info.py`.
Steps: implement §4.2 and §4.3; mirror the test style of `tests/test_stop.py`
(agent-side) and `tests/test_state_store.py` (API-side).
AC:
- POST interject on a running ticket stores comment + field; 409 on terminal and on
  `awaiting_customer_guidance` (with the distinct detail string).
- Agent test: iteration loop with `pending_interject` set → message injected into
  conversation, field cleared, `user_interjection` event emitted once.
- `GET /transitions` returns `VALID_TRANSITIONS` values; `awaiting_customer_guidance`
  returns `[previous_status]`.
- Lint + full pytest green (including all pre-existing tests — `agents/base.py` is
  shared code; regressions here are the top risk of this task).

**C1/C2 — Go config + REST client** (WS-C, Go)
Files: `tui/internal/config/*`, `tui/internal/api/*`.
Steps: config precedence per §3.6/§4.4 with `client.toml`
(`server_url`, `token`, `default_hosts`, `default_priority`, `verbose`); typed
client covering every §3.1 endpoint plus S1–S3; context-aware; single retry with
backoff on 5xx/connection errors; typed errors distinguishing 401/404/409.
AC: unit tests against a stub `httptest.Server` (not the full mockserver) cover
every method, auth header injection, error mapping, and config precedence
(env > file; flag > env). `make check` green.

**C3/C4 — Mock server + stream sources** (WS-D, Go)
Files: `tui/internal/mockserver/*`, `tui/internal/stream/*`, `tui/internal/events/*`.
Steps: mockserver implements REST subset + SSE with a scriptable scenario API
(`s.Emit(ticket, event)`, `s.SetTicket(...)`, `s.KillConnections()`,
`s.DisableSSE()`); `stream.Source` interface with `Events() <-chan events.Line`-style
channel of normalized events + `Status() <-chan ConnState`; SSE source with
jittered exponential backoff (0.5 s → 30 s cap) and Last-Event-ID resume; polling
source (2 s, per-ticket seq cursors) auto-selected on SSE 404.
AC:
- Reconnect test: `KillConnections()` mid-stream → no event lost, no duplicate
  delivered (dedupe on ticket:seq).
- Fallback test: `DisableSSE()` → identical event sequence via polling; status
  channel reports `sse`→`poll`.
- Normalization: table-driven test mapping every `EVENT_TYPES` member (incl.
  `user_interjection`) to a `Line`. `make check` green.

**Gate G1:** all four workstreams' AC green; coordinator merges the `router.py`
include lines; **integration spot-check:** run the real state store
(`STORE_PORT=8091 python3 -m state_store.main` with temp `AGENTIC_PERF` dirs, via
`scripts/start-bg.sh` conventions — never bare `&`, per AGENTS.md), append events
to a JSONL file by hand, and confirm a 20-line Go program using C2+C4 prints them
live. Commit the program as `tui/cmd/aptui-probe/main.go` (kept as a debug tool).

### Phase 2 — TUI shell (1–2 agents)

**T1 — App shell** — Files: `tui/internal/ui/{app,input,statusbar}.go`,
`tui/cmd/aptui/main.go`. Root model, mode enum, viewport+textarea+statusbar layout,
resize handling, quit confirm, `tea.WithAltScreen`, tick loop.
**T2 — Rendering pipeline** — Files: `tui/internal/ui/render.go` (+`events`
adjustments). Implement §4.5 rendering incl. verbose toggle and degradation modes.
AC (joint): `aptui --server ... --token ...` against mockserver scenario shows a
scripted triage→benchmark sequence rendering correctly; golden-file tests via
`teatest` (`github.com/charmbracelet/x/exp/teatest`) for: startup, event render,
resize, NO_COLOR. Scrollback holds 10k lines without visible lag (benchmark test
with `testing.B`, budget <16 ms per frame render). `make check` green.

**Gate G2:** teatest goldens green; manual smoke against mockserver documented in
`tui/docs/recon-notes.md`.

### Phase 3 — Features (fan-out ×3, disjoint files)

**F1 — Lifecycle commands** — Files: `tui/internal/ui/commands.go`,
`tui/internal/ui/cmd_lifecycle.go` (new).
`/submit` guided flow (summary → description multi-line → hosts w/ config default →
priority → directives) + one-liner form (`/submit <text>` ⇒ summary=text,
description=text); auto `new→triage_pending` transition; `/tickets [active]`,
`/ticket ID`, `/abort ID` (comment + transition per `cmd_reply --abort` semantics),
`/retry ID` (fetch S3 valid transitions, pick stage), `/stop ID [--hard]`,
`/logs ID [agent]` (transcript endpoint, rendered through T2), `/usage [ID]`.
AC: teatest per command against mockserver; `/retry` hides stage-picker gracefully
when S3 endpoint 404s.

**F2 — HITL + Approval modes** — Files: `tui/internal/ui/mode_hitl.go`,
`mode_approval.go` (new).
Implement §4.5 HITL and Approval modes, exact wire sequences from §3.3/§3.4,
including optional reply flags (`/reply` inline modifiers `--model --provider
--max-iterations --remember` for parity with `cli.py`).
AC: teatest scenario — agent asks, bell fires (assert BEL byte in output), user
answers multi-line, mockserver receives comment+transition in order; approval
scenario asserts exact `pending_approval` field mutations for a/t/d.

**F3 — Follow engine, interject, selector, notifications** — Files:
`tui/internal/ui/{follow,mode_interject,selector,notify}.go` (new).
Follow-most-recent default; `/follow [ID]`; Tab selector; Esc→interject
(§4.5, incl. 409→HITL rerouting); statusbar badges; unfollowed-ticket
input-needed still notifies (multi-ticket stream watches all active).
AC: teatest — two active tickets, HITL arrives on the unfollowed one → bell +
badge; selecting it enters HITL mode; interject on running ticket sends POST.

**Gate G3:** all Phase 3 AC green; full manual walkthrough against mockserver
scenario `scenario_full_pipeline` (write it in mockserver as part of whichever
subagent finishes first — coordinator assigns).

### Phase 4 — Polish (serial)

**P0** — First-run wizard (prompt URL/token → write client.toml 0600), `/connect`,
`/config [key value]`, connection-lost banner + auto-resume, `--plain` flag,
Ctrl+C double-tap quit, `/help`. AC: teatest first-run golden; kill mockserver
mid-session → banner → restart → resumes with no dupes.

**Gate G4:** `make check` green; README quickstart written and followed verbatim
by the coordinator in a fresh shell.

### Phase 5 — CI, packaging, E2E

**P1 — CI** — Files: `.github/workflows/ci.yml` (append jobs; do not modify
existing jobs). Jobs: `tui-check` (setup-go 1.23, cache, `make check`);
`tui-cross` (matrix of 4 GOOS/GOARCH targets, upload artifacts named
`aptui-<os>-<arch>`).
**P2 — Packaging/docs** — `make cross` producing `dist/`; `tui/README.md` full
docs (install, config file format, keybindings table, command reference,
degradation notes); update `docs/tui-concept.md` status header pointing here;
add TUI row to `AGENTS.md` key-paths table.
**P3 — E2E smoke** — `tui/e2e/e2e_test.go` (build tag `e2e`): start the real
state store as a subprocess (temp HOME/dirs, random port), drive `aptui` via
teatest: submit → hand-append agent events to the JSONL → HITL question →
reply → assert ticket transitioned. CI job `tui-e2e` (installs Python deps +
Go; `make e2e`), allowed to be `continue-on-error: false` but isolated from
existing jobs.

**Gate G5 / Definition of Done:**
1. All CI jobs green on a PR from a clean branch.
2. `aptui` static binaries build for all 4 targets; linux/amd64 binary ≤ 15 MB.
3. E2E smoke passes.
4. Concept-doc feature parity checklist (Section 10) fully checked.
5. No modifications to `VALID_TRANSITIONS`; no `EventBus.emit` from state-store
   process; all pre-existing Python tests still green.

---

## 7. Testing strategy summary

| Layer | Tool | Where |
|---|---|---|
| Python endpoints (S1–S3) | pytest, httpx streaming client, temp dirs | `tests/test_event_stream.py`, `tests/test_interject.py`, `tests/test_transitions_info.py` |
| Agent pickup (S2b) | pytest, pattern of `tests/test_stop.py` | `tests/test_interject.py` |
| Go client/stream | `go test` vs `internal/mockserver` | per-package `*_test.go` |
| TUI behavior | `teatest` golden files + scripted mockserver scenarios | `tui/internal/ui/*_test.go` |
| Render perf | `testing.B` (<16 ms/frame at 10k-line scrollback) | `render_bench_test.go` |
| E2E | real state store subprocess + teatest, tag `e2e` | `tui/e2e/` |

Mockserver scenarios to script (reused across tests + demos):
`scenario_full_pipeline`, `scenario_hitl`, `scenario_approval`,
`scenario_two_tickets`, `scenario_reconnect`, `scenario_no_sse`.

---

## 8. Guardrails (hard rules for every subagent)

1. **Never** call `EventBus.emit` from state-store code paths (§3.5 — documented
   seq-collision bug class).
2. **Never** modify `state_store/models.py::VALID_TRANSITIONS` or the
   `TicketStatus` enum.
3. Stay inside your task's **Files** list; shared files (`state_store/api/router.py`,
   `.github/workflows/ci.yml`, `AGENTS.md`, `.gitignore`) are coordinator-only.
4. Python changes must keep `./scripts/lint.sh` and the **entire** existing pytest
   suite green — especially for `agents/base.py` (task S2b).
5. Backgrounding services in tooling/tests: follow AGENTS.md — use
   `scripts/start-bg.sh` semantics (nohup + explicit log redirection), never bare
   `&` in compound commands.
6. Go code: no new dependencies beyond §4.4 without coordinator sign-off; SSE
   parser is hand-rolled; `CGO_ENABLED=0` must remain viable.
7. Don't promise Shift+Enter (terminal reality); Alt+Enter is the documented
   multi-line binding.
8. Tokens are secrets: never log them, never write client.toml with mode
   other than 0600, redact in `/config` display.

---

## 9. Claude Code orchestration guide

**Read first (in order):** this plan → `AGENTS.md` → `docs/tui-concept.md` →
`state_store/models.py` → `state_store/api/` → `agents/base.py` (iteration loop +
`_request_human_input`) → `cli.py` (`cmd_watch`, `cmd_reply`, `cmd_approve`,
`_format_event`).

**Loop pattern (every task):** implement → run the task's AC commands → on failure,
fix and rerun (do not widen scope to "fix" unrelated red tests — report them) →
when green, commit with message `tui(<task-id>): <summary>` → report AC evidence
(command output) to coordinator.

**Fan-out map:** Phase 1 = 4 subagents (WS-A…WS-D above; file ownership is disjoint
by construction except `router.py`, which the coordinator edits after both Python
workstreams land). Phase 3 = 3 subagents (F1/F2/F3; each owns distinct new files
under `internal/ui/`; `app.go` mode-registration hooks are added by the coordinator
in one small commit after fan-in, or expose a `RegisterMode` seam in T1 to avoid
even that). Phases 0, 2 (optionally 2 agents), 4, 5 are serial or lightly parallel.

**Gates are blocking.** Never start phase N+1 with gate N red. Gate commands:
- Python: `./scripts/lint.sh && ./scripts/test.sh`
- Go: `cd tui && make check`
- G1 additionally: the `aptui-probe` live integration spot-check.
- G5: full CI on a PR.

**Context for subagents:** give each subagent only (a) its task block from Section 6,
(b) Sections 3, 4 (relevant subsection), and 8 of this plan, (c) the specific repo
files in its Files list. Do not hand the whole plan to every subagent.

**When reality disagrees with this plan** (most likely: V1 findings about
`agents/base.py`, or `EventBus` read semantics in V2): stop the affected task,
write the discrepancy + proposed amendment into `tui/docs/recon-notes.md`, get
coordinator/human sign-off, then proceed. Do not silently improvise around the
state machine, the event bus, or auth.

**Branch/PR strategy:** one branch per phase (`tui/phase-1-transport`, …), one PR
per phase against `main`, tasks as separate commits. If the repo owner prefers a
single long-lived `tui` branch, phases become stacked commits — ask once at start.

---

## 10. Feature-parity checklist (from docs/tui-concept.md)

- [ ] `/submit` guided + one-liner, auto `new→triage_pending`
- [ ] `/follow` live view: tool calls, results, progress (`sample x/y, elapsed`), transitions
- [ ] Default-follow most recent active ticket; selector for multiple
- [ ] HITL prompt inline: bell + visual indicator, agent+ticket shown, multi-line reply, `done`/`submit` passthrough
- [ ] Command approval prompt (repo feature beyond concept doc)
- [ ] Esc-to-interject, queued delivery on next agent iteration
- [ ] `/tickets`, `/tickets active`, `/ticket ID`, `/abort`, `/retry` (stage-aware via S3), `/stop`, `/logs ID [agent]`, `/usage`
- [ ] `/connect`, `/config`, first-run wizard, `~/.config/agentic-perf/client.toml`
- [ ] SSE endpoint w/ ticket & type filtering, Last-Event-ID resume, heartbeats
- [ ] Polling fallback when server lacks SSE
- [ ] Keyboard-driven, vim scrollback, no mouse required
- [ ] NO_COLOR / dumb-terminal degradation; works in tmux/screen
- [ ] Single static binary, 4 cross targets, small enough to scp
- [ ] Interject endpoint + agent pickup + `user_interjection` event

