"""Tests for format_directive_schema_for_prompt()."""

from providers.directives import format_directive_schema_for_prompt


def test_described_key_includes_description():
    """A key with a registered description appears as '- **key**: description'."""
    result = format_directive_schema_for_prompt({"harness"})
    assert "- **harness**: " in result
    # The description text should follow the colon
    assert "- **harness**:" in result
    assert "- **harness**\n" not in result  # must NOT be bare


def test_undescribed_key_appears_without_description():
    """A key with NO description appears as '- **key**' (no colon/text)."""
    result = format_directive_schema_for_prompt({"totally_custom_key"})
    assert "- **totally_custom_key**" in result
    assert "- **totally_custom_key**:" not in result


def test_empty_keys_returns_empty_string():
    """An empty key set produces an empty string."""
    assert format_directive_schema_for_prompt(set()) == ""


def test_output_contains_canonical_key_guidance():
    """The output includes the instruction to use only canonical keys."""
    result = format_directive_schema_for_prompt({"harness"})
    assert "Use ONLY these canonical keys" in result


def test_mixed_described_and_undescribed():
    """Described and undescribed keys coexist correctly."""
    result = format_directive_schema_for_prompt({"harness", "unknown_directive"})
    # harness is described
    assert "- **harness**: " in result
    # unknown_directive is not
    assert "- **unknown_directive**" in result
    assert "- **unknown_directive**:" not in result
