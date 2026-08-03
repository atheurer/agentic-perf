from __future__ import annotations

from agents.review.agent import _is_approved


def test_is_approved_standard_inputs() -> None:
    # Exact matches
    assert _is_approved("done") is True
    assert _is_approved("submit") is True
    assert _is_approved("submit the review") is True
    assert _is_approved("that's enough") is True
    assert _is_approved("wrap it up") is True


def test_is_approved_slash_commands() -> None:
    # Slash commands with and without leading slashes and whitespace
    assert _is_approved("/submit") is True
    assert _is_approved("/done") is True
    assert _is_approved("  /submit  ") is True
    assert _is_approved("\n/done") is True
    assert _is_approved("/submit the review") is True


def test_is_approved_startswith_done() -> None:
    # Starts with done or /done
    assert _is_approved("done with everything") is True
    assert _is_approved("/done with everything") is True
    assert _is_approved("done, thanks") is True
    assert _is_approved("/done, thanks") is True


def test_is_approved_negative_cases() -> None:
    # Negative cases
    assert _is_approved("don") is False
    assert _is_approved("not done") is False
    assert _is_approved("no") is False
    assert _is_approved("submit_review") is False
    assert _is_approved("") is False
    assert _is_approved("   ") is False
