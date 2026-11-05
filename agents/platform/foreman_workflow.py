"""Ticket-bound Foreman reinstall with durable before-send checkpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from agents.server_utils import (
    assert_ticket_active,
    build_organization_skill_resolver_async,
    build_secrets_provider,
    ticket_state_headers,
    tool_progress,
)
from providers.execution import AuditedAsyncHTTPClient
from providers.foreman import ForemanClient, ForemanError
from providers.foreman_config import foreman_credentials, resolve_foreman_config
from providers.resource.quads_foreman import (
    quads_assignment_directive,
    verify_quads_ownership,
)
from providers.rhel_kernel import build_reboot_command


def selected_hosts(cf: dict[str, Any], request: dict[str, Any]) -> list[str]:
    """Resolve requested roles conservatively from authoritative host identities."""
    directive = quads_assignment_directive(cf)
    assigned = cf.get("assigned_hardware_ips")
    if not isinstance(assigned, dict):
        if directive is None:
            raise ForemanError("host", "No allocated host identities")
        assigned = {}
    controller = assigned.get("controller")
    targets = assigned.get("targets", [])
    if not isinstance(targets, list) or any(
        not isinstance(x, str) or not x for x in targets
    ):
        raise ForemanError("host", "Invalid allocated host identities")
    hosts = list(dict.fromkeys(([controller] if controller else []) + targets))
    if directive is not None:
        required = cf.get("required_hosts")
        named = (
            [
                entry["host"].strip()
                for entry in required
                if isinstance(entry, dict)
                and isinstance(entry.get("host"), str)
                and entry["host"].strip()
            ]
            if isinstance(required, list)
            else []
        )
        if not named:
            raise ForemanError(
                "host", "Formal QUADS assignment requires named required_hosts"
            )
        if not hosts:
            hosts = list(dict.fromkeys(named))
        if any(host not in set(named) for host in hosts):
            raise ForemanError("host", "Requested host is not named in required_hosts")
    if not hosts or any(not isinstance(x, str) or not x for x in hosts):
        raise ForemanError("host", "No allocated host identities")
    roles = request.get("roles")
    if roles is None:
        return hosts
    if (
        not isinstance(roles, list)
        or not roles
        or any(not isinstance(role, str) or not role for role in roles)
    ):
        raise ForemanError("host", "Invalid reinstall roles")
    if len(hosts) == 1:
        required = cf.get("required_hosts", [])
        if (
            not isinstance(required, list)
            or len(required) != 1
            or not isinstance(required[0], dict)
        ):
            raise ForemanError("host", "Requested reinstall role cannot be verified")
        entry = required[0]
        if entry.get("host") and entry["host"] != hosts[0]:
            raise ForemanError("host", "Requested reinstall role cannot be verified")
        known = (
            set(entry.get("roles", []))
            if isinstance(entry.get("roles"), list)
            else set()
        )
        if not set(roles).issubset(known):
            raise ForemanError("host", "Requested reinstall role cannot be verified")
        return hosts
    raise ForemanError("host", "Requested reinstall roles cannot be mapped safely")


def assignment_metadata(
    cf: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str] | None]:
    """Return Foreman identity, letting a user directive outrank stale metadata."""
    metadata = cf.get("resource_provider_metadata") or {}
    if not isinstance(metadata, dict):
        raise ForemanError("host", "Resource allocation metadata is invalid")
    metadata = dict(metadata)
    directive = quads_assignment_directive(cf)
    if directive is not None:
        metadata["cloud_name"] = directive["cloud_name"]
        metadata["ticket"] = directive["ticket"]
        metadata.pop("assignment_id", None)
    return metadata, directive


def request_fingerprint(cf: dict[str, Any]) -> str:
    directives = cf.get("directives", {})
    request = (
        directives.get("platform_reinstall") if isinstance(directives, dict) else None
    )
    if not isinstance(request, dict):
        raise ForemanError("configuration", "Invalid platform reinstall request")
    metadata, _directive = assignment_metadata(cf)
    payload = {
        "request": request,
        "hosts": selected_hosts(cf, request),
        "provider": cf.get("resource_provider"),
        "assignment_id": metadata.get("assignment_id"),
        "cloud_name": metadata.get("cloud_name"),
        "allocation_ticket": metadata.get("ticket"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def reinstall_verified(cf: dict[str, Any]) -> bool:
    state = cf.get("foreman_reinstall_state")
    if not isinstance(state, dict) or state.get("status") != "verified":
        return False
    try:
        directives = cf.get("directives", {})
        hosts = selected_hosts(cf, directives["platform_reinstall"])
        records = state.get("hosts")
        return (
            state.get("fingerprint") == request_fingerprint(cf)
            and isinstance(records, dict)
            and all(
                isinstance(records.get(host), dict)
                and records[host].get("phase") == "verified"
                for host in hosts
            )
        )
    except (ForemanError, KeyError, TypeError, ValueError):
        return False


async def _save_state(ticket: dict[str, Any], state: dict[str, Any]) -> None:
    ticket_id = ticket.get("id") or os.environ.get("TICKET_ID", "")
    if not ticket_id:
        raise ForemanError("state", "Ticket identity is required for reinstall")
    store = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
    async with AuditedAsyncHTTPClient(
        base_url=store, headers=ticket_state_headers(), timeout=15.0
    ) as client:
        response = await client.patch(
            f"/api/v1/tickets/{ticket_id}/fields",
            json={"fields": {"foreman_reinstall_state": state}},
        )
        response.raise_for_status()
    ticket.setdefault("custom_fields", {})["foreman_reinstall_state"] = state


async def _assert_active(ticket_id: str, fingerprint: str) -> None:
    ticket = await assert_ticket_active(ticket_id, expected_status="preparing_platform")
    if ticket.get("status") == "rejected":
        raise ForemanError("state", "Ticket is no longer authorized for platform setup")
    if request_fingerprint(ticket.get("custom_fields", {})) != fingerprint:
        raise ForemanError("state", "Reinstall intent or allocation changed")


def _os_release_matches(output: str, expected: dict[str, str]) -> bool:
    data: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key not in {"ID", "VERSION_ID"}:
            continue
        try:
            parsed = shlex.split(value)
        except ValueError:
            return False
        if len(parsed) == 1:
            data[key] = parsed[0]
    return (
        data.get("ID") == expected["id"]
        and data.get("VERSION_ID") == expected["version"]
    )


def _expected_release(
    os_details: dict[str, Any], options: dict[str, Any]
) -> dict[str, str]:
    expected_id = options.get("os_release_id")
    expected_version = options.get("os_release_version")
    if not expected_id:
        aliases = {
            "rhel": "rhel",
            "redhat": "rhel",
            "fedora": "fedora",
            "rocky": "rocky",
        }
        expected_id = aliases.get(str(os_details.get("name", "")).casefold())
    if not expected_version:
        major, minor = os_details.get("major"), os_details.get("minor")
        if isinstance(major, str) and major and isinstance(minor, str) and minor:
            expected_version = f"{major}.{minor}"
        elif isinstance(major, str) and major and minor in ("", None):
            expected_version = major
    if (
        not isinstance(expected_id, str)
        or not expected_id
        or not isinstance(expected_version, str)
        or not expected_version
    ):
        raise ForemanError(
            "configuration", "Expected OS release identity is not configured"
        )
    return {"id": expected_id, "version": expected_version}


def _build_matches(host: dict[str, Any], options: dict[str, int]) -> bool:
    return host.get("build") is True and all(
        host.get(k) == v for k, v in options.items()
    )


def _install_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    if value.endswith(" UTC"):
        value = value[:-4] + "+00:00"
    elif value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = datetime.fromisoformat(value + "+00:00")
    except ValueError:
        return None
    return parsed


def _installation_completed(host: dict[str, Any], record: dict[str, Any]) -> bool:
    installed = _install_time(host.get("installed_at"))
    previous_raw = record.get("previous_installed_at")
    previous = _install_time(previous_raw)
    return (
        installed is not None
        and (previous_raw is None or previous is not None)
        and (previous is None or installed > previous)
    )


def _verify_key_pair(quads: Any, ssh: Any) -> None:
    key = Path(quads.ssh_key_path).expanduser() if quads else None
    ssh_key = Path(ssh.key_path).expanduser() if ssh.key_path else None
    if (
        key is None
        or ssh_key != key
        or not key.is_file()
        or not Path(f"{key}.pub").is_file()
    ):
        raise ForemanError(
            "configuration", "Configured SSH key pair is missing or mismatched"
        )


async def _bootstrap(quads: Any, ssh: Any, host: str, config: dict[str, Any]) -> None:
    bootstrap = config["bootstrap"]
    if bootstrap["mode"] == "existing":
        return
    _verify_key_pair(quads, ssh)
    result = await quads.setup_ssh([host])
    if result.get("status") != "success":
        raise ForemanError("ssh", "Post-install SSH key setup failed")


async def _monitor_host(
    *,
    client: Any,
    quads: Any,
    ssh: Any,
    host: str,
    record: dict[str, Any],
    config: dict[str, Any],
    metadata: dict[str, Any],
    ticket_id: str,
    fingerprint: str,
    build_timeout: float = 3600,
    ssh_timeout: float = 1800,
    interval: float = 20,
    require_active_assignment: bool = True,
) -> None:
    build_deadline = time.monotonic() + build_timeout
    last_observation = "Foreman build remains in progress"

    async def current_host() -> dict[str, Any] | None:
        """Return a host snapshot, retrying transient Foreman GET failures."""
        nonlocal last_observation
        try:
            current = await client.find_host(host)
        except ForemanError as exc:
            if exc.category not in {"transport", "api"}:
                raise
            last_observation = f"Transient Foreman error: {exc}"
            await tool_progress(
                f"Retrying Foreman status for {host}", "provision_platform"
            )
            return None
        if current.get("id") != record["host_id"]:
            raise ForemanError("host", "Foreman host identity changed")
        return current

    async def check_ownership() -> None:
        await _assert_active(ticket_id, fingerprint)
        if quads is not None:
            await verify_quads_ownership(
                quads,
                metadata,
                [host],
                require_active_assignment=require_active_assignment,
            )

    while True:
        await check_ownership()
        current = await current_host()
        if current is not None and current.get("build") is False:
            if not _installation_completed(current, record):
                last_observation = "Foreman has not reported a new installation"
            elif (
                current.get("operatingsystem_id")
                != record["options"]["operatingsystem_id"]
            ):
                raise ForemanError("os", "Foreman reports a different OS after install")
            else:
                break
        if time.monotonic() >= build_deadline:
            raise ForemanError(
                "timeout", f"Timed out waiting for Foreman build: {last_observation}"
            )
        await tool_progress(
            f"Waiting for Foreman install on {host}", "provision_platform"
        )
        await asyncio.sleep(interval)

    bootstrapped = False
    ssh_deadline = time.monotonic() + ssh_timeout
    while True:
        await check_ownership()
        current = await current_host()
        if current is not None and current.get("build") is False:
            if not _installation_completed(current, record):
                last_observation = "Foreman has not reported a new installation"
            elif (
                current.get("operatingsystem_id")
                != record["options"]["operatingsystem_id"]
            ):
                raise ForemanError("os", "Foreman reports a different OS after install")
            else:
                if not bootstrapped:
                    try:
                        await _bootstrap(quads, ssh, host, config)
                        bootstrapped = True
                    except ForemanError as exc:
                        if exc.category != "ssh":
                            raise
                        last_observation = "Post-install SSH bootstrap failed"
                if bootstrapped:
                    result = await ssh.run(
                        host,
                        "cat /proc/sys/kernel/random/boot_id /etc/os-release",
                        timeout=30,
                        mutating=False,
                    )
                    if result.exit_code == 0:
                        lines = result.stdout.splitlines()
                        if (
                            lines
                            and lines[0].strip()
                            and lines[0].strip() != record["previous_boot_id"]
                            and _os_release_matches(
                                "\n".join(lines[1:]), record["expected_release"]
                            )
                        ):
                            return
                        last_observation = "Boot identity or installed OS did not match"
                    else:
                        last_observation = "Post-install SSH was unavailable"
                if config["bootstrap"]["mode"] == "quads":
                    # A successful key copy can have reached the old OS before
                    # installation erased its authorized_keys. Retry after any
                    # unsuccessful boot/OS verification.
                    bootstrapped = False
        elif current is not None:
            last_observation = "Foreman build remains in progress"
        if time.monotonic() >= ssh_deadline:
            raise ForemanError(
                "timeout",
                f"Timed out verifying Foreman installation: {last_observation}",
            )
        await tool_progress(
            f"Waiting for Foreman install on {host}", "provision_platform"
        )
        await asyncio.sleep(interval)


async def _prepare_host(
    *,
    client: Any,
    quads: Any,
    ssh: Any,
    host: str,
    records: dict[str, Any],
    state: dict[str, Any],
    ticket: dict[str, Any],
    ticket_id: str,
    fingerprint: str,
    os_name: str,
    configured_options: dict[str, Any],
    provenance: dict[str, Any],
    metadata: dict[str, Any],
    require_active_assignment: bool,
) -> bool:
    """Advance one host to monitor-ready, or reverify a completed one.

    Returning ``True`` means the caller must monitor the host; ``False`` means
    the durable checkpoint is already verified and was revalidated.
    """
    await _assert_active(ticket_id, fingerprint)
    if quads is not None:
        await verify_quads_ownership(
            quads,
            metadata,
            [host],
            require_active_assignment=require_active_assignment,
        )
    current = await client.find_host(host)
    record = records.get(host)
    if record is None:
        if current.get("build") is not False:
            raise ForemanError("build", "Host is already in Foreman build mode")
        if "installed_at" not in current:
            raise ForemanError(
                "api",
                "Foreman host does not expose installation completion time",
            )
        previous_installed_at = current.get("installed_at")
        if (
            previous_installed_at is not None
            and _install_time(previous_installed_at) is None
        ):
            raise ForemanError("api", "Foreman installation timestamp is invalid")
        options = await client.resolve_install_options(
            current,
            os_name,
            medium_id=configured_options.get("medium_id"),
            ptable_id=configured_options.get("ptable_id"),
        )
        details = await client.get_operating_system(options["operatingsystem_id"])
        expected = _expected_release(details, configured_options)
        result = await ssh.run(
            host,
            "cat /proc/sys/kernel/random/boot_id",
            timeout=30,
            mutating=False,
        )
        if result.exit_code != 0 or not result.stdout.strip():
            raise ForemanError("ssh", "Cannot verify host access before reinstall")
        record = {
            "phase": "build_unknown",
            "host_id": current["id"],
            "options": options,
            "expected_release": expected,
            "previous_boot_id": result.stdout.strip(),
            "previous_installed_at": previous_installed_at,
        }
        records[host] = record
        state["provenance"] = provenance
        await _save_state(ticket, state)
        await _assert_active(ticket_id, fingerprint)
        await verify_quads_ownership(
            quads,
            metadata,
            [host],
            require_active_assignment=require_active_assignment,
        )
        await client.set_build(record["host_id"], options)
        record["phase"] = "build_armed"
        await _save_state(ticket, state)
    if record.get("phase") == "verified":
        if (
            current.get("id") != record["host_id"]
            or current.get("build") is not False
            or not _installation_completed(current, record)
        ):
            raise ForemanError("host", "Previously verified host is no longer ready")
        if current.get("operatingsystem_id") != record["options"]["operatingsystem_id"]:
            raise ForemanError("os", "Previously verified host has a different OS")
        result = await ssh.run(
            host,
            "cat /proc/sys/kernel/random/boot_id /etc/os-release",
            timeout=30,
            mutating=False,
        )
        lines = result.stdout.splitlines()
        if (
            result.exit_code != 0
            or not lines
            or not lines[0].strip()
            or lines[0].strip() == record["previous_boot_id"]
            or not _os_release_matches("\n".join(lines[1:]), record["expected_release"])
        ):
            raise ForemanError(
                "ssh", "Previously verified host could not be reverified"
            )
        return False
    if record.get("phase") == "build_unknown":
        if not _build_matches(current, record["options"]):
            raise ForemanError(
                "build_unknown",
                "Foreman build outcome is unknown; manual reconciliation required",
            )
        record["phase"] = "build_armed"
        await _save_state(ticket, state)
    if record.get("phase") == "build_armed":
        current = await client.find_host(host)
        if not _build_matches(current, record["options"]):
            raise ForemanError(
                "build_unknown",
                "Foreman build is not armed; manual reconciliation required",
            )
        if quads is not None:
            await verify_quads_ownership(
                quads,
                metadata,
                [host],
                require_active_assignment=require_active_assignment,
            )
        record["phase"] = "reboot_unknown"
        await _save_state(ticket, state)
        await _assert_active(ticket_id, fingerprint)
        await verify_quads_ownership(
            quads,
            metadata,
            [host],
            require_active_assignment=require_active_assignment,
        )
        try:
            reboot = await ssh.run(
                host, build_reboot_command(), timeout=30, mutating=True
            )
        except Exception:
            raise ForemanError(
                "reboot_unknown",
                "Reboot outcome is unknown; reconcile the host before retrying",
            ) from None
        if reboot.exit_code not in (0, 255, -1) or "@@rebooting" not in reboot.stdout:
            raise ForemanError(
                "reboot",
                "Host did not confirm the detached reboot; manual reconciliation required",
            )
    if record.get("phase") != "reboot_unknown":
        raise ForemanError("state", "Unknown Foreman reinstall checkpoint")
    return True


async def provision_foreman(
    ticket: dict[str, Any],
    ssh: Any,
    *,
    build_timeout: float = 3600,
    ssh_timeout: float = 1800,
    interval: float = 20,
) -> dict[str, Any]:
    """Run or safely resume one explicitly requested Foreman reinstall."""
    cf = ticket.get("custom_fields", {})
    request = cf.get("directives", {}).get("platform_reinstall")
    if not isinstance(request, dict) or request.get("provider") != "foreman":
        raise ForemanError("configuration", "Invalid Foreman reinstall request")
    os_name = request.get("os")
    if not isinstance(os_name, str) or not os_name.strip():
        raise ForemanError("configuration", "Requested OS is missing")
    hosts = selected_hosts(cf, request)
    fingerprint = request_fingerprint(cf)
    state = cf.get("foreman_reinstall_state")
    if state is None:
        state = {"fingerprint": fingerprint, "status": "in_progress", "hosts": {}}
    elif not isinstance(state, dict):
        raise ForemanError("state", "Invalid Foreman reinstall checkpoint")
    elif state.get("fingerprint") != fingerprint and state.get("status") == "verified":
        old_hosts = state.get("hosts")
        if (
            not isinstance(old_hosts, dict)
            or not old_hosts
            or any(
                not isinstance(record, dict) or record.get("phase") != "verified"
                for record in old_hosts.values()
            )
        ):
            raise ForemanError("state", "Incomplete prior Foreman verification")
        state = {"fingerprint": fingerprint, "status": "in_progress", "hosts": {}}
    elif state.get("fingerprint") != fingerprint:
        raise ForemanError("state", "Reinstall request changed after work started")
    if state.get("status") == "verified":
        if not reinstall_verified(cf):
            raise ForemanError("state", "Incomplete Foreman verification checkpoint")
    records = state.get("hosts")
    if not isinstance(records, dict):
        raise ForemanError("state", "Invalid reinstall checkpoint")
    ticket_id = ticket.get("id") or os.environ.get("TICKET_ID", "")
    if not ticket_id:
        raise ForemanError("state", "Ticket identity is required for reinstall")
    secrets = build_secrets_provider()
    resolver = await build_organization_skill_resolver_async(
        secrets_provider=secrets, ticket_id=ticket_id
    )
    config, provenance = await resolve_foreman_config(
        resolver, secrets, request.get("site")
    )
    bootstrap = config.get("bootstrap")
    if not isinstance(bootstrap, dict) or bootstrap.get("mode") not in {
        "existing",
        "quads",
    }:
        raise ForemanError(
            "configuration", "Post-install SSH bootstrap policy is missing"
        )
    if (
        bootstrap.get("mode") == "quads"
        and bootstrap.get("host_key_policy") != "replace-unverified"
    ):
        raise ForemanError("configuration", "QUADS host-key policy must be configured")
    binding = hashlib.sha256(
        json.dumps(
            {"config": config, "site": provenance.get("site")},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if state.get("service_binding") not in (None, binding):
        raise ForemanError("state", "Foreman service changed after work started")
    state["service_binding"] = binding
    metadata, directive = assignment_metadata(cf)
    provider = cf.get("resource_provider")
    if directive is not None and provider not in {"quads", "user_provided"}:
        raise ForemanError(
            "host", "QUADS assignment directive is not valid for this provider"
        )
    if provider != "quads" and directive is None:
        raise ForemanError("host", "Provider does not support verified Foreman targets")
    require_active_assignment = directive is None
    quads = None
    client = None
    try:
        from providers.quads import QuadsClient

        quads_config = (
            resolver.get_runtime_config("resource/quads")
            if resolver.has_runtime_config("resource/quads")
            else None
        )
        quads = await QuadsClient.from_secrets(
            secrets, organization_config=quads_config
        )
        if quads.uses_https is not True:
            raise ForemanError(
                "configuration", "QUADS ownership requires verified HTTPS"
            )
        await verify_quads_ownership(
            quads,
            metadata,
            hosts,
            require_active_assignment=require_active_assignment,
        )
        if bootstrap["mode"] == "quads":
            _verify_key_pair(quads, ssh)
        username, password = await foreman_credentials(config, secrets, metadata)
        client = ForemanClient(
            config["endpoint"], username, password, ca_bundle=config.get("ca_bundle")
        )
        site_options = config.get("os_options", {})
        if not isinstance(site_options, dict):
            raise ForemanError("configuration", "Invalid OS installation options")
        configured_options = site_options.get(os_name, {})
        if not isinstance(configured_options, dict):
            raise ForemanError("configuration", "Invalid OS installation options")

        host_results: dict[str, dict[str, Any]] = {}
        monitor_hosts: list[str] = []
        for host in hosts:
            needs_monitor = await _prepare_host(
                client=client,
                quads=quads,
                ssh=ssh,
                host=host,
                records=records,
                state=state,
                ticket=ticket,
                ticket_id=ticket_id,
                fingerprint=fingerprint,
                os_name=os_name,
                configured_options=configured_options,
                provenance=provenance,
                metadata=metadata,
                require_active_assignment=require_active_assignment,
            )
            if needs_monitor:
                monitor_hosts.append(host)
            else:
                host_results[host] = {"status": "verified"}

        if monitor_hosts:
            outcomes = await asyncio.gather(
                *(
                    _monitor_host(
                        client=client,
                        quads=quads,
                        ssh=ssh,
                        host=host,
                        record=records[host],
                        config=config,
                        metadata=metadata,
                        ticket_id=ticket_id,
                        fingerprint=fingerprint,
                        build_timeout=build_timeout,
                        ssh_timeout=ssh_timeout,
                        interval=interval,
                        require_active_assignment=require_active_assignment,
                    )
                    for host in monitor_hosts
                ),
                return_exceptions=True,
            )
            for host, outcome in zip(monitor_hosts, outcomes, strict=True):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                if isinstance(outcome, ForemanError):
                    host_results[host] = {
                        "status": "failed",
                        "category": outcome.category,
                        "message": str(outcome),
                    }
                elif isinstance(outcome, BaseException):
                    raise outcome
                else:
                    records[host]["phase"] = "verified"
                    host_results[host] = {"status": "verified"}
            await _save_state(ticket, state)

        diagnostics = [
            f"{host}: OS and SSH verified"
            if result["status"] == "verified"
            else f"{host}: {result['message']}"
            for host, result in host_results.items()
        ]
        verified = all(
            result["status"] == "verified" for result in host_results.values()
        )
        if not verified:
            state["status"] = "in_progress"
            await _save_state(ticket, state)
            return {
                "success": False,
                "status": "partial"
                if any(
                    result["status"] == "verified" for result in host_results.values()
                )
                else "failed",
                "hosts": hosts,
                "host_results": host_results,
                "diagnostics": diagnostics,
            }
        state["status"] = "verified"
        await _save_state(ticket, state)
        return {
            "success": True,
            "status": "success",
            "hosts": hosts,
            "host_results": host_results,
            "diagnostics": diagnostics,
        }
    finally:
        if client is not None:
            await client.close()
        if quads is not None:
            await quads.close()
