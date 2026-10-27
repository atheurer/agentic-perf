"""The SSH host-key error annotation (lab-host robustness #1233).

Fresh lab allocations present new host keys; every SSH step fails closed.
The annotation turns the refusal into an operator recipe that targets the
changed host and explains how to refresh both address forms safely.
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


def test_annotation_targets_changed_host_and_other_form():
    annotated = annotate_host_key_error(_CHANGED)
    assert "ssh-keygen -R 10.6.60.88" in annotated
    assert "ssh-keygen -R REPLACE_WITH_OTHER_HOSTNAME_OR_IP" in annotated
    assert "ssh-keygen -R /root/.ssh/known_hosts" not in annotated
    assert "ssh-keyscan" in annotated


def test_annotation_drops_the_banner_noise():
    annotated = annotate_host_key_error(_CHANGED)
    assert "REMOTE HOST IDENTIFICATION" not in annotated
    assert set(annotated.splitlines()[0].strip()) != {"@"}
    # Compact enough for a tool-result error field.
    assert len(annotated) < 500


def test_annotation_without_offending_line_still_gives_the_recipe():
    stderr = (
        "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\n"
        "Host key verification failed.\n"
    )
    annotated = annotate_host_key_error(stderr)
    assert "ssh-keygen -R REPLACE_WITH_HOST_OR_IP" in annotated
    assert "ssh-keyscan" in annotated


def test_fingerprint_is_checked_before_scanned_keys_are_trusted():
    annotated = annotate_host_key_error(_CHANGED)
    scan = annotated.index("ssh-keyscan")
    fingerprint = annotated.index("ssh-keygen -lf")
    verification = annotated.index("Compare to an independently confirmed")
    append = annotated.index('cat "$candidate_keys" >> ~/.ssh/known_hosts')

    assert scan < fingerprint < verification < append
    assert "untrusted scan results" in annotated


def test_removal_target_comes_from_the_changed_host_not_known_hosts_path():
    stderr = _CHANGED.replace(
        "Offending ED25519 key in /root/.ssh/known_hosts:61",
        "Offending ED25519 key in /root/.ssh/known_hosts:12",
    )
    annotated = annotate_host_key_error(stderr)
    assert "ssh-keygen -R 10.6.60.88" in annotated
    assert "ssh-keygen -R /root/.ssh/known_hosts" not in annotated


def test_changed_host_is_shell_quoted_before_use_in_recipe():
    stderr = _CHANGED.replace(
        "Host key for 10.6.60.88 has changed",
        "Host key for bad;touch has changed",
    )
    annotated = annotate_host_key_error(stderr)
    assert "ssh-keygen -R 'bad;touch'" in annotated
    assert "ssh-keygen -R bad;touch" not in annotated
