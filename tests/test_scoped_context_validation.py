"""Tests for scoped_context key validation in the triage agent."""

from __future__ import annotations

from agents.triage.agent import _KNOWN_SCOPED_CONTEXT_KEYS


class TestKnownScopedContextKeys:
    def test_known_keys_present(self):
        assert "shared" in _KNOWN_SCOPED_CONTEXT_KEYS
        assert "resource" in _KNOWN_SCOPED_CONTEXT_KEYS
        assert "provision" in _KNOWN_SCOPED_CONTEXT_KEYS
        assert "benchmark" in _KNOWN_SCOPED_CONTEXT_KEYS
        assert "review" in _KNOWN_SCOPED_CONTEXT_KEYS

    def test_old_provisioning_key_rejected(self):
        assert "provisioning" not in _KNOWN_SCOPED_CONTEXT_KEYS

    def test_unknown_keys_rejected(self):
        assert "teardown" not in _KNOWN_SCOPED_CONTEXT_KEYS
        assert "foobar" not in _KNOWN_SCOPED_CONTEXT_KEYS
        assert "analyze" not in _KNOWN_SCOPED_CONTEXT_KEYS


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
