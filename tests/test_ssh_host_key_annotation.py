"""The SSH host-key error annotation (lab-host robustness #1233).

Fresh lab allocations present new host keys; every SSH step fails closed.
The annotation turns the raw refused-connection noise into an operator
recipe that names the offending known_hosts entry and warns that BOTH the
hostname and the resolved-IP entries need refreshing.
"""

from __future__ import annotations

import paths  # noqa: F401
from providers.ssh import annotate_host_key_error

_CHANGED = (
    "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
    "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n"
    "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
    "IT IS POSSIBLE THAT SOMEONE IS DOING SOMETHING NASTY!\n"
    "Someone could be eavesdropping on you right now (man-in-the-middle attack)!\n"
    "It is also possible that a host key has just been changed.\n"
    "The fingerprint for the ED25519 key sent by the remote host is\n"
    "SHA256:yXUHy9RKvsyIx5YJ6TOc9BfADSnq22yTQtpSMqkcZ7c.\n"
    "Add correct host key in /root/.ssh/known_hosts to get rid of this message.\n"
    "Offending ED25519 key in /root/.ssh/known_hosts:61\n"
    "Host key for 10.6.60.88 has changed and you have requested strict checking.\n"
    "Host key verification failed.\n"
)


def test_unrelated_stderr_passes_through_unchanged():
    stderr = "Connection refused"
    assert annotate_host_key_error(stderr) == stderr


def test_annotation_names_the_offending_entry_and_both_forms():
    annotated = annotate_host_key_error(_CHANGED)
    assert "Offending ED25519 key in /root/.ssh/known_hosts:61" in annotated
    assert "ssh-keygen -R /root/.ssh/known_hosts" in annotated
    assert "resolved IP" in annotated
    assert "ssh-keyscan" in annotated


def test_annotation_drops_the_banner_noise():
    annotated = annotate_host_key_error(_CHANGED)
    assert "REMOTE HOST IDENTIFICATION" not in annotated
    assert set(annotated.splitlines()[0].strip()) != {"@"}
    # Compact enough for a tool-result error field.
    assert len(annotated) < 800


def test_annotation_without_offending_line_still_gives_the_recipe():
    stderr = (
        "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\n"
        "Host key verification failed.\n"
    )
    annotated = annotate_host_key_error(stderr)
    assert "ssh-keygen -R <host-or-ip>" in annotated
    assert "ssh-keyscan" in annotated


def test_offending_entry_is_named_verbatim():
    stderr = _CHANGED.replace(
        "Offending ED25519 key in /root/.ssh/known_hosts:61",
        "Offending ED25519 key in /root/.ssh/known_hosts:12",
    )
    annotated = annotate_host_key_error(stderr)
    # The recipe references the known_hosts file from the offending line.
    assert "ssh-keygen -R /root/.ssh/known_hosts" in annotated
    assert "Offending ED25519 key in /root/.ssh/known_hosts:12" in annotated
