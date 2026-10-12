"""Tests for agents/benchmark/arcaflow_plugin_server.py."""

from __future__ import annotations

from agents.benchmark.arcaflow_plugin_server import (
    _validate_plugin_host,
    _validate_plugin_image,
)


class TestPluginImageValidation:
    def test_valid_image(self):
        ok, _ = _validate_plugin_image(
            "quay.io/arcalot/arcaflow-plugin-sysbench:latest"
        )
        assert ok

    def test_valid_image_with_digest(self):
        ok, _ = _validate_plugin_image(
            "quay.io/arcalot/arcaflow-plugin-fio@sha256:" + "a" * 64
        )
        assert ok

    def test_rejects_empty(self):
        ok, msg = _validate_plugin_image("")
        assert not ok
        assert "non-empty" in msg

    def test_rejects_none(self):
        ok, msg = _validate_plugin_image(None)  # type: ignore[arg-type]
        assert not ok

    def test_rejects_shell_injection(self):
        ok, msg = _validate_plugin_image(
            "quay.io/arcalot/arcaflow-plugin-fio:latest; rm -rf /"
        )
        assert not ok
        assert "quay.io/arcalot/arcaflow-plugin" in msg

    def test_rejects_non_arcalot_registry(self):
        ok, _ = _validate_plugin_image("docker.io/malicious/arcaflow-plugin-fio:latest")
        assert not ok

    def test_rejects_path_traversal(self):
        ok, _ = _validate_plugin_image(
            "quay.io/arcalot/../evil/arcaflow-plugin-fio:latest"
        )
        assert not ok


class TestPluginHostValidation:
    def test_rejects_empty(self):
        ok, msg = _validate_plugin_host("")
        assert not ok
        assert "No target" in msg

    def test_rejects_unassigned_host(self):
        ok, msg = _validate_plugin_host("10.0.0.99")
        assert not ok
        assert "not assigned" in msg
