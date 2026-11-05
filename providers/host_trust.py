"""Targeted controller host-key inspection and replacement."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
import shlex
import struct
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HostKey:
    """A scanned public host key and its OpenSSH fingerprint."""

    algorithm: str
    encoded: str
    fingerprint: str


def validate_host(host: str) -> str:
    """Accept a literal hostname or IP, never a shell fragment or option."""
    if not isinstance(host, str) or not host or host != host.strip():
        raise ValueError("host must be a hostname or IP address")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError("host must be a hostname or IP address")
    if any(
        not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
        for label in host.rstrip(".").split(".")
    ):
        raise ValueError("host must be a hostname or IP address")
    return host


def parse_host_keys(output: str) -> list[HostKey]:
    """Parse and fingerprint unique, well-formed SSH public host keys."""
    keys: dict[str, HostKey] = {}
    for line in output.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3 or parts[1] not in {
            "ssh-ed25519",
            "ssh-rsa",
            "ecdsa-sha2-nistp256",
            "ecdsa-sha2-nistp384",
            "ecdsa-sha2-nistp521",
        }:
            continue
        try:
            raw = base64.b64decode(parts[2], validate=True)
            fields = []
            offset = 0
            while offset < len(raw):
                length = struct.unpack(">I", raw[offset : offset + 4])[0]
                offset += 4
                if not length or offset + length > len(raw):
                    raise ValueError("invalid key field")
                fields.append(raw[offset : offset + length])
                offset += length
            algorithm = parts[1]
            if not fields or fields[0] != algorithm.encode():
                continue
            if algorithm == "ssh-ed25519" and (
                len(fields) != 2 or len(fields[1]) != 32
            ):
                continue
            if algorithm == "ssh-rsa" and len(fields) != 3:
                continue
            if algorithm.startswith("ecdsa-") and (
                len(fields) != 3
                or fields[1] != algorithm.removeprefix("ecdsa-sha2-").encode()
            ):
                continue
        except (ValueError, IndexError, struct.error):
            continue
        fingerprint = "SHA256:" + base64.b64encode(
            hashlib.sha256(raw).digest()
        ).decode().rstrip("=")
        keys[parts[2]] = HostKey(algorithm, parts[2], fingerprint)
    return list(keys.values())


def preferred_host_key(output: str) -> HostKey | None:
    """Choose one supported algorithm; reject multiple keys for that algorithm."""
    keys = parse_host_keys(output)
    for algorithm in (
        "ssh-ed25519",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "ssh-rsa",
    ):
        candidates = [key for key in keys if key.algorithm == algorithm]
        if candidates:
            return candidates[0] if len(candidates) == 1 else None
    return None


async def inspect_controller_host_key(
    ssh: Any, controller: str, target: str, observed_fingerprint: str | None = None
) -> dict[str, Any]:
    """Inspect the controller's stored entry and the key currently presented."""
    validate_host(controller)
    validate_host(target)
    if observed_fingerprint is not None and not re.fullmatch(
        r"SHA256:[A-Za-z0-9+/]{43}", observed_fingerprint
    ):
        return {"status": "rejected", "message": "Invalid observed fingerprint."}
    quoted = shlex.quote(target)
    existing = await ssh.run(
        controller,
        "if [ -f /root/.ssh/known_hosts ]; then "
        f"ssh-keygen -F {quoted} -f /root/.ssh/known_hosts; else exit 1; fi",
        timeout=15,
        mutating=False,
    )
    if existing.exit_code not in (0, 1):
        return {
            "status": "error",
            "message": "Could not inspect controller host records.",
        }
    if existing.exit_code == 1 or not existing.stdout.strip():
        return {
            "status": "not_found",
            "message": "No stored entry for this host; use normal first-contact handling.",
        }
    scanned = await ssh.run(
        controller,
        f"ssh-keyscan -T 5 -t ed25519,ecdsa,rsa {quoted}",
        timeout=20,
        mutating=False,
    )
    scanned_keys = parse_host_keys(scanned.stdout)
    if observed_fingerprint:
        matches = [
            key for key in scanned_keys if key.fingerprint == observed_fingerprint
        ]
        candidate = matches[0] if len(matches) == 1 else None
        if (
            candidate
            and sum(key.algorithm == candidate.algorithm for key in scanned_keys) != 1
        ):
            candidate = None
    else:
        candidate = preferred_host_key(scanned.stdout)
    if scanned.exit_code != 0 or candidate is None:
        return {
            "status": "error",
            "message": "Could not obtain one unambiguous supported host key from the target.",
        }
    existing_keys = parse_host_keys(existing.stdout)
    if candidate in existing_keys:
        return {"status": "already_trusted", "fingerprint": candidate.fingerprint}
    return {
        "status": "changed",
        "algorithm": candidate.algorithm,
        "fingerprint": candidate.fingerprint,
        "key": candidate.encoded,
    }


async def replace_controller_host_key(
    ssh: Any, controller: str, target: str, approved_key: HostKey
) -> dict[str, Any]:
    """Replace only target's controller record after checking the presented key again."""
    validate_host(controller)
    validate_host(target)
    scanned = await ssh.run(
        controller,
        f"ssh-keyscan -T 5 -t ed25519,ecdsa,rsa {shlex.quote(target)}",
        timeout=20,
        mutating=False,
    )
    keys = parse_host_keys(scanned.stdout)
    same_algorithm = [key for key in keys if key.algorithm == approved_key.algorithm]
    if scanned.exit_code != 0 or same_algorithm != [approved_key]:
        return {
            "status": "rejected",
            "message": "The presented host key changed since approval; inspect it again.",
        }
    entry = shlex.quote(f"{target} {approved_key.algorithm} {approved_key.encoded}")
    target_arg = shlex.quote(target)
    # Lock cooperating writers and replace atomically, preserving other hosts.
    command = (
        "( "
        "flock -x 9 || exit 1; "
        "[ ! -L /root/.ssh/known_hosts ] || exit 1; "
        "tmp=$(mktemp /root/.ssh/.known-hosts.XXXXXX) || exit 1; "
        'trap \'rm -f "$tmp" "$tmp.old"\' EXIT; '
        "if [ -f /root/.ssh/known_hosts ]; then "
        'cp /root/.ssh/known_hosts "$tmp" || exit 1; '
        f'ssh-keygen -R {target_arg} -f "$tmp" >/dev/null || exit 1; fi; '
        f"printf '%s\\n' {entry} >> \"$tmp\" && "
        'chmod 600 "$tmp" && mv -f "$tmp" /root/.ssh/known_hosts '
        ") 9>/root/.ssh/.agentic-perf-hostkeys.lock"
    )
    result = await ssh.run(controller, command, timeout=30, mutating=True)
    if result.exit_code != 0:
        return {
            "status": "error",
            "message": "Could not update the controller host record.",
            "error": result.stderr,
        }
    return {
        "status": "replaced",
        "controller": controller,
        "target": target,
        "fingerprint": approved_key.fingerprint,
    }
