"""Changed-host-key errors give tool-oriented guidance."""

from __future__ import annotations

import paths  # noqa: F401
from providers.ssh import annotate_host_key_error


def test_unrelated_stderr_passes_through():
    assert annotate_host_key_error("Connection refused") == "Connection refused"


def test_changed_key_identifies_host_without_shell_recipe():
    result = annotate_host_key_error(
        "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\n"
        "SHA256:yXUHy9RKvsyIx5YJ6TOc9BfADSnq22yTQtpSMqkcZ7c\n"
        "Host key for test.example has changed and you have requested strict checking.\n"
        "Offending ED25519 key in /root/.ssh/known_hosts:61\n"
    )
    assert "test.example" in result
    assert "SHA256:yXUHy9RKvsyIx5YJ6TOc9BfADSnq22yTQtpSMqkcZ7c" in result
    assert "host-key replacement tool" in result
    assert "ssh-keygen" not in result
    assert "ssh-keyscan" not in result
    assert "independently confirmed" not in result


def test_changed_key_without_host_does_not_guess_a_target():
    result = annotate_host_key_error("REMOTE HOST IDENTIFICATION HAS CHANGED")
    assert "the target host" in result
