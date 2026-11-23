"""Tests for remembered-resume reply delivery (issue #1377).

`agentic-perf reply <ticket> "<instructions>" --remember` sets
remember_previous, so the resumed agent restores the stored conversation
snapshot and never calls _build_messages — the "## Previous Comments"
block carrying the user's reply was discarded, and the agent replayed
its stale prior outcome. The resume path must append the comments that
arrived after the snapshot's watermark so the resuming instruction is
always delivered. Also covers the multi-user comment-author fix: human
comments carry the submitting user's real name, not the literal "user".
"""

from __future__ import annotations

import copy
from typing import Any

from agents.base import AgentBase
from tests.test_investigation_context import _run


def _snapshot() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "User directive: KEEP THIS EXACT."},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "c1", "name": "read", "input": {}}],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": "result"}
            ],
        },
    ]


def _ticket_with_comments(comments: list[dict[str, Any]], **cf: Any) -> dict[str, Any]:
    return {
        "status": "triage_pending",
        "comments": comments,
        "custom_fields": cf,
    }


class TestRememberedResumeDeliversReply:
    async def test_new_comment_appended_after_snapshot(self):
        snapshot = _snapshot()
        original = copy.deepcopy(snapshot)
        ticket = _ticket_with_comments(
            [
                {"id": "c-1", "author": "user", "body": "original instruction"},
                {
                    "id": "c-2",
                    "author": "user",
                    "body": "root cause fixed, start a new execution",
                },
            ],
            remember_previous=True,
            previous_messages=snapshot,
            previous_messages_comment_ids=["c-1"],
        )
        _, resumed = await _run(ticket)
        # The snapshot is preserved verbatim...
        assert resumed[: len(original)] == original
        # ...and the reply that resumed the agent is appended after it.
        assert len(resumed) == len(original) + 1
        appended = resumed[-1]
        assert appended["role"] == "user"
        assert "root cause fixed, start a new execution" in appended["content"]
        assert "Comments Since the Saved Conversation" in appended["content"]
        # The watermarked (already-known) comment is not re-delivered.
        assert "original instruction" not in appended["content"]

    async def test_no_new_comments_leaves_snapshot_untouched(self):
        snapshot = _snapshot()
        original = copy.deepcopy(snapshot)
        ticket = _ticket_with_comments(
            [{"id": "c-1", "author": "user", "body": "original instruction"}],
            remember_previous=True,
            previous_messages=snapshot,
            previous_messages_comment_ids=["c-1"],
        )
        _, resumed = await _run(ticket)
        assert resumed == original

    async def test_missing_watermark_appends_all_user_comments(self):
        """Old snapshots carry no watermark; every user comment is delivered
        (duplication is acceptable, lost instructions are not)."""
        snapshot = _snapshot()
        ticket = _ticket_with_comments(
            [
                {"id": "c-1", "author": "user", "body": "original instruction"},
                {"id": "c-2", "author": "user", "body": "the resuming reply"},
            ],
            remember_previous=True,
            previous_messages=snapshot,
        )
        _, resumed = await _run(ticket)
        appended = resumed[-1]
        assert "the resuming reply" in appended["content"]
        assert "original instruction" in appended["content"]

    async def test_machine_comments_are_never_appended(self):
        snapshot = _snapshot()
        ticket = _ticket_with_comments(
            [
                {"id": "c-1", "author": "benchmark-agent", "body": "handoff noise"},
                {"id": "c-2", "author": "orchestrator", "body": "system note"},
                {"id": "c-3", "author": "user", "body": "the resuming reply"},
            ],
            remember_previous=True,
            previous_messages=snapshot,
        )
        _, resumed = await _run(ticket)
        appended = resumed[-1]
        assert "the resuming reply" in appended["content"]
        assert "handoff noise" not in appended["content"]
        assert "system note" not in appended["content"]

    async def test_multi_user_reply_is_delivered(self):
        """In multi-user mode comments are stored with the submitter's real
        username; the old author == "user" filter dropped them (#1377)."""
        snapshot = _snapshot()
        ticket = _ticket_with_comments(
            [{"id": "c-9", "author": "alice", "body": "please rerun the benchmark"}],
            remember_previous=True,
            previous_messages=snapshot,
        )
        _, resumed = await _run(ticket)
        appended = resumed[-1]
        assert "please rerun the benchmark" in appended["content"]
        assert "alice" in appended["content"]

    async def test_consecutive_remembered_resumes_do_not_reappend(self):
        """The appended comments must become part of the next watermark, not
        grow cumulatively across resumes."""
        snapshot = _snapshot()
        comments = [{"id": "c-2", "author": "user", "body": "first reply"}]
        ticket = _ticket_with_comments(
            comments,
            remember_previous=True,
            previous_messages=snapshot,
            previous_messages_comment_ids=["c-1"],
        )
        _, first = await _run(ticket)
        assert "first reply" in first[-1]["content"]

        # Second resume: the snapshot now contains the first reply's message
        # and the watermark includes c-2. A new reply (c-3) is the only new
        # comment delivered.
        comments.append({"id": "c-3", "author": "user", "body": "second reply"})
        ticket2 = _ticket_with_comments(
            comments,
            remember_previous=True,
            previous_messages=first,
            previous_messages_comment_ids=["c-1", "c-2"],
        )
        _, second = await _run(ticket2)
        assert second[: len(first)] == first
        appended = second[-1]
        assert "second reply" in appended["content"]
        assert "first reply" not in appended["content"]


class TestUserCommentsFilter:
    def test_literal_user_author_included(self):
        ticket = {
            "comments": [
                {"id": "1", "author": "user", "body": "a"},
                {"id": "2", "author": "benchmark-agent", "body": "b"},
                {"id": "3", "author": "orchestrator", "body": "c"},
                {"id": "4", "author": "system", "body": "d"},
                {"id": "5", "author": "fleet-coordinator", "body": "e"},
                {"id": "6", "author": "image-builder", "body": "f"},
            ]
        }
        assert [c["body"] for c in AgentBase._user_comments(ticket)] == ["a"]

    def test_multi_user_real_username_included(self):
        ticket = {
            "comments": [
                {"id": "1", "author": "alice", "body": "human"},
                {"id": "2", "author": "triage-agent", "body": "machine"},
            ]
        }
        assert [c["body"] for c in AgentBase._user_comments(ticket)] == ["human"]

    def test_empty_and_missing_authors_treated_as_machine(self):
        ticket = {
            "comments": [
                {"id": "1", "author": "", "body": "a"},
                {"id": "2", "body": "b"},
            ]
        }
        assert AgentBase._user_comments(ticket) == []
