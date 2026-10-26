"""Regression tests for snapshot-driven dashboard interactions."""

from __future__ import annotations

import subprocess
from pathlib import Path

INDEX = Path(__file__).parents[1] / "state_store" / "static" / "index.html"


def _function_source(html: str, name: str, next_name: str) -> str:
    start = html.index(name)
    end = html.index(next_name, start)
    return html[start:end]


def test_activity_poll_advances_past_a_hidden_only_page() -> None:
    html = INDEX.read_text(encoding="utf-8")
    poller = _function_source(
        html,
        "async function pollEvents(ticketId)",
        "function updateIntrospectionCard(ticketId, cachedTicket)",
    )
    script = (
        "let pollPaused = false; let eventPollInFlight = false;\n"
        "let lastSeq = 7; let snapshotTotalEvents = 7;\n"
        "let currentTicketId = 'PERF-1'; let trailStates = []; let eventView = 'activity';\n"
        "let pollUrls = []; let rendered = [];\n"
        "let chatEnabled = false;\n"
        "async function api(path) {\n"
        "  if (path.includes('/events?')) { pollUrls.push(path); return pollUrls.length === 1 ? {events: [], latest_seq: 11} : {events: [{seq: 12, event_source: 'audit'}], latest_seq: 12}; }\n"
        "  return {status: 'executing_benchmark'};\n"
        "}\n"
        "const document = {getElementById: function() { return null; }};\n"
        "function renderNewEvents(events) { rendered = rendered.concat(events); } function rebuildTrail() {}\n"
        "function statusBadge() { return ''; } function actionButtonsHtml() { return ''; }\n"
        "function bindActionButtons() {} function refreshResultSection() {}\n"
        "function updateUsageSidebar() {} function updateIntrospectionCard() {}\n"
        "function isTicketTerminal() { return false; }\n"
        + poller
        + r"""
(async function() {
  const assert = require('node:assert/strict');
  await pollEvents('PERF-1');
  assert.equal(lastSeq, 11);
  assert.equal(snapshotTotalEvents, 11);
  assert.match(pollUrls[0], /view=activity/);
  eventView = 'audit';
  await pollEvents('PERF-1');
  assert.match(pollUrls[1], /view=audit/);
  assert.equal(lastSeq, 12);
  assert.deepEqual(rendered.map(function(evt) { return evt.seq; }), [12]);
})();
"""
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_changing_event_view_refetches_audit_inclusive_snapshot() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert "eventView = this.value;\n      reloadEventSnapshot(ticketId);" in html
    reloader = _function_source(
        html,
        "function showLoadEarlierButton(ticketId, label)",
        "async function loadEarlierEvents(ticketId,",
    )
    script = (
        "let currentTicketId = 'PERF-2'; let eventSnapshotRequestId = 0;\n"
        "let historyLoadRequestId = 0; let historyLoadInFlight = false;\n"
        "let eventView = 'audit'; let eventSnapshotRecent = 75;\n"
        "let eventRecords = []; let maxRenderedSeq = 0; let renderedEventCount = 0;\n"
        "let autoScroll = true; let lastSeq = 0; let snapshotTotalEvents = 0;\n"
        "let autoHistoryPagesLoaded = 0; const MAX_AUTO_HISTORY_PAGES = 3;\n"
        "let earliestLoadedSeq = Infinity; let eventSearch = ''; let condenseEvents = true;\n"
        "let requestUrl = '';\n"
        "async function api(url) { requestUrl = url; return {latest_seq: 9, total_events: 9, recent_events: [{seq: 9, event_source: 'audit', audit: {outcome: 'success'}}], usage: {}}; }\n"
        "function renderNewEvents(events) { eventRecords = eventRecords.concat(events); }\n"
        "function renderEventStream() {} function renderUsageData() {}\n"
        "function requestAnimationFrame(fn) { fn(); }\n"
        "const stream = {textContent: '', innerHTML: '', scrollTop: 0, scrollHeight: 200, clientHeight: 100};\n"
        "const button = {addEventListener: function() {}};\n"
        "const history = {innerHTML: '', querySelector: function() { return button; }};\n"
        "const document = {getElementById: function(id) { return id === 'event-stream' ? stream : id === 'load-history-container' ? history : {textContent: ''}; }};\n"
        "function escHtml(value) { return value; }\n"
        + reloader
        + r"""
(async function() {
  const assert = require('node:assert/strict');
  await reloadEventSnapshot('PERF-2');
  assert.match(requestUrl, /recent=75&view=audit/);
  assert.deepEqual(eventRecords.map(function(evt) { return evt.seq; }), [9]);
  assert.equal(lastSeq, 9);
})();
"""
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_stale_initial_snapshot_cannot_overwrite_selected_view() -> None:
    html = INDEX.read_text(encoding="utf-8")
    detail = _function_source(
        html,
        "async function renderDetail(ticketId)",
        "function downloadWithAuth(url, filename)",
    )
    apply_snapshot = _function_source(
        html,
        "function applyInitialEventSnapshot(",
        "async function renderDetail(ticketId)",
    )
    assert "var initialSnapshotRequestId = eventSnapshotRequestId;" in detail
    assert "applyInitialEventSnapshot(" in detail
    script = (
        "let currentTicketId = 'PERF-4'; let eventSnapshotRequestId = 2; let eventView = 'audit';\n"
        "let lastSeq = 9; let snapshotTotalEvents = 1; let earliestLoadedSeq = 9;\n"
        "let eventRecords = [{seq: 9, event_source: 'audit'}]; let usageSnapshot = null;\n"
        "const document = {getElementById: function() { return null; }};\n"
        "function renderNewEvents(events) { eventRecords = eventRecords.concat(events); }\n"
        "function renderUsageData(snapshot) { usageSnapshot = snapshot; }\n"
        + apply_snapshot
        + r"""
(async function() {
  const assert = require('node:assert/strict');
  const stale = applyInitialEventSnapshot(
    {latest_seq: 3, total_events: 3, recent_events: [{seq: 3}]},
    'PERF-4', 1, 'activity'
  );
  assert.equal(stale, null);
  assert.equal(lastSeq, 9);
  assert.deepEqual(eventRecords.map(function(evt) { return evt.seq; }), [9]);
  assert.equal(usageSnapshot, null);

  const current = applyInitialEventSnapshot(
    {latest_seq: 10, total_events: 2, recent_events: [{seq: 10}]},
    'PERF-4', 2, 'audit'
  );
  assert.deepEqual(current.map(function(evt) { return evt.seq; }), [10]);
  assert.equal(lastSeq, 10);
  assert.deepEqual(eventRecords.map(function(evt) { return evt.seq; }), [9, 10]);
})();
"""
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_underfill_animation_frame_drops_a_stale_view_request() -> None:
    html = INDEX.read_text(encoding="utf-8")
    scheduler = _function_source(
        html,
        "function showLoadEarlierButton(ticketId, label)",
        "async function reloadEventSnapshot(ticketId)",
    )
    script = (
        "let currentTicketId = 'PERF-7'; let eventSnapshotRequestId = 3;\n"
        "let eventView = 'activity'; let autoHistoryPagesLoaded = 0;\n"
        "const MAX_AUTO_HISTORY_PAGES = 3; let earliestLoadedSeq = 50;\n"
        "let historyLoadInFlight = false; let queuedFrame = null; let loadCalls = [];\n"
        "const stream = {scrollHeight: 100, clientHeight: 300};\n"
        "const history = {innerHTML: '', querySelector: function() { return null; }};\n"
        "const document = {getElementById: function(id) { return id === 'event-stream' ? stream : history; }};\n"
        "function requestAnimationFrame(fn) { queuedFrame = fn; }\n"
        "function escHtml(value) { return value; }\n"
        "function loadEarlierEvents(ticketId, autoFill) { loadCalls.push([ticketId, autoFill]); }\n"
        + scheduler
        + r"""
(function() {
  const assert = require('node:assert/strict');
  scheduleEarlierEventsIfUnderfilled('PERF-7');
  const staleFrame = queuedFrame;
  eventSnapshotRequestId = 4;
  eventView = 'audit';
  staleFrame();
  assert.deepEqual(loadCalls, []);

  scheduleEarlierEventsIfUnderfilled('PERF-7');
  queuedFrame();
  assert.deepEqual(loadCalls, [['PERF-7', true]]);
})();
"""
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_activity_underfill_stops_at_budget_and_keeps_manual_history_access() -> None:
    html = INDEX.read_text(encoding="utf-8")
    scheduler = _function_source(
        html,
        "function showLoadEarlierButton(ticketId, label)",
        "async function reloadEventSnapshot(ticketId)",
    )
    visibility = _function_source(
        html,
        "function isAuditEvent(evt)",
        "function stableEventValue(value)",
    )
    loader = _function_source(
        html,
        "async function loadEarlierEvents(ticketId,",
        "function renderNewEvents(events)",
    )
    script = (
        "let currentTicketId = 'PERF-5'; let eventSnapshotRequestId = 4;\n"
        "let historyLoadRequestId = 0; let historyLoadInFlight = false;\n"
        "let eventView = 'activity'; let earliestLoadedSeq = 100; let eventPageSize = 15;\n"
        "let autoHistoryPagesLoaded = 0; const MAX_AUTO_HISTORY_PAGES = 3;\n"
        "let eventRecords = []; let autoScroll = true; let requests = 0; let requestUrls = [];\n"
        "const stream = {scrollHeight: 100, clientHeight: 300, scrollTop: 0};\n"
        "const button = {handler: null, addEventListener: function(name, fn) { if (name === 'click') this.handler = fn; }};\n"
        "const history = {innerHTML: '', querySelector: function() { return button; }};\n"
        "const document = {getElementById: function(id) { return id === 'event-stream' ? stream : history; }};\n"
        "function escHtml(value) { return value; }\n"
        "function requestAnimationFrame(fn) { fn(); }\n"
        "function renderEventStream() { stream.scrollHeight = 100 + eventRecords.filter(eventMatchesView).length * 90; }\n"
        "async function api(url) {\n"
        "  requestUrls.push(url); requests++;\n"
        "  const start = 100 - 15 * requests;\n"
        "  const events = Array.from({length: 15}, function(_, i) {\n"
        "    const evt = {seq: start + i, event_source: 'activity', event_type: i % 3 === 0 ? 'transition' : i % 3 === 1 ? 'context_resolution' : 'tool_called', agent: 'introspection-agent'};\n"
        "    if (requests === 4 && i === 0) { evt.event_type = 'tool_called'; evt.agent = 'worker'; }\n"
        "    return evt;\n"
        "  });\n"
        "  return {events: events};\n"
        "}\n"
        + visibility
        + scheduler
        + loader
        + r"""
(async function() {
  const assert = require('node:assert/strict');
  scheduleEarlierEventsIfUnderfilled('PERF-5');
  await new Promise(function(resolve) { setTimeout(resolve, 20); });
  assert.equal(requests, 3);
  assert.equal(autoHistoryPagesLoaded, 3);
  assert.equal(stream.scrollHeight, 100);
  assert.equal(eventRecords.length, 45);
  assert.ok(requestUrls.every(function(url) { return /view=activity/.test(url); }));
  assert.equal(typeof button.handler, 'function');

  button.handler();
  await new Promise(function(resolve) { setTimeout(resolve, 20); });
  assert.equal(requests, 4);
  assert.equal(stream.scrollHeight, 190);
  assert.equal(eventRecords.length, 60);
  assert.equal(typeof button.handler, 'function');
})();
"""
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_terminal_summary_retry_handles_missing_intro_and_fetch_errors() -> None:
    html = INDEX.read_text(encoding="utf-8")
    updater = _function_source(
        html,
        "function clearIntrospectionSummaryRetry()",
        "function renderIntrospectionContent(intro)",
    )
    script = (
        "let currentTicketId = 'PERF-6'; let introspectionPollInFlight = false;\n"
        "let introspectionRetryTimer = null; let introspectionRetryDeadline = 0;\n"
        "let introspectionRetryAttempts = 0; let introspectionTerminalRetryTicketId = null;\n"
        "let retryCallback = null; let retryDelays = []; let fetches = 0;\n"
        "const card = {classList: {add: function() {}, remove: function() {}}};\n"
        "const content = {dataset: {}, innerHTML: '', querySelector: function() { return null; }};\n"
        "const document = {getElementById: function(id) { return id === 'introspection-card' ? card : id === 'introspection-content' ? content : null; }};\n"
        "function isTicketTerminal(status) { return status === 'closed'; }\n"
        "function renderIntrospectionContent() { return 'interim'; }\n"
        "function renderIntrospectionSummary() { return 'final summary'; }\n"
        "function _saveNarrativeScroll() { return 0; } function _restoreNarrativeScroll() {}\n"
        "function _attachNarrativeOpenClick() {}\n"
        "function setTimeout(fn, delay) { retryDelays.push(delay); retryCallback = fn; return retryDelays.length; }\n"
        "function clearTimeout() {}\n"
        "async function api() { fetches++; if (fetches === 1) throw new Error('temporary failure'); "
        "if (fetches === 2) return {status: 'closed', custom_fields: {}}; "
        "return {status: 'closed', custom_fields: {introspection_summary: {text: 'done'}}}; }\n"
        + updater
        + r"""
(async function() {
  const assert = require('node:assert/strict');
  updateIntrospectionCard('PERF-6', {status: 'closed', custom_fields: {}});
  await new Promise(function(resolve) { setImmediate(resolve); });
  assert.equal(typeof retryCallback, 'function');
  assert.equal(retryDelays[0], 2000);

  retryCallback();
  await new Promise(function(resolve) { setImmediate(resolve); });
  assert.equal(fetches, 1);
  assert.equal(retryDelays[1], 4000);

  retryCallback();
  await new Promise(function(resolve) { setImmediate(resolve); });
  assert.equal(fetches, 2);
  assert.equal(retryDelays[2], 8000);

  retryCallback();
  await new Promise(function(resolve) { setImmediate(resolve); });
  assert.equal(fetches, 3);
  assert.equal(content.dataset.summaryRendered, '1');
  assert.equal(content.innerHTML, 'final summary');
  assert.equal(introspectionRetryTimer, null);

  let timeoutCalls = 0;
  const originalNow = Date.now;
  Date.now = function() { return 1000; };
  introspectionTerminalRetryTicketId = 'PERF-6';
  introspectionRetryDeadline = 999;
  const priorDelays = retryDelays.length;
  scheduleIntrospectionSummaryRetry('PERF-6');
  timeoutCalls = retryDelays.length - priorDelays;
  assert.equal(timeoutCalls, 0);
  assert.equal(introspectionRetryTimer, null);
  Date.now = originalNow;
})();
"""
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
