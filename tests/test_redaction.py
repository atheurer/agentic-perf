from __future__ import annotations

import base64
import json
import time
from unittest.mock import patch
from urllib.parse import quote as url_quote

import pytest

from providers.redaction import (
    _DENYLIST,
    _MAX_DEPTH,
    _MAX_NODES,
    _MIN_VALUE_LEN,
    DEFAULT_PATTERNS,
    Redactor,
)

# ── helpers ────────────────────────────────────────────────────


@pytest.fixture
def redactor() -> Redactor:
    return Redactor()


TICKET = "T-1234"
SECRET = "s3cret-p@ssw0rd!"
SECRET_PATH = "quads/config.json#password"


# ── 1. Registration ───────────────────────────────────────────


class TestRegistration:
    def test_register_adds_to_compiled_regex(self, redactor: Redactor) -> None:
        redactor.register(TICKET, SECRET_PATH, SECRET)
        registry = redactor._registries[TICKET]
        assert registry.compiled is not None
        assert registry.compiled.search(SECRET)

    def test_register_creates_base64_variant(self, redactor: Redactor) -> None:
        redactor.register(TICKET, SECRET_PATH, SECRET)
        registry = redactor._registries[TICKET]
        b64 = base64.b64encode(SECRET.encode()).decode()
        assert f"{SECRET_PATH}#base64" in registry.values
        assert registry.values[f"{SECRET_PATH}#base64"] == b64

    def test_register_creates_urlenc_variant(self, redactor: Redactor) -> None:
        redactor.register(TICKET, SECRET_PATH, SECRET)
        registry = redactor._registries[TICKET]
        urlenc = url_quote(SECRET, safe="")
        assert f"{SECRET_PATH}#urlenc" in registry.values
        assert registry.values[f"{SECRET_PATH}#urlenc"] == urlenc

    def test_rejects_short_values(self, redactor: Redactor) -> None:
        redactor.register(TICKET, "path", "short")
        assert TICKET not in redactor._registries

    def test_rejects_denylisted_values(self, redactor: Redactor) -> None:
        long_enough = [w for w in _DENYLIST if len(w) >= _MIN_VALUE_LEN]
        assert len(long_enough) > 0, "Need at least one testable word"
        for word in long_enough:
            redactor.register(TICKET, "path", word)
        assert TICKET not in redactor._registries

    def test_min_length_boundary(self, redactor: Redactor) -> None:
        exactly_min = "a" * _MIN_VALUE_LEN
        redactor.register(TICKET, "path", exactly_min)
        assert TICKET in redactor._registries

    def test_denylist_is_case_insensitive(self, redactor: Redactor) -> None:
        redactor.register(TICKET, "path", "CHANGEME")
        assert TICKET not in redactor._registries


# ── 2. Redaction — values ────────────────────────────────────


class TestValueRedaction:
    def test_registered_value_replaced(self, redactor: Redactor) -> None:
        redactor.register(TICKET, SECRET_PATH, SECRET)
        text = f"connecting with password {SECRET} to host"
        result = redactor.redact_string(TICKET, text)
        assert SECRET not in result
        assert "[REDACTED:" in result

    def test_longer_value_matched_first(self, redactor: Redactor) -> None:
        short = "abcdefgh"
        longer = "abcdefghijkl"
        redactor.register(TICKET, "short", short)
        redactor.register(TICKET, "longer", longer)
        text = f"value is {longer}"
        result = redactor.redact_string(TICKET, text)
        assert "[REDACTED:longer]" in result

    def test_base64_variant_redacted(self, redactor: Redactor) -> None:
        redactor.register(TICKET, SECRET_PATH, SECRET)
        b64 = base64.b64encode(SECRET.encode()).decode()
        text = f"encoded: {b64}"
        result = redactor.redact_string(TICKET, text)
        assert b64 not in result

    def test_urlenc_variant_redacted(self, redactor: Redactor) -> None:
        redactor.register(TICKET, SECRET_PATH, SECRET)
        urlenc = url_quote(SECRET, safe="")
        text = f"url param: key={urlenc}"
        result = redactor.redact_string(TICKET, text)
        assert urlenc not in result

    def test_unregistered_ticket_values_unchanged(self, redactor: Redactor) -> None:
        """Value redaction is ticket-scoped; patterns still fire globally."""
        redactor.register(TICKET, SECRET_PATH, SECRET)
        text = f"password {SECRET}"
        result = redactor.redact_string("OTHER-TICKET", text)
        assert result == text

    def test_patterns_fire_for_unregistered_ticket(self, redactor: Redactor) -> None:
        text = "Bearer eyJtoken123.abc.def"
        result = redactor.redact_string("NO-SUCH-TICKET", text)
        assert "eyJtoken123" not in result

    def test_regex_metacharacters_in_value(self, redactor: Redactor) -> None:
        meta_val = "sec.ret+val*ue??"
        redactor.register(TICKET, "path", meta_val)
        text = f"value is {meta_val}"
        result = redactor.redact_string(TICKET, text)
        assert meta_val not in result


# ── 3. Redaction — patterns ──────────────────────────────────


class TestPatternRedaction:
    def test_bearer_token(self, redactor: Redactor) -> None:
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def"
        result = redactor.redact_string(TICKET, text)
        assert "eyJ" not in result
        assert "[REDACTED:pattern#" in result

    def test_aws_access_key(self, redactor: Redactor) -> None:
        text = "key = AKIAIOSFODNN7EXAMPLE"
        result = redactor.redact_string(TICKET, text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result

    def test_aws_secret_key(self, redactor: Redactor) -> None:
        text = "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCY"
        result = redactor.redact_string(TICKET, text)
        assert "wJalrXUtnFEMI" not in result

    def test_password_cli_flag(self, redactor: Redactor) -> None:
        text = "cmd --password=SuperSecret123"
        result = redactor.redact_string(TICKET, text)
        assert "SuperSecret123" not in result

    def test_env_sensitive_key(self, redactor: Redactor) -> None:
        text = "podman run -e KUBEADMIN_PASSWORD=abc123xyz"
        result = redactor.redact_string(TICKET, text)
        assert "abc123xyz" not in result

    def test_pem_block(self, redactor: Redactor) -> None:
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIBogIBAAJBALR4M+fLN...\n"
            "-----END RSA PRIVATE KEY-----"
        )
        result = redactor.redact_string(TICKET, pem)
        assert "MIIBogIBAAJBALR4M" not in result

    def test_inline_url_creds(self, redactor: Redactor) -> None:
        text = "postgres://admin:p4ssw0rd@db.internal:5432/mydb"
        result = redactor.redact_string(TICKET, text)
        assert "admin:p4ssw0rd@" not in result

    def test_pexpect_sendline(self, redactor: Redactor) -> None:
        text = "child.sendline('my-root-password')"
        result = redactor.redact_string(TICKET, text)
        assert "my-root-password" not in result

    def test_bearer_prefix_preserved(self, redactor: Redactor) -> None:
        """Pattern replacement keeps structural prefix for debuggability."""
        text = "header Bearer eyJsecrettoken"
        result = redactor.redact_string(TICKET, text)
        assert "Bearer " in result
        assert "eyJsecrettoken" not in result

    def test_env_key_prefix_preserved(self, redactor: Redactor) -> None:
        text = "podman run -e KUBEADMIN_PASSWORD=abc123xyz"
        result = redactor.redact_string(TICKET, text)
        assert "-e KUBEADMIN_PASSWORD=" in result
        assert "abc123xyz" not in result

    def test_all_default_patterns_have_names(self) -> None:
        for name, pattern in DEFAULT_PATTERNS:
            assert name, "Pattern name must be non-empty"
            assert pattern.pattern, "Pattern regex must be non-empty"


# ── 4. Redaction — recursive ─────────────────────────────────


class TestRecursiveRedaction:
    def test_nested_dict(self, redactor: Redactor) -> None:
        redactor.register(TICKET, SECRET_PATH, SECRET)
        data = {
            "outer": {
                "inner": f"password is {SECRET}",
            },
        }
        result = redactor.redact(TICKET, data)
        assert SECRET not in json.dumps(result)

    def test_list_of_strings(self, redactor: Redactor) -> None:
        redactor.register(TICKET, SECRET_PATH, SECRET)
        data = {
            "commands": [
                f"echo {SECRET}",
                "ls -la",
                f"connect {SECRET}",
            ],
        }
        result = redactor.redact(TICKET, data)
        serialized = json.dumps(result)
        assert SECRET not in serialized
        assert "ls -la" in serialized

    def test_mixed_types_pass_through(self, redactor: Redactor) -> None:
        redactor.register(TICKET, SECRET_PATH, SECRET)
        data = {
            "count": 42,
            "enabled": True,
            "nothing": None,
            "ratio": 3.14,
            "secret_field": SECRET,
        }
        result = redactor.redact(TICKET, data)
        assert result["count"] == 42
        assert result["enabled"] is True
        assert result["nothing"] is None
        assert result["ratio"] == 3.14
        assert SECRET not in result["secret_field"]

    def test_secret_in_dict_key(self, redactor: Redactor) -> None:
        """Secrets appearing as dict keys are redacted too."""
        redactor.register(TICKET, SECRET_PATH, SECRET)
        data = {SECRET: "some value", "safe_key": "safe_value"}
        result = redactor.redact(TICKET, data)
        serialized = json.dumps(result)
        assert SECRET not in serialized
        assert "safe_key" in serialized

    def test_content_blocks(self, redactor: Redactor) -> None:
        """LLM responses use content-block lists with text fields."""
        redactor.register(TICKET, SECRET_PATH, SECRET)
        data = {
            "raw_content": [
                {
                    "type": "text",
                    "text": f"Found credential: {SECRET}",
                },
                {
                    "type": "tool_use",
                    "input": {"password": SECRET},
                },
            ],
        }
        result = redactor.redact(TICKET, data)
        assert SECRET not in json.dumps(result)


# ── 5. Fail-closed ───────────────────────────────────────────


class TestFailClosed:
    def test_exception_returns_safe_error(self, redactor: Redactor) -> None:
        redactor.register(TICKET, SECRET_PATH, SECRET)
        data = {"key": SECRET, "other": "visible"}
        with patch.object(
            redactor,
            "_redact_recursive",
            side_effect=RuntimeError(f"contains {SECRET}"),
        ):
            result = redactor.redact(TICKET, data)
        assert "redaction_error" in result
        assert result["redaction_error"] == "RuntimeError"
        assert SECRET not in result["redaction_error"]
        assert result["original_keys"] == ["key", "other"]
        assert SECRET not in json.dumps(result)

    def test_fail_closed_with_non_dict(self, redactor: Redactor) -> None:
        with patch.object(
            redactor,
            "_redact_recursive",
            side_effect=RuntimeError("oops"),
        ):
            result = redactor.redact(TICKET, "not a dict")
        assert "redaction_error" in result
        assert result["original_keys"] == []

    def test_redact_string_fail_closed(self, redactor: Redactor) -> None:
        """Direct redact_string() callers get fail-closed too."""
        redactor.register(TICKET, SECRET_PATH, SECRET)
        with patch.object(
            redactor,
            "_redact_string_inner",
            side_effect=RuntimeError("regex blow up"),
        ):
            result = redactor.redact_string(TICKET, f"secret: {SECRET}")
        assert result == "[REDACTED:redaction_error]"
        assert SECRET not in result


# ── 6. Deregister ────────────────────────────────────────────


class TestDeregister:
    def test_values_no_longer_redacted_after_deregister(
        self, redactor: Redactor
    ) -> None:
        redactor.register(TICKET, SECRET_PATH, SECRET)
        assert SECRET not in redactor.redact_string(TICKET, SECRET)

        redactor.deregister_ticket(TICKET)
        assert redactor.redact_string(TICKET, SECRET) == SECRET

    def test_deregister_nonexistent_ticket_no_error(self, redactor: Redactor) -> None:
        redactor.deregister_ticket("DOES-NOT-EXIST")


# ── 7. JSON decomposition ───────────────────────────────────


class TestJSONDecomposition:
    def test_json_blob_password_extracted(self, redactor: Redactor) -> None:
        blob = json.dumps(
            {
                "username": "admin",
                "password": "vault-secret-value-12345",
            }
        )
        redactor.register(TICKET, "vault/creds", blob)
        text = "user typed vault-secret-value-12345 in terminal"
        result = redactor.redact_string(TICKET, text)
        assert "vault-secret-value-12345" not in result

    def test_json_blob_multiple_sensitive_keys(self, redactor: Redactor) -> None:
        blob = json.dumps(
            {
                "api_key": "key-abcdefghijkl",
                "secret": "sec-mnopqrstuvwx",
                "host": "db.internal",
            }
        )
        redactor.register(TICKET, "vault/multi", blob)
        text = "api=key-abcdefghijkl secret=sec-mnopqrstuvwx"
        result = redactor.redact_string(TICKET, text)
        assert "key-abcdefghijkl" not in result
        assert "sec-mnopqrstuvwx" not in result

    def test_json_decompose_skips_short_inner_values(self, redactor: Redactor) -> None:
        blob = json.dumps({"password": "short"})
        redactor.register(TICKET, "vault/short", blob)
        text = "value short appears"
        result = redactor.redact_string(TICKET, text)
        assert "short" in result

    def test_json_decomposed_values_have_base64_variants(
        self, redactor: Redactor
    ) -> None:
        inner_secret = "vault-secret-value-12345"
        blob = json.dumps({"password": inner_secret})
        redactor.register(TICKET, "vault/creds", blob)
        b64_inner = base64.b64encode(inner_secret.encode()).decode()
        text = f"encoded: {b64_inner}"
        result = redactor.redact_string(TICKET, text)
        assert b64_inner not in result

    def test_non_json_value_not_decomposed(self, redactor: Redactor) -> None:
        redactor.register(TICKET, "path", "not-json-at-all")
        assert TICKET in redactor._registries
        assert len(redactor._registries[TICKET].values) > 0


# ── 8. Performance ───────────────────────────────────────────


class TestPerformance:
    def test_100kb_payload_under_50ms(self, redactor: Redactor) -> None:
        for i in range(20):
            val = f"secret-value-{i:04d}-{'x' * 20}"
            redactor.register(TICKET, f"path/{i}", val)

        payload_str = "a" * 50_000
        for i in range(20):
            val = f"secret-value-{i:04d}-{'x' * 20}"
            payload_str += f" {val} "
        payload_str += "b" * (100_000 - len(payload_str))

        data = {
            "output": payload_str,
            "nested": {
                "field": payload_str[:10_000],
            },
        }

        # Warm up regex compilation
        redactor.redact(TICKET, {"warmup": "test"})

        iterations = 10
        start = time.perf_counter()
        for _ in range(iterations):
            result = redactor.redact(TICKET, data)
        elapsed = (time.perf_counter() - start) / iterations

        assert elapsed < 0.05, (
            f"Redaction took {elapsed * 1000:.1f}ms, expected <50ms per call"
        )

        serialized = json.dumps(result)
        for i in range(20):
            val = f"secret-value-{i:04d}-{'x' * 20}"
            assert val not in serialized


# ── 9. Depth / node caps ─────────────────────────────────────


class TestDepthNodeCaps:
    def test_deeply_nested_secrets_not_leaked(self, redactor: Redactor) -> None:
        """Secrets past depth cap are replaced with cap markers, not leaked."""
        redactor.register(TICKET, SECRET_PATH, SECRET)
        data: dict = {"leaf": SECRET}
        for _ in range(_MAX_DEPTH + 10):
            data = {"nested": data}

        result = redactor.redact(TICKET, data)
        assert isinstance(result, dict)
        serialized = json.dumps(result)
        assert SECRET not in serialized
        assert "depth_limit_exceeded" in serialized

    def test_wide_structure_secrets_not_leaked(self, redactor: Redactor) -> None:
        """Secrets past node cap are replaced with cap markers, not leaked."""
        redactor.register(TICKET, SECRET_PATH, SECRET)
        data = {
            "items": [SECRET] * (_MAX_NODES + 100),
        }

        start = time.perf_counter()
        result = redactor.redact(TICKET, data)
        elapsed = time.perf_counter() - start

        assert elapsed < 5.0, f"Wide-structure redaction took {elapsed:.1f}s"
        assert isinstance(result, dict)
        serialized = json.dumps(result)
        assert SECRET not in serialized
