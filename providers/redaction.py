from __future__ import annotations

import base64
import json
import logging
import re
from urllib.parse import quote as url_quote

logger = logging.getLogger(__name__)

_MIN_VALUE_LEN = 8

_DENYLIST = frozenset(
    {
        "root",
        "admin",
        "password",
        "changeme",
        "true",
        "false",
        "localhost",
        "default",
    }
)

_MAX_DEPTH = 20
_MAX_NODES = 10_000

_SENSITIVE_JSON_KEYS = frozenset(
    {
        "password",
        "secret",
        "token",
        "key",
        "credential",
        "api_key",
        "private_key",
        "access_key",
        "secret_key",
    }
)

DEFAULT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "bearer_token",
        re.compile(r"(Bearer\s+)\S+"),
    ),
    (
        "authorization_header",
        re.compile(r"(Authorization:\s*)\S+"),
    ),
    (
        "aws_access_key",
        re.compile(r"AKIA[A-Z0-9]{16}"),
    ),
    (
        "aws_secret_key",
        re.compile(r"(?<=AWS_SECRET_ACCESS_KEY[=:])\s*\S+"),
    ),
    (
        "password_cli_flag",
        re.compile(r"--password[= ]\S+"),
    ),
    (
        "env_sensitive_key",
        re.compile(
            r"-e\s+"
            r"(?:KUBEADMIN_PASSWORD|[A-Z_]*(?:SECRET|TOKEN|API_KEY))"
            r"[= ]\S+",
        ),
    ),
    (
        "pem_block",
        re.compile(r"-----BEGIN [A-Z ]+-----[\s\S]+?-----END [A-Z ]+-----"),
    ),
    (
        "inline_url_creds",
        re.compile(r"://[^@\s]+@"),
    ),
    (
        "pexpect_sendline",
        re.compile(r"(child\.sendline\()(['\"])[^'\"]+\2(\))"),
    ),
]


class _TicketRegistry:
    """Per-ticket container for registered secret values."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self._compiled: re.Pattern[str] | None = None

    def add(self, path: str, value: str) -> None:
        self.values[path] = value
        self._rebuild()

    def _rebuild(self) -> None:
        if not self.values:
            self._compiled = None
            return
        # Longest-first so longer values match before substrings
        escaped = sorted(
            (re.escape(v) for v in self.values.values()),
            key=len,
            reverse=True,
        )
        self._compiled = re.compile("|".join(escaped))

    @property
    def compiled(self) -> re.Pattern[str] | None:
        return self._compiled


class Redactor:
    """Per-ticket secret value and pattern redaction engine.

    Standalone — does not wire into EventBus or SecretsProvider.
    """

    def __init__(self) -> None:
        self._registries: dict[str, _TicketRegistry] = {}
        self._patterns: list[tuple[str, re.Pattern[str]]] = list(DEFAULT_PATTERNS)

    def register(
        self,
        ticket_id: str,
        path: str,
        value: str,
    ) -> None:
        """Register a secret value for redaction on a ticket.

        Also registers base64 and URL-encoded variants. Rejects
        values shorter than _MIN_VALUE_LEN or in _DENYLIST.
        If the value parses as JSON with sensitive keys, the
        individual field values are registered separately.
        """
        if len(value) < _MIN_VALUE_LEN:
            logger.debug(
                "Redactor: skipping short value for %s (len=%d)",
                path,
                len(value),
            )
            return

        if value.lower() in _DENYLIST:
            logger.debug(
                "Redactor: skipping denylisted value for %s",
                path,
            )
            return

        registry = self._registries.setdefault(ticket_id, _TicketRegistry())
        registry.add(path, value)

        b64_val = base64.b64encode(value.encode()).decode()
        if b64_val != value:
            registry.add(f"{path}#base64", b64_val)

        url_val = url_quote(value, safe="")
        if url_val != value:
            registry.add(f"{path}#urlenc", url_val)

        self._try_json_decompose(ticket_id, path, value)

    def _try_json_decompose(
        self,
        ticket_id: str,
        path: str,
        value: str,
    ) -> None:
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return

        if not isinstance(parsed, dict):
            return

        for key, inner in parsed.items():
            if (
                key.lower() in _SENSITIVE_JSON_KEYS
                and isinstance(inner, str)
                and len(inner) >= _MIN_VALUE_LEN
                and inner.lower() not in _DENYLIST
            ):
                registry = self._registries.setdefault(ticket_id, _TicketRegistry())
                registry.add(f"{path}.{key}", inner)

    def deregister_ticket(self, ticket_id: str) -> None:
        """Remove all registered values for a ticket."""
        self._registries.pop(ticket_id, None)

    def redact(self, ticket_id: str, data: dict) -> dict:
        """Recursively redact registered values and pattern matches.

        Fail-closed: on any exception, returns a safe error dict
        rather than leaking raw data.
        """
        try:
            return self._redact_recursive(ticket_id, data, depth=0, counter=[0])
        except Exception as exc:
            logger.error("Redaction failed for %s: %s", ticket_id, exc)
            return {
                "redaction_error": str(exc),
                "original_keys": (list(data.keys()) if isinstance(data, dict) else []),
            }

    def redact_string(self, ticket_id: str, text: str) -> str:
        """Apply value regex + pattern regexes to a single string."""
        registry = self._registries.get(ticket_id)
        if registry and registry.compiled:
            reverse_map = {v: k for k, v in registry.values.items()}

            def _value_replacer(m: re.Match[str]) -> str:
                matched = m.group(0)
                field = reverse_map.get(matched, "value")
                return f"[REDACTED:{field}]"

            text = registry.compiled.sub(_value_replacer, text)

        for name, pattern in self._patterns:
            text = pattern.sub(f"[REDACTED:pattern#{name}]", text)

        return text

    def _redact_recursive(
        self,
        ticket_id: str,
        data: object,
        depth: int,
        counter: list[int],
    ) -> object:
        counter[0] += 1
        if counter[0] > _MAX_NODES:
            return data
        if depth > _MAX_DEPTH:
            return data

        if isinstance(data, dict):
            return {
                k: self._redact_recursive(ticket_id, v, depth + 1, counter)
                for k, v in data.items()
            }

        if isinstance(data, list):
            return [
                self._redact_recursive(ticket_id, item, depth + 1, counter)
                for item in data
            ]

        if isinstance(data, str):
            return self.redact_string(ticket_id, data)

        return data
