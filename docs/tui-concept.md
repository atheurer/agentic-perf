# agentic-perf TUI: Interactive Terminal Client

## Problem

Today, interacting with agentic-perf requires stitching together multiple
tools and APIs manually:

- **Submitting a ticket** means crafting a `curl -X POST` with JSON, knowing
  the state store URL, finding the API token, and remembering to transition
  the ticket from `new` to `triage_pending`.
- **Monitoring progress** means tailing `~/.agentic-perf/logs/orchestrator.log`
  or polling the ticket API. There's no consolidated view of what each agent
  is doing, what tools it's calling, or how far along the pipeline a ticket is.
- **HITL (human-in-the-loop) interaction** is the biggest gap. When an agent
  calls `request_clarification`, it transitions the ticket to
  `awaiting_customer_guidance` and polls a field on the ticket every few
  seconds waiting for a response. The user must notice this happened (by
  watching logs or polling), then PATCH the ticket's `customer_response`
  field via curl. There's no notification, no prompt, no conversation flow.
- **Reviewing agent reasoning** requires reading JSONL event logs in
  `~/.agentic-perf/events/<ticket-id>/` or the transcript logs. There's no
  live view of what the agent is thinking or what tool results it received.
- **Managing tickets** (listing, checking status, aborting, resubmitting)
  is all raw API calls.

This friction compounds: a single E2E run (triage → resource → provision →
benchmark → review) takes 1-3 hours, and the user needs to be available for
HITL prompts at unpredictable times. Missing a prompt means the agent sits
idle until timeout (default 30 minutes, configurable to infinite). The user
has no way to know when their input is needed without actively watching.

## Vision

A terminal UI application — similar in spirit to Claude Code's interactive
REPL — that serves as the primary interface to an agentic-perf deployment.
The user launches it, points it at a running agentic-perf server, and gets
a unified interface for submitting work, watching agents operate, and
intervening when needed.

### Core Interaction Model

The TUI presents a scrolling conversation-style view. The user sees agent
activity streaming in real-time (tool calls, progress updates, state
transitions) interleaved with HITL prompts that they can respond to inline.
The feel should be collaborative — like pair-programming with the agents —
not like monitoring a dashboard.

Key interaction patterns:
- **Slash commands** for structured actions (`/submit`, `/tickets`, `/abort`,
  `/logs`, `/follow`)
- **Free-text input** for responding to HITL prompts — the agent asks a
  question, the user types an answer, just like a chat
- **Escape to interject** — while agents are working (not in HITL), the user
  can press Esc to inject guidance into the current agent's context, similar
  to how Claude Code allows interruption. This would transition to
  `awaiting_customer_guidance` and deliver the user's message.
- **Notifications** — visual and/or audible alert when an agent needs input,
  so the user can work on other things and come back

### Architecture

```
┌─────────────┐         ┌──────────────────┐
│   TUI       │ ──────► │  State Store API  │  (tickets, transitions, HITL)
│  (terminal) │ ──────► │  Event Stream     │  (SSE/WebSocket for real-time)
│             │         │  :8090            │
└─────────────┘         └──────────────────┘
      │
      │  The TUI is a standalone binary.
      │  It does NOT need to run on the same host as the server.
      │  All communication is over HTTP(S) to the state store.
```

**Standalone binary**: The TUI should be distributable as a single binary
(Rust or Go are natural choices for TUI + cross-compilation). The user
installs it on their workstation, laptop, or any machine with network access
to the agentic-perf server. It authenticates with the existing bearer token.

**Real-time event stream**: The state store currently has no push mechanism.
The TUI will need either:
- A new SSE (Server-Sent Events) endpoint on the state store that streams
  ticket events as they happen (state transitions, agent tool calls, HITL
  prompts, progress updates)
- Or WebSocket support for bidirectional communication

SSE is simpler and sufficient — the TUI only needs to push data through
existing REST endpoints (POST responses, PATCH fields). The event bus
(`providers/events.py`) already writes structured JSONL events per ticket;
the SSE endpoint would tail these and forward to connected clients.

### Features

#### 1. Ticket Submission (`/submit`)

Interactive guided flow:
```
> /submit
Summary: 400G single-stream TCP baseline on ConnectX-7
Description: (opens $EDITOR or multi-line input)
Hosts: (auto-discovers from config or prompts)
Priority: high
Directives: disable_hitl_timeout, skip_user_approval

Ticket PERF-ABC123 created and submitted for triage.
```

Also accept one-liner for quick submissions:
```
> /submit Run uperf tcp stream on cloud18 hosts, 6 samples x 60s
```

The TUI handles the create → triage_pending transition automatically.

#### 2. Live Agent Progress (`/follow`)

Follow a ticket's progress in real-time:
```
> /follow PERF-ABC123

[triage-agent] Analyzing request...
[triage-agent] → parse_requirements({summary: "400G single-stream..."})
[triage-agent] Identified: uperf, tcp, stream, 6 samples, 60s
[triage-agent] → submit_triage_result({harness: "crucible", ...})
[triage-agent] ✓ Triage complete

[resource-agent] Starting resource allocation...
[resource-agent] → validate_host("10.1.37.12", ...)
[resource-agent]   Host validated: 768 CPUs, 512 GB RAM, 2 NUMA nodes
[resource-agent]   NICs: eno16695np0 (400G, NUMA 1), ens1f0 (100G, NUMA 0)
[resource-agent] → submit_resource_result(...)
[resource-agent] ✓ Resources allocated

[benchmark-agent] Preparing benchmark...
[benchmark-agent] → get_ethtool_info("10.1.37.12", "eno16695np0")
[benchmark-agent] → execute_benchmark(controller, run_file, "crucible")
[benchmark-agent] ⏳ Running... (sample 2/6, elapsed 3m42s)
```

The most recent ticket is followed by default if only one is active.
Multiple tickets can be followed in split panes or with a ticket selector.

#### 3. HITL Interaction

When an agent needs input, the TUI surfaces it as a prompt:
```
[review-agent] 🔔 Requesting your input:

  Initial analysis of run 7ac75ac6:
  - Mean throughput: 29.8 Gbps (single-stream TCP over 400G)
  - Server is the bottleneck: CPU 511 at 97% sys, CPU 341 at 72% soft
  - Both CPUs are on NUMA 1, same node as the ConnectX-7 NIC
  - Client CPU usage is negligible (<5% across all cores)

  I'd like to investigate per-CPU interrupt distribution on the server
  to understand if irqbalance is spreading NIC IRQs optimally.

  What would you like me to investigate?

> Check the GRO counters on eno16695np0 and also look at per-sample
  throughput variation — are all 6 samples consistent?

[review-agent] Investigating...
[review-agent] → get_ethtool_info("10.1.37.14", "eno16695np0", mode="stats")
```

The TUI should:
- Play a terminal bell / show a visual indicator when input is needed
- Support multi-line responses (Enter submits, Shift+Enter or a continuation
  character for multi-line)
- Show which agent is asking and what ticket it's for
- Allow the user to respond with "done" / "submit" to end the HITL loop

#### 4. Escape to Interject

While an agent is actively working (not in HITL), pressing Esc pauses the
view and opens an input prompt:
```
[benchmark-agent] ⏳ Running... (sample 4/6, elapsed 8m12s)

  [Esc pressed]
> Skip the remaining samples and move to review — I saw enough in the
  first 3 samples to know the pattern.

[system] Guidance delivered to benchmark-agent on PERF-ABC123.
```

This would transition the ticket to `awaiting_customer_guidance`, deliver
the message, and let the agent read it when it next checks. For agents
mid-tool-call (like `execute_benchmark` which runs for minutes), the
guidance would be queued and delivered when the tool call completes.

#### 5. Ticket Management

```
> /tickets                    # list all tickets with status
> /tickets active             # only in-progress tickets
> /ticket PERF-ABC123         # detailed view of one ticket
> /abort PERF-ABC123          # cancel a running ticket
> /retry PERF-ABC123          # resubmit from a specific stage
> /logs PERF-ABC123           # view event log for a ticket
> /logs PERF-ABC123 review    # view only review agent events
```

#### 6. Configuration and Connection

```
> /connect https://perf-server.example.com:8090
> /config                     # show current connection, token, defaults
> /config default_hosts ...   # set defaults for ticket submission
```

First-run experience: the TUI prompts for server URL and API token,
stores them in `~/.config/agentic-perf/client.toml` or similar.

### Server-Side Changes Needed

The TUI is mostly a client, but a few server-side additions are needed:

1. **SSE event stream endpoint** — `GET /api/v1/events/stream?ticket_id=X`
   that tails the event bus and pushes events as SSE. Should support
   filtering by ticket ID and event type. The event bus already writes
   structured JSONL; this endpoint reads and forwards it.

2. **HITL notification field** — the ticket model already has
   `awaiting_customer_guidance` status and the `customer_response` field
   pattern. The TUI polls or receives SSE for transitions to this status.

3. **Interject endpoint** — `POST /api/v1/tickets/{id}/interject` that
   transitions to `awaiting_customer_guidance` and stores the user's
   message. The current HITL mechanism works by the agent polling for a
   response; interjection would use the same mechanism but be user-initiated
   rather than agent-initiated.

4. **Agent progress events** — the event bus captures tool calls and state
   transitions, but richer progress data (benchmark progress percentage,
   current sample number, elapsed time) would improve the TUI experience.
   Some of this already exists via `tool_progress` events from
   `execute_benchmark`.

### Design Priorities

1. **Responsiveness over completeness** — show something immediately, fill
   in details as they arrive. Don't block the UI waiting for API responses.
2. **Works over high-latency links** — the user may be on a VPN or across
   continents from the server. SSE with reconnection, not WebSocket.
3. **Keyboard-driven** — no mouse required. Vim-style navigation for
   scrolling through history. Slash commands for actions.
4. **Minimal dependencies** — single binary, no runtime requirements.
   Should work in tmux, screen, and bare terminals.
5. **Graceful degradation** — if the server doesn't support SSE yet, fall
   back to polling. If the terminal doesn't support colors, degrade to
   plain text.

### Technology Considerations

- **Rust + ratatui** or **Go + bubbletea** are the natural choices for a
  cross-platform terminal UI with good async networking
- The binary should be small enough to scp to a jump host
- Configuration stored in a simple TOML/YAML file
- Token can be read from the file, an environment variable, or prompted

### Relationship to Existing Tools

This replaces the current workflow of:
- `curl` for ticket CRUD → `/submit`, `/tickets`, `/abort`
- `tail -f orchestrator.log` → `/follow`
- `curl PATCH customer_response` → inline HITL responses
- `cat events/*.jsonl` → `/logs`

The REST API remains the source of truth. The TUI is a client that makes
the API ergonomic for interactive use.
