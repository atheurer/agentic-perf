"""Regression coverage for chat stream error messages in the dashboard."""

from __future__ import annotations

import subprocess
from pathlib import Path

INDEX = Path(__file__).parents[1] / "state_store" / "static" / "index.html"


def test_chat_stream_keeps_http_errors_user_visible() -> None:
    html = INDEX.read_text(encoding="utf-8")
    start = html.index("async function _streamChatRequest")
    end = html.index("\nasync function sendChatMessage()", start)
    helper = html[start:end]
    script = (
        "const assert = require('node:assert/strict');\n"
        "const API = '/api/v1';\n"
        "function getAuthHeaders() { return {}; }\n"
        "function updateChatCost() {}\n"
        "function appendChatMessage() {}\n"
        "async function refreshChatRecoveryStatus() { return {indeterminate: false, delivery_pending: false, request_in_flight: false}; }\n"
        "const document = {getElementById: function() { return null; }};\n"
        "let status = 0;\n"
        "globalThis.fetch = async function() {\n"
        "  return {ok: false, status: status, body: null};\n"
        "};\n"
        + helper
        + r"""
(async function() {
  const cases = [
    [401, 'Authentication required. Please log in to use the chat.'],
    [429, 'Rate limit reached. Please wait a moment and try again.'],
    [503, 'Chat agent is not available. Check that chat is enabled in the configuration.'],
    [504, 'The request timed out. Check chat status before retrying.'],
    [500, 'Something went wrong (HTTP 500).']
  ];
  for (const item of cases) {
    status = item[0];
    const result = await _streamChatRequest('hello', undefined, 'chat', 'cost');
    assert.equal(result.ok, false);
    assert.equal(result.error, item[1]);
  }
})().catch(function(error) {
  process.nextTick(function() { throw error; });
});
"""
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_chat_recovery_ui_explains_reconciliation_and_handles_clear_conflict() -> None:
    html = INDEX.read_text(encoding="utf-8")
    start = html.index("function updateChatRecovery")
    end = html.index("\nfunction appendChatMessage", start)
    recovery = html[start:end]
    script = (
        "const assert = require('node:assert/strict');\n"
        "const API = '/api/v1';\n"
        "let currentTicketForChat = null;\n"
        "let chatSessionIndeterminate = false;\n"
        "let chatDeliveryPending = false;\n"
        "let chatRequestInFlight = false;\n"
        "let chatRecoveryStatusUnavailable = false;\n"
        "let status = 409;\n"
        "const messages = [];\n"
        "const elements = new Map();\n"
        "function element(id) {\n"
        "  if (!elements.has(id)) elements.set(id, {style: {}, innerHTML: '', textContent: '', querySelector: function(selector) { if (selector === 'div') return this.message; if (selector === '.chat-recovery-refresh') return this.refreshButton; return this.clearButton; }, message: {textContent: ''}, refreshButton: {style: {}, disabled: false}, clearButton: {style: {}, disabled: false}});\n"
        "  return elements.get(id);\n"
        "}\n"
        "const document = {\n"
        "  getElementById: element,\n"
        "  querySelector: function(selector) {\n"
        "    if (selector === '#chat-recovery .chat-recovery-clear') return element('chat-recovery').clearButton;\n"
        "    if (selector === '#ticket-chat-recovery .chat-recovery-clear') return element('ticket-chat-recovery').clearButton;\n"
        "    return null;\n"
        "  }\n"
        "};\n"
        "const window = {confirm: function() { return true; }};\n"
        "function getAuthHeaders() { return {Authorization: 'Bearer user'}; }\n"
        "function appendChatMessage(id, role, text) { messages.push([id, role, text]); }\n"
        "function renderChatMessages(id, rows) { element(id).innerHTML = JSON.stringify(rows); }\n"
        "function updateChatCost() {}\n"
        "globalThis.fetch = async function(url, options) {\n"
        "  assert.equal(url, API + '/chat/session');\n"
        "  assert.equal(options.method, 'DELETE');\n"
        "  assert.equal(options.headers.Authorization, 'Bearer user');\n"
        "  return {ok: status !== 409, status: status};\n"
        "};\n"
        + recovery
        + r"""
(async function() {
  updateChatRecovery(true);
  assert.equal(element('chat-recovery').style.display, 'block');
  assert.equal(element('ticket-chat-recovery').style.display, 'block');
  await clearChatSession();
  assert.equal(chatSessionIndeterminate, true);
  assert.match(messages[0][2], /still finishing/i);

  status = 204;
  await clearChatSession();
  assert.equal(chatSessionIndeterminate, false);
  assert.equal(element('chat-recovery').style.display, 'none');
  assert.equal(element('ticket-chat-recovery').style.display, 'none');
  assert.equal(element('chat-messages').innerHTML, '[]');

  updateChatRecovery(false, true);
  assert.equal(chatDeliveryPending, true);
  assert.equal(element('chat-recovery').style.display, 'block');
  assert.match(element('chat-recovery').message.textContent, /waiting for browser confirmation/i);
  assert.equal(element('chat-recovery').clearButton.style.display, 'none');

  updateChatRecovery(false, false, false, true);
  assert.equal(chatRequestInFlight, true);
  assert.equal(element('chat-recovery').style.display, 'block');
  assert.equal(element('ticket-chat-recovery').style.display, 'block');
  assert.match(element('chat-recovery').message.textContent, /still running/i);
  assert.equal(element('chat-recovery').refreshButton.style.display, '');
  assert.equal(element('ticket-chat-recovery').refreshButton.style.display, '');
  assert.equal(element('chat-recovery').clearButton.style.display, 'none');
})().catch(function(error) {
  process.nextTick(function() { throw error; });
});
"""
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_delivery_ack_retries_and_polls_until_recovery_status_changes() -> None:
    html = INDEX.read_text(encoding="utf-8")
    start = html.index("function updateChatRecovery")
    end = html.index("\nfunction appendChatMessage", start)
    recovery = html[start:end]
    script = (
        "const assert = require('node:assert/strict');\n"
        "const API = '/api/v1';\n"
        "let currentTicketForChat = null;\n"
        "let chatSessionIndeterminate = false;\n"
        "let chatDeliveryPending = false;\n"
        "let chatRequestInFlight = false;\n"
        "let chatRecoveryStatusUnavailable = false;\n"
        "let chatRecoveryMonitor = null;\n"
        "const elements = new Map();\n"
        "function element(id) { if (!elements.has(id)) elements.set(id, {style: {}, querySelector: function(selector) { return selector === 'div' ? this.message : this.button; }, message: {textContent: ''}, button: {style: {}, disabled: false}}); return elements.get(id); }\n"
        "const document = {getElementById: element, querySelector: function(selector) { return selector === '#chat-recovery button' ? element('chat-recovery').button : element('ticket-chat-recovery').button; }};\n"
        "const window = {confirm: function() { return true; }};\n"
        "function getAuthHeaders() { return {}; }\n"
        "function appendChatMessage() {}\n"
        "function renderChatMessages() {}\n"
        "function updateChatCost() {}\n"
        "let ackCalls = 0;\n"
        "let historyCalls = 0;\n"
        "const nativeSetTimeout = globalThis.setTimeout;\n"
        "globalThis.setTimeout = function(callback, delay) { return nativeSetTimeout(callback, delay === 1000 ? 0 : delay); };\n"
        "globalThis.fetch = async function(url, options) {\n"
        "  if (url === API + '/chat/message/ack') { ackCalls++; return {ok: true, json: async function() { return {acknowledged: false}; }}; }\n"
        "  if (url === API + '/chat/history') { historyCalls++; const indeterminate = historyCalls >= 5; return {ok: true, json: async function() { return {indeterminate: indeterminate, delivery_pending: !indeterminate, request_in_flight: false}; }}; }\n"
        "  throw new Error('unexpected URL ' + url);\n"
        "};\n"
        + recovery
        + r"""
(async function() {
  const acknowledged = await acknowledgeChatDelivery('request-A');
  assert.equal(acknowledged, false);
  assert.ok(chatRecoveryMonitor);
  const monitor = chatRecoveryMonitor;
  await monitor;
  assert.equal(ackCalls, 5);
  assert.equal(historyCalls, 5);
  assert.equal(chatSessionIndeterminate, true);
  assert.equal(element('chat-recovery').style.display, 'block');
})().catch(function(error) {
  process.nextTick(function() { throw error; });
});
"""
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_delivery_recovery_retries_status_after_transient_history_outage() -> None:
    html = INDEX.read_text(encoding="utf-8")
    start = html.index("function updateChatRecovery")
    end = html.index("\nfunction appendChatMessage", start)
    recovery = html[start:end]
    script = (
        "const assert = require('node:assert/strict');\n"
        "const API = '/api/v1';\n"
        "let currentTicketForChat = null;\n"
        "let chatSessionIndeterminate = false;\n"
        "let chatDeliveryPending = false;\n"
        "let chatRequestInFlight = false;\n"
        "let chatRecoveryStatusUnavailable = false;\n"
        "let chatRecoveryMonitor = null;\n"
        "const elements = new Map();\n"
        "function element(id) { if (!elements.has(id)) elements.set(id, {style: {}, querySelector: function(selector) { if (selector === 'div') return this.message; if (selector === '.chat-recovery-refresh') return this.refreshButton; return this.clearButton; }, message: {textContent: ''}, refreshButton: {style: {}}, clearButton: {style: {}}}); return elements.get(id); }\n"
        "const document = {getElementById: element, querySelector: function() { return null; }};\n"
        "function getAuthHeaders() { return {}; }\n"
        "function appendChatMessage() {}\n"
        "function renderChatMessages() {}\n"
        "function updateChatCost() {}\n"
        "let ackCalls = 0;\n"
        "let historyCalls = 0;\n"
        "const nativeSetTimeout = globalThis.setTimeout;\n"
        "globalThis.setTimeout = function(callback, delay) { return nativeSetTimeout(callback, delay === 1000 ? 0 : delay); };\n"
        "globalThis.fetch = async function(url) {\n"
        "  if (url === API + '/chat/message/ack') { ackCalls++; return {ok: true, json: async function() { return {acknowledged: false}; }}; }\n"
        "  if (url === API + '/chat/history') { historyCalls++; if (historyCalls === 1) throw new Error('temporary outage'); const indeterminate = historyCalls >= 5; return {ok: true, json: async function() { return {indeterminate: indeterminate, delivery_pending: !indeterminate, request_in_flight: false}; }}; }\n"
        "  throw new Error('unexpected URL ' + url);\n"
        "};\n"
        + recovery
        + r"""
(async function() {
  assert.equal(await acknowledgeChatDelivery('request-A'), false);
  assert.ok(chatRecoveryMonitor);
  await chatRecoveryMonitor;
  assert.ok(ackCalls > 2);
  assert.ok(historyCalls >= 5);
  assert.equal(chatRecoveryMonitor, null);
  assert.equal(chatSessionIndeterminate, true);
  assert.equal(chatRecoveryStatusUnavailable, false);
  assert.equal(element('chat-recovery').style.display, 'block');
  assert.equal(element('ticket-chat-recovery').style.display, 'block');
})().catch(function(error) {
  process.nextTick(function() { throw error; });
});
"""
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_delivery_monitor_covers_server_watchdog_window() -> None:
    html = INDEX.read_text(encoding="utf-8")
    start = html.index("function updateChatRecovery")
    end = html.index("\nfunction appendChatMessage", start)
    recovery = html[start:end]
    script = (
        "const assert = require('node:assert/strict');\n"
        "const API = '/api/v1';\n"
        "let currentTicketForChat = null;\n"
        "let chatSessionIndeterminate = false;\n"
        "let chatDeliveryPending = false;\n"
        "let chatRequestInFlight = false;\n"
        "let chatRecoveryStatusUnavailable = false;\n"
        "let chatRecoveryMonitor = null;\n"
        "const elements = new Map();\n"
        "function element(id) { if (!elements.has(id)) elements.set(id, {style: {}, querySelector: function(selector) { return selector === 'div' ? this.message : this.button; }, message: {textContent: ''}, button: {style: {}, disabled: false}}); return elements.get(id); }\n"
        "const document = {getElementById: element, querySelector: function() { return null; }};\n"
        "function getAuthHeaders() { return {}; }\n"
        "function appendChatMessage() {}\n"
        "function renderChatMessages() {}\n"
        "function updateChatCost() {}\n"
        "let ackCalls = 0;\n"
        "let historyCalls = 0;\n"
        "let fakeNow = 0;\n"
        "Date.now = function() { return fakeNow; };\n"
        "const nativeSetTimeout = globalThis.setTimeout;\n"
        "globalThis.setTimeout = function(callback, delay) { if (delay === 1000) { fakeNow += 1000; return nativeSetTimeout(callback, 0); } return nativeSetTimeout(callback, delay); };\n"
        "globalThis.fetch = async function(url) {\n"
        "  if (url === API + '/chat/message/ack') { ackCalls++; return {ok: true, json: async function() { return {acknowledged: false}; }}; }\n"
        "  if (url === API + '/chat/history') { historyCalls++; const indeterminate = fakeNow >= 30000; return {ok: true, json: async function() { return {indeterminate: indeterminate, delivery_pending: !indeterminate, request_in_flight: false}; }}; }\n"
        "  throw new Error('unexpected URL ' + url);\n"
        "};\n"
        + recovery
        + r"""
(async function() {
  assert.equal(await acknowledgeChatDelivery('request-A'), false);
  assert.ok(chatRecoveryMonitor);
  await chatRecoveryMonitor;
  assert.ok(fakeNow >= 30000);
  assert.ok(historyCalls >= 32);
  assert.ok(ackCalls >= 31);
  assert.equal(chatSessionIndeterminate, true);
  assert.equal(chatRecoveryMonitor, null);
})().catch(function(error) {
  process.nextTick(function() { throw error; });
});
"""
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_fully_unavailable_delivery_status_keeps_manual_recovery_on_both_surfaces() -> (
    None
):
    html = INDEX.read_text(encoding="utf-8")
    start = html.index("function updateChatRecovery")
    end = html.index("\nfunction appendChatMessage", start)
    recovery = html[start:end]
    script = (
        "const assert = require('node:assert/strict');\n"
        "const API = '/api/v1';\n"
        "let currentTicketForChat = null;\n"
        "let chatSessionIndeterminate = false;\n"
        "let chatDeliveryPending = false;\n"
        "let chatRequestInFlight = false;\n"
        "let chatRecoveryStatusUnavailable = false;\n"
        "let chatRecoveryMonitor = null;\n"
        "let recoveryAvailable = false;\n"
        "const elements = new Map();\n"
        "function element(id) { if (!elements.has(id)) elements.set(id, {style: {}, querySelector: function(selector) { if (selector === 'div') return this.message; if (selector === '.chat-recovery-refresh') return this.refreshButton; return this.clearButton; }, message: {textContent: ''}, refreshButton: {style: {}}, clearButton: {style: {}}}); return elements.get(id); }\n"
        "const document = {getElementById: element, querySelector: function() { return null; }};\n"
        "function getAuthHeaders() { return {}; }\n"
        "function appendChatMessage() {}\n"
        "function renderChatMessages() {}\n"
        "function updateChatCost() {}\n"
        "let ackCalls = 0;\n"
        "let historyCalls = 0;\n"
        "const nativeSetTimeout = globalThis.setTimeout;\n"
        "globalThis.setTimeout = function(callback, delay) { return nativeSetTimeout(callback, delay === 1000 ? 0 : delay); };\n"
        "globalThis.fetch = async function(url) {\n"
        "  if (url === API + '/chat/message/ack') { ackCalls++; return {ok: false}; }\n"
        "  if (url === API + '/chat/history') { historyCalls++; if (!recoveryAvailable) throw new Error('offline'); return {ok: true, json: async function() { return {indeterminate: true, delivery_pending: false, request_in_flight: false}; }}; }\n"
        "  throw new Error('unexpected URL ' + url);\n"
        "};\n"
        + recovery
        + r"""
(async function() {
  assert.equal(await acknowledgeChatDelivery('request-A'), false);
  assert.ok(chatRecoveryMonitor);
  await chatRecoveryMonitor;
  assert.equal(chatRecoveryMonitor, null);
  assert.ok(ackCalls <= 38);
  assert.ok(historyCalls <= 38);
  assert.equal(chatRecoveryStatusUnavailable, true);
  assert.equal(element('chat-recovery').style.display, 'block');
  assert.equal(element('ticket-chat-recovery').style.display, 'block');
  assert.equal(element('chat-recovery').refreshButton.style.display, '');
  assert.equal(element('ticket-chat-recovery').refreshButton.style.display, '');
  assert.equal(element('chat-recovery').clearButton.style.display, 'none');

  recoveryAvailable = true;
  assert.equal(await refreshChatRecoveryManually(), true);
  assert.equal(chatRecoveryStatusUnavailable, false);
  assert.equal(chatSessionIndeterminate, true);
  assert.equal(element('chat-recovery').clearButton.style.display, '');
  assert.equal(element('ticket-chat-recovery').clearButton.style.display, '');
})().catch(function(error) {
  process.nextTick(function() { throw error; });
});
"""
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_stream_uses_structured_indeterminate_status_and_acknowledges_response_id() -> (
    None
):
    html = INDEX.read_text(encoding="utf-8")
    start = html.index("async function _streamChatRequest")
    end = html.index("\nasync function sendChatMessage()", start)
    helper = html[start:end]
    assert "outcome is uncertain" not in helper
    script = (
        "const assert = require('node:assert/strict');\n"
        "const API = '/api/v1';\n"
        "const updates = [];\n"
        "const acknowledgements = [];\n"
        "function getAuthHeaders() { return {Authorization: 'Bearer user'}; }\n"
        "function updateChatCost() {}\n"
        "function appendChatMessage() {}\n"
        "function updateChatRecovery(indeterminate, pending) { updates.push([indeterminate, pending]); }\n"
        "async function refreshChatRecoveryStatus() { return {indeterminate: true, delivery_pending: false}; }\n"
        "async function acknowledgeChatDelivery(id) { acknowledgements.push(id); return true; }\n"
        "const document = {getElementById: function() { return null; }};\n"
        'const bytes = new TextEncoder().encode(\'data: {"text":"Action completed"}\\n\\n\' + \'data: {"done":true,"delivery_id":"request-A","indeterminate":true}\\n\\n\');\n'
        "let read = false;\n"
        "globalThis.fetch = async function(url) {\n"
        "  assert.equal(url, API + '/chat/message/stream');\n"
        "  return {ok: true, body: {getReader: function() { return {read: async function() { if (read) return {done: true}; read = true; return {done: false, value: bytes}; }}; }}};\n"
        "};\n"
        + helper
        + r"""
(async function() {
  const result = await _streamChatRequest('start action', undefined, 'chat', 'cost');
  assert.equal(result.ok, true);
  assert.deepEqual(acknowledgements, ['request-A']);
  assert.deepEqual(updates, [[true, false]]);
})().catch(function(error) {
  process.nextTick(function() { throw error; });
});
"""
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_incomplete_stream_checks_status_before_allowing_retry() -> None:
    html = INDEX.read_text(encoding="utf-8")
    start = html.index("async function _streamChatRequest")
    end = html.index("\nasync function sendChatMessage()", start)
    helper = html[start:end]
    script = (
        "const assert = require('node:assert/strict');\n"
        "const API = '/api/v1';\n"
        "const updates = [];\n"
        "const acknowledgements = [];\n"
        "function getAuthHeaders() { return {}; }\n"
        "function updateChatCost() {}\n"
        "function appendChatMessage() {}\n"
        "function updateChatRecovery(indeterminate, pending) { updates.push([indeterminate, pending]); }\n"
        "async function refreshChatRecoveryStatus() { const state = {indeterminate: false, delivery_pending: true, request_in_flight: false}; updateChatRecovery(state.indeterminate, state.delivery_pending); return state; }\n"
        "async function acknowledgeChatDelivery(id) { acknowledgements.push(id); return true; }\n"
        "function startChatRecoveryMonitor() {}\n"
        "const document = {getElementById: function() { return null; }};\n"
        'const bytes = new TextEncoder().encode(\'data: {"text":"Action may have run"}\\n\\n\');\n'
        "let read = false;\n"
        "globalThis.fetch = async function() { return {ok: true, body: {getReader: function() { return {read: async function() { if (read) return {done: true}; read = true; return {done: false, value: bytes}; }}; }}}; };\n"
        + helper
        + r"""
(async function() {
  const result = await _streamChatRequest('start action', undefined, 'chat', 'cost');
  assert.equal(result.ok, false);
  assert.match(result.error, /still being finalized/i);
  assert.doesNotMatch(result.error, /please try again/i);
  assert.deepEqual(acknowledgements, []);
  assert.deepEqual(updates, [[false, true]]);
})().catch(function(error) {
  process.nextTick(function() { throw error; });
});
"""
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
