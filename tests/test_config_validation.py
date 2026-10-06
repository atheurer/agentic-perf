"""Tests for config.json parse validation (fail-fast on syntax errors)."""

from __future__ import annotations

import textwrap
from unittest.mock import patch

import pytest

from orchestrator.config import _load_config_file


class TestConfigValidation:
    """Verify _load_config_file fails fast on invalid JSON."""

    def test_valid_json_loads(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"llm": {"model": "test"}}')
        with patch("orchestrator.config.CONFIG_PATH", config_file):
            result = _load_config_file()
        assert result == {"llm": {"model": "test"}}

    def test_missing_file_returns_empty(self, tmp_path):
        config_file = tmp_path / "nonexistent.json"
        with patch("orchestrator.config.CONFIG_PATH", config_file):
            result = _load_config_file()
        assert result == {}

    def test_invalid_json_raises_system_exit(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(
            textwrap.dedent("""\
            {
              "llm": {}
              "auth": {}
            }
        """)
        )
        with patch("orchestrator.config.CONFIG_PATH", config_file):
            with pytest.raises(SystemExit, match="FATAL.*invalid JSON"):
                _load_config_file()

    def test_invalid_json_includes_line_info(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(
            textwrap.dedent("""\
            {
              "llm": {}
              "auth": {}
            }
        """)
        )
        with patch("orchestrator.config.CONFIG_PATH", config_file):
            with pytest.raises(SystemExit, match=r"line \d+.*column \d+"):
                _load_config_file()

    def test_unreadable_file_raises_system_exit(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"valid": true}')
        with patch("orchestrator.config.CONFIG_PATH", config_file):
            with patch.object(
                type(config_file),
                "read_text",
                side_effect=OSError("Permission denied"),
            ):
                with pytest.raises(SystemExit, match="FATAL.*cannot read"):
                    _load_config_file()

    def test_empty_file_raises_system_exit(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text("")
        with patch("orchestrator.config.CONFIG_PATH", config_file):
            with pytest.raises(SystemExit, match="FATAL.*invalid JSON"):
                _load_config_file()
