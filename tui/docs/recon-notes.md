# TUI Phase 0 — Recon Notes

## V1: Interject Hook Point

### Finding: Agent does NOT re-fetch ticket on every iteration by default

The `AgentBase.run()` main loop (`agents/base.py:135-534`) checks
`self._stop_requested` (an in-memory flag, line 140) at the top of
each iteration. This is set by the orchestrator's
`Dispatcher.stop_agent()` — it does not involve a ticket re-fetch.

The only ticket re-fetch within the loop happens inside
`_check_budget()` (line 749: `ticket = await self._get_ticket(ticket_id)`)
which runs on `iteration > 1` when `self._events` is set (line 292).
Budget checking is the sole place where the agent refreshes ticket state
mid-loop.

### Confirmed: `pending_interject` in custom_fields is invisible to dispatcher

The dispatcher (`orchestrator/dispatcher.py`) maps ticket **status** to
agent type via `STATUS_AGENT_MAP` (line 28). It never inspects
`custom_fields` for dispatch decisions — only for `stop_requested`
processing (handled in `orchestrator/main.py:_process_stop_requests`,
line 691). Writing `pending_interject` to custom_fields will not
interfere with dispatch logic.

### Recommended S2b Design (pickup in agent loop)

Add an interject check immediately after the stop-request check (after
line 152, before the LLM call). This runs on every iteration including
iteration 1:

```python
# After stop check (line 152), before LLM call:
ticket = await self._get_ticket(ticket_id)
cf = ticket.get("custom_fields", {})
interject = cf.get("pending_interject")
if interject:
    messages.append({
        "role": "user",
        "content": f"[USER INTERJECTION] {interject['message']}",
    })
    await self._update_fields(ticket_id, {"pending_interject": None})
    self._emit(ticket_id, "user_interjection", {
        "message": interject["message"],
    })
```

This adds one HTTP call per iteration (the `_get_ticket`). If the
budget check already fetched the ticket on this iteration, we could
share that fetch — but keeping them separate is simpler and the
extra call is negligible at the ~5s+ iteration cadence.

**No contradictions with the plan.** The plan's S2b description at §4.2
matches this approach exactly.

---

## V2: Event-Bus Read Path

### Confirmed: JSONL files are shared between processes

Both processes use `EventBus()` with no arguments, which defaults to
`paths.LOG_DIR` = `~/.agentic-perf/logs` (paths.py:13).

- **Orchestrator** (`orchestrator/main.py:754`): `events = EventBus()`
  — writes events via `emit()` which appends to `{ticket_id}.jsonl`
  files in `LOG_DIR`.
- **State store** (`state_store/main.py:40`): `app.state.event_bus = EventBus()`
  — reads events via `get_events()` → `_read_from_file()` which reads
  those same JSONL files.

The `get_events()` method (events.py:269) merges in-memory events with
file-based events, deduplicating by seq. Since the state store's
EventBus never calls `emit()`, its in-memory cache is always empty,
and all events come from the JSONL files written by the orchestrator.

### S1 SSE Viability: Confirmed

The SSE endpoint can poll `EventBus.get_events()` from the state store
process. This reads the JSONL files written by the orchestrator's
EventBus. The `_read_from_file()` method (events.py:284) opens and
reads the file on each call — no file handle caching on the read side —
so it always sees the latest data.

The `since` parameter works correctly: it filters by line number
(renumbered sequentially during read), not by embedded seq values.
This means the SSE endpoint can maintain a cursor per ticket and
only return new events.

### Critical Constraint Verified

The state store's EventBus instance **never calls `emit()`**. Searching
the `state_store/` directory confirms no code path writes events. All
event emission happens in the orchestrator process via `agents/base.py`
and `orchestrator/main.py`. This preserves the monotonic seq guarantee
described in the plan §3.5.

**No contradictions with the plan.**

---

## Test Environment Isolation

For running integration tests without colliding with a production
agentic-perf instance on the same host:

| Env Var | Purpose | Example |
|---|---|---|
| `AGENTIC_PERF_HOME` | Data/config/logs directory | `/tmp/aptui-test` |
| `STORE_PORT` | State store listen port | `18090` |
| `STATE_STORE_URL` | Orchestrator → state store URL | `http://localhost:18090` |
| `AGENTIC_PERF_SECRETS` | Secrets directory (can share) | `~/.agentic-perf/secrets` |
