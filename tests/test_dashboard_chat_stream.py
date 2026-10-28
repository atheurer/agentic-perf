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
    [504, 'The request took longer than expected. Please try again — the chat will pick up where it left off.'],
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
