"""Tests for context window truncation in AgentBase."""

from __future__ import annotations

from agents.base import AgentBase


class TestTruncateContext:
    """Test _truncate_context static method."""

    def _make_messages(self, count: int) -> list[dict]:
        """Build a message list with initial context + tool exchanges."""
        msgs = [{"role": "user", "content": "Initial ticket context"}]
        for i in range(count):
            msgs.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"tool_{i}",
                            "name": "read_file",
                            "input": {"path": f"file_{i}.json"},
                        }
                    ],
                }
            )
            msgs.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"tool_{i}",
                            "content": f"Result {i}" * 100,
                        }
                    ],
                }
            )
        return msgs

    def test_short_history_unchanged(self):
        """Messages below threshold are not truncated."""
        msgs = self._make_messages(2)  # 5 messages total
        result = AgentBase._truncate_context(msgs, keep_recent=6)
        assert len(result) == len(msgs)

    def test_long_history_truncated(self):
        """Long history is truncated to initial + note + recent."""
        msgs = self._make_messages(10)  # 21 messages total
        result = AgentBase._truncate_context(msgs, keep_recent=6)
        # initial (1) + truncation note (1) + recent (6) = 8
        assert len(result) == 8

    def test_initial_context_preserved(self):
        """First message (ticket context) is always kept."""
        msgs = self._make_messages(10)
        result = AgentBase._truncate_context(msgs, keep_recent=6)
        assert result[0]["content"] == "Initial ticket context"

    def test_truncation_note_inserted(self):
        """A system note explains what was truncated."""
        msgs = self._make_messages(10)
        result = AgentBase._truncate_context(msgs, keep_recent=6)
        note = result[1]
        assert note["role"] == "user"
        assert "truncated" in note["content"].lower()

    def test_recent_messages_preserved(self):
        """The most recent messages are kept intact."""
        msgs = self._make_messages(10)
        result = AgentBase._truncate_context(msgs, keep_recent=6)
        # Last 6 messages should match
        assert result[-6:] == msgs[-6:]

    def test_custom_keep_recent(self):
        """keep_recent parameter controls how many to keep."""
        msgs = self._make_messages(10)  # 21 messages
        result = AgentBase._truncate_context(msgs, keep_recent=4)
        # initial (1) + note (1) + recent (4) = 6
        assert len(result) == 6
        assert result[-4:] == msgs[-4:]

    def test_exact_threshold_not_truncated(self):
        """Messages at exactly keep_recent+1 are not truncated."""
        msgs = self._make_messages(3)  # 7 messages
        result = AgentBase._truncate_context(msgs, keep_recent=6)
        assert len(result) == len(msgs)

    def test_valid_alternation_after_truncation(self):
        """Truncated messages maintain valid user/assistant alternation."""
        msgs = self._make_messages(10)
        result = AgentBase._truncate_context(msgs, keep_recent=6)
        # message[0] = user (ticket context)
        # message[1] = user (truncation note)
        # message[2] should be assistant (tool_use)
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "user"
        assert result[2]["role"] == "assistant"

    def test_strips_leading_user_in_recent(self):
        """If recent starts with user, it's stripped for valid alternation."""
        msgs = self._make_messages(10)  # 21 messages
        # keep_recent=5 would grab: user(result) + assistant + user + assistant + user
        # The leading user should be stripped
        result = AgentBase._truncate_context(msgs, keep_recent=5)
        # After initial + note, first recent should be assistant
        assert result[2]["role"] == "assistant"
