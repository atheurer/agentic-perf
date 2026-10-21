from __future__ import annotations

import pytest

from providers.skills.runtime_config import validate_runtime_config


def test_quads_runtime_config_accepts_shared_connection_defaults() -> None:
    validate_runtime_config(
        "resource/quads",
        {
            "connection": {
                "api_host": "quads.example.test",
                "api_scheme": "https",
            }
        },
    )


@pytest.mark.parametrize("scheme", ["ftp", "HTTPS", ""])
def test_quads_runtime_config_rejects_unsupported_scheme(scheme: str) -> None:
    with pytest.raises(ValueError, match="scheme must be http or https"):
        validate_runtime_config(
            "resource/quads", {"connection": {"api_scheme": scheme}}
        )


def test_quads_runtime_config_rejects_invalid_connection_shape() -> None:
    with pytest.raises(ValueError, match="connection must be an object"):
        validate_runtime_config("resource/quads", {"connection": []})


def test_quads_runtime_config_rejects_empty_host() -> None:
    with pytest.raises(ValueError, match="host must not be empty"):
        validate_runtime_config("resource/quads", {"connection": {"api_host": "  "}})
