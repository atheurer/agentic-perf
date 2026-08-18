"""Tests for scoped_context key validation in the triage agent."""

from __future__ import annotations

from agents.triage.agent import (
    _KNOWN_SCOPED_CONTEXT_KEYS,
    _discover_scoped_context_keys,
)


class TestKnownScopedContextKeys:
    def test_discovery_finds_agent_keys(self):
        # Verify discovery actually ran and populated the set from agent files.
        # If the glob path is wrong, this would be {"shared"} only.
        assert len(_KNOWN_SCOPED_CONTEXT_KEYS) > 1, (
            "Discovery found no agent keys — check agents/*/agent.py glob"
        )

    def test_known_keys_present(self):
        # These agents call _get_scoped_context, so discovery must find them.
        assert "shared" in _KNOWN_SCOPED_CONTEXT_KEYS
        assert "resource" in _KNOWN_SCOPED_CONTEXT_KEYS
        assert "provision" in _KNOWN_SCOPED_CONTEXT_KEYS
        assert "benchmark" in _KNOWN_SCOPED_CONTEXT_KEYS
        assert "review" in _KNOWN_SCOPED_CONTEXT_KEYS

    def test_old_provisioning_key_rejected(self):
        # "provisioning" was the pre-rename key — no agent uses it now.
        assert "provisioning" not in _KNOWN_SCOPED_CONTEXT_KEYS

    def test_non_participating_agents_absent(self):
        # These agents exist but don't call _get_scoped_context.
        assert "teardown" not in _KNOWN_SCOPED_CONTEXT_KEYS
        assert "analyze" not in _KNOWN_SCOPED_CONTEXT_KEYS
        assert "foobar" not in _KNOWN_SCOPED_CONTEXT_KEYS

    def test_discovery_is_deterministic(self):
        # Running discovery twice produces the same result.
        assert _discover_scoped_context_keys() == _KNOWN_SCOPED_CONTEXT_KEYS


class TestScopedContextFiltering:
    def _filter(self, raw: dict) -> dict:
        return {k: v for k, v in raw.items() if k in _KNOWN_SCOPED_CONTEXT_KEYS}

    def test_all_known_keys_pass_through(self):
        raw = {
            "shared": "env",
            "resource": "hosts",
            "provision": "install",
            "benchmark": "run",
            "review": "analyze",
        }
        assert self._filter(raw) == raw

    def test_unknown_key_dropped(self):
        raw = {"shared": "env", "provision": "install", "foobar": "dropped"}
        filtered = self._filter(raw)
        assert "foobar" not in filtered
        assert filtered == {"shared": "env", "provision": "install"}

    def test_old_provisioning_key_dropped(self):
        raw = {"provisioning": "old key"}
        assert self._filter(raw) == {}

    def test_empty_dict_stays_empty(self):
        assert self._filter({}) == {}

    def test_only_unknown_keys_yields_empty(self):
        raw = {"teardown": "x", "foobar": "y"}
        assert self._filter(raw) == {}
