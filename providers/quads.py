from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import ssl
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from providers.execution import AuditedAsyncHTTPClient, AuditedSubprocessRunner

logger = logging.getLogger(__name__)

QUADS_ASSIGNMENT_DESCRIPTION_MAX_LENGTH = 32


class QuadsAPIError(Exception):
    def __init__(self, status_code: int, message: str, path: str) -> None:
        self.status_code = status_code
        self.message = message
        self.path = path
        super().__init__(f"QUADS API error {status_code} on {path}: {message}")


class QuadsClient:
    """Async client for the QUADS self-service REST API."""

    def __init__(
        self,
        api_host: str,
        email: str,
        password: str,
        owner: str,
        ssh_key_path: str,
        default_root_password: str,
        api_scheme: str = "https",
        ca_bundle: str | None = None,
    ) -> None:
        self.api_host = api_host
        self._base_url = f"{api_scheme}://{api_host}"
        self.email = email
        self.password = password
        self.owner = owner
        self.ssh_key_path = str(Path(ssh_key_path).expanduser())
        self.default_root_password = default_root_password
        client_kwargs: dict[str, Any] = {"timeout": 30.0}
        if api_scheme == "https":
            ca_file = str(Path(ca_bundle).expanduser()) if ca_bundle else None
            client_kwargs["verify"] = ssl.create_default_context(cafile=ca_file)
        self._client = AuditedAsyncHTTPClient(**client_kwargs)
        if api_scheme == "http":
            logger.warning(
                "QUADS API using plaintext HTTP — credentials are not encrypted in transit"
            )

    @property
    def uses_https(self) -> bool:
        """Whether allocation and ownership queries use authenticated TLS."""
        from urllib.parse import urlsplit

        return urlsplit(self._base_url).scheme == "https"

    @classmethod
    async def from_secrets(
        cls,
        secrets_provider,
        *,
        organization_config: dict[str, Any] | None = None,
    ) -> QuadsClient:
        raw = await secrets_provider.get_secret("quads/config.json")
        if not raw:
            raise ValueError("QUADS config not found at secrets/quads/config.json")
        config = json.loads(raw)
        if not isinstance(config, dict):
            raise ValueError("QUADS secrets configuration must be an object")

        connection = {}
        if organization_config is not None:
            connection = organization_config.get("connection", {})
            if not isinstance(connection, dict):
                raise ValueError("QUADS organization connection config is invalid")
            connection = dict(connection)
        # Keep existing local settings as per-install overrides during the
        # migration. Null optional values mean "not set" and leave the shared
        # organization default in place.
        for key in ("api_host", "api_scheme", "ca_bundle"):
            value = config.pop(key, None)
            if value is not None:
                connection[key] = value
        config = {**config, **connection}
        required = [
            "api_host",
            "email",
            "password",
            "owner",
            "ssh_key_path",
            "default_root_password",
        ]
        missing = [k for k in required if k not in config]
        if missing:
            raise ValueError(f"QUADS secrets missing required fields: {missing}")
        if not isinstance(config["api_host"], str) or not config["api_host"].strip():
            raise ValueError("QUADS API host must be a nonempty string")
        if config.get("api_scheme", "https") not in {"http", "https"}:
            raise ValueError("QUADS API scheme must be http or https")
        if "ca_bundle" in config and config["ca_bundle"] == "":
            raise ValueError("QUADS CA bundle path must not be empty")
        return cls(
            api_host=config["api_host"],
            email=config["email"],
            password=config["password"],
            owner=config["owner"],
            ssh_key_path=config["ssh_key_path"],
            default_root_password=config["default_root_password"],
            api_scheme=config.get("api_scheme", "https"),
            ca_bundle=config.get("ca_bundle"),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _login(self) -> str:
        r = await self._client.post(
            f"{self._base_url}/api/v3/login/",
            auth=(self.email, self.password),
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        return r.json()["auth_token"]

    async def _authed_request(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        token = await self._login()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        headers.setdefault("Content-Type", "application/json")
        r = await self._client.request(
            method,
            f"{self._base_url}{path}",
            headers=headers,
            **kwargs,
        )
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = body.get("message", body.get("error", r.text))
            except Exception:
                msg = r.text
            raise QuadsAPIError(r.status_code, msg, path)
        return r

    async def get_available(
        self,
        model_filter: str | None = None,
        vendor_filter: str | None = None,
        speed_filter: int | None = None,
        disk_type_filter: str | None = None,
        duration_hours: int = 36,
    ) -> list[dict[str, Any]]:
        from datetime import datetime, timedelta, timezone

        end = datetime.now(timezone.utc) + timedelta(hours=duration_hours)
        r = await self._client.get(
            f"{self._base_url}/api/v3/available",
            params={
                "can_self_schedule": "true",
                "end": end.strftime("%Y-%m-%dT%H:%M"),
            },
        )
        r.raise_for_status()
        hostnames: list[str] = r.json()

        if model_filter:
            hostnames = [h for h in hostnames if model_filter.lower() in h.lower()]

        results = []
        for hostname in hostnames:
            try:
                detail_r = await self._client.get(
                    f"{self._base_url}/api/v3/hosts/{hostname}"
                )
                detail_r.raise_for_status()
                details = detail_r.json()
            except Exception:
                logger.warning(f"Failed to fetch details for {hostname}")
                continue

            ifaces = details.get("interfaces", [])
            if vendor_filter:
                ifaces = [
                    i
                    for i in ifaces
                    if vendor_filter.lower() in i.get("vendor", "").lower()
                ]
            if speed_filter:
                ifaces = [i for i in ifaces if i.get("speed") == speed_filter]

            disks = details.get("disks", [])
            if disk_type_filter:
                matching_disks = [
                    d
                    for d in disks
                    if disk_type_filter.lower() in d.get("disk_type", "").lower()
                ]
                if not matching_disks:
                    continue
            else:
                matching_disks = disks

            if vendor_filter or speed_filter:
                if not ifaces:
                    continue

            proc = (details.get("processors") or [{}])[0]
            results.append(
                {
                    "hostname": hostname,
                    "model": details.get("model", "unknown"),
                    "cpu": proc.get("product", "unknown"),
                    "cores": proc.get("cores", "unknown"),
                    "memory_gb": sum(
                        m.get("size_gb", 0) for m in details.get("memory", [])
                    ),
                    "disks": [
                        {
                            "disk_type": d.get("disk_type"),
                            "size_gb": d.get("size_gb"),
                            "count": d.get("count"),
                        }
                        for d in disks
                    ],
                    "nics": [
                        {
                            "name": i.get("name"),
                            "vendor": i.get("vendor"),
                            "speed": i.get("speed"),
                            "mac": i.get("mac_address"),
                        }
                        for i in details.get("interfaces", [])
                    ],
                }
            )

        return results

    async def create_assignment(
        self,
        description: str,
        owner: str | None = None,
        os_type: str | None = None,
        wipe: bool = True,
    ) -> dict[str, Any]:
        description = " ".join(description.split())
        # QUADS uses this field as a Jira title; bound arbitrary caller text.
        if len(description) > QUADS_ASSIGNMENT_DESCRIPTION_MAX_LENGTH:
            description = description[: QUADS_ASSIGNMENT_DESCRIPTION_MAX_LENGTH - 1]
            word_boundary = description.rfind(" ")
            if word_boundary > QUADS_ASSIGNMENT_DESCRIPTION_MAX_LENGTH // 2:
                description = description[:word_boundary]
            description = f"{description.rstrip()}…"
        r = await self._authed_request(
            "POST",
            "/api/v3/assignments/self",
            json={
                "description": description,
                "owner": owner or self.owner,
                "qinq": 0,
                "wipe": wipe,
                **({"ostype": os_type} if os_type is not None else {}),
            },
        )
        data = r.json()
        return {
            "id": data["id"],
            "cloud_name": data["cloud"]["name"],
            "ticket": data.get("ticket"),
        }

    async def get_os_list(self) -> list[dict[str, Any]]:
        r = await self._client.get(f"{self._base_url}/api/v3/hosts/os_list")
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise ValueError("QUADS OS list response must be an array")
        return data

    async def schedule_host(
        self, cloud_name: str, hostname: str, duration_hours: int = 36
    ) -> dict[str, Any]:
        from datetime import datetime, timedelta, timezone

        end = datetime.now(timezone.utc) + timedelta(hours=duration_hours)
        end_str = end.strftime("%Y-%m-%dT%H:%M")

        r = await self._authed_request(
            "POST",
            "/api/v3/schedules",
            json={"cloud": cloud_name, "hostname": hostname, "end": end_str},
        )
        data = r.json()
        return {
            "hostname": hostname,
            "cloud": cloud_name,
            "end": data.get("end", end_str),
        }

    async def get_host_cloud(self, hostname: str) -> str | None:
        """Return the current QUADS cloud, or None when not reported."""
        from urllib.parse import quote

        response = await self._client.get(
            f"{self._base_url}/api/v3/hosts/{quote(hostname, safe='')}"
        )
        response.raise_for_status()
        data = response.json()
        cloud = data.get("cloud") if isinstance(data, dict) else None
        return cloud.get("name") if isinstance(cloud, dict) else None

    async def get_foreman_assignment_identity(
        self, assignment_id: int
    ) -> dict[str, Any]:
        """Fetch allocation identity for internal authorization only."""
        response = await self._client.get(
            f"{self._base_url}/api/v3/assignments/{assignment_id}"
        )
        response.raise_for_status()
        data = response.json()
        cloud = data.get("cloud")
        return {
            "active": data.get("active"),
            "validated": data.get("validated"),
            "cloud": cloud.get("name") if isinstance(cloud, dict) else None,
            "ticket": data.get("ticket"),
        }

    async def get_active_cloud_assignment(
        self, cloud_name: str
    ) -> dict[str, Any] | None:
        """Return the active assignment for a cloud, or None when there is none."""
        from urllib.parse import quote

        response = await self._client.get(
            f"{self._base_url}/api/v3/assignments/active/{quote(cloud_name, safe='')}/"
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not data:
            return None
        cloud = data.get("cloud")
        return {
            "active": data.get("active"),
            "validated": data.get("validated"),
            "cloud": cloud.get("name") if isinstance(cloud, dict) else None,
            "ticket": data.get("ticket"),
        }

    async def get_assignment_status(self, assignment_id: int) -> dict[str, Any]:
        r = await self._client.get(
            f"{self._base_url}/api/v3/assignments/{assignment_id}"
        )
        r.raise_for_status()
        data = r.json()
        return {
            "id": assignment_id,
            "description": data.get("description"),
            "cloud": data.get("cloud", {}).get("name"),
            "owner": data.get("owner"),
            "validated": data.get("validated"),
            "provisioned": data.get("provisioned"),
        }

    async def get_move_progress(self, cloud_name: str) -> list[dict[str, Any]]:
        """Read QUADS' last reported move records for an allocation."""
        response = await self._client.get(
            f"{self._base_url}/api/v3/moves/progress/",
            params={"cloud": cloud_name},
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise ValueError("QUADS move progress response is not a list")
        return [item for item in data if isinstance(item, dict)]

    async def get_host_progress(self, hostname: str) -> dict[str, Any]:
        """Read the host fields relevant to QUADS validation."""
        response = await self._client.get(
            f"{self._base_url}/api/v3/hosts/{quote(hostname, safe='')}"
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("QUADS host response is not an object")
        return data

    async def poll_until_validated(
        self,
        assignment_id: int,
        interval: int = 120,
        timeout: int = 3600,
        *,
        progress_callback: Callable[[dict[str, Any], int, str], Awaitable[None]]
        | None = None,
    ) -> dict[str, Any]:
        elapsed = 0
        status: dict[str, Any] = {}

        async def report(phase: str) -> None:
            if progress_callback is None:
                return
            try:
                # Reporting is useful but must never strand an allocation.
                await asyncio.wait_for(progress_callback(status, elapsed, phase), 15)
            except Exception:
                logger.debug("QUADS progress reporting failed")

        while elapsed < timeout:
            status = await self.get_assignment_status(assignment_id)
            logger.info(
                f"[quads] Assignment {assignment_id}: "
                f"validated={status.get('validated')}, "
                f"provisioned={status.get('provisioned')} "
                f"({elapsed}s elapsed)"
            )
            if status.get("validated") is True:
                await report("validated")
                return status
            # Fetching diagnostics should not extend the reservation poll interval.
            await asyncio.gather(report("waiting"), asyncio.sleep(interval))
            elapsed += interval

        await report("timed_out")
        raise TimeoutError(
            f"QUADS assignment {assignment_id} not validated after {timeout}s"
        )

    async def terminate_assignment(self, assignment_id: int) -> dict[str, Any]:
        r = await self._authed_request(
            "POST",
            f"/api/v3/assignments/terminate/{assignment_id}",
        )
        return {
            "assignment_id": assignment_id,
            "status": "terminated",
            "response": r.json() if r.content else {},
        }

    def validate_ssh_identity(self) -> None:
        """Require an existing key pair before reserving or bootstrapping hosts."""
        key_path = Path(self.ssh_key_path)
        pubkey_path = Path(f"{self.ssh_key_path}.pub")
        missing = [str(path) for path in (key_path, pubkey_path) if not path.is_file()]
        if missing:
            raise ValueError(f"SSH identity files not found: {', '.join(missing)}")

    async def setup_ssh(self, hosts: list[str]) -> dict[str, Any]:
        try:
            self.validate_ssh_identity()
            for host in hosts:
                self._validate_ssh_hostname(host)
        except ValueError as exc:
            return {"status": "failed", "message": str(exc)}
        pubkey_path = Path(f"{self.ssh_key_path}.pub")
        pubkey = pubkey_path.read_text().strip()

        for host in hosts:
            await AuditedSubprocessRunner().run(
                ["ssh-keygen", "-R", host], mutating=True
            )

        results: dict[str, str] = {}
        for host in hosts:
            try:
                result = await self._copy_ssh_key(host, pubkey)
                results[host] = result
            except Exception:
                results[host] = "failed: SSH key bootstrap failed"

        return {
            "status": "success"
            if all(v in {"added", "existing"} for v in results.values())
            else "partial",
            "ssh_key_path": self.ssh_key_path,
            "hosts": results,
        }

    PROVISIONING_KEY_COMMENT = "host-key-do-not-remove"

    async def cleanup_ssh_keys(self, hosts: list[str]) -> dict[str, Any]:
        """Remove the QUADS provisioning key from authorized_keys on each host."""
        results: dict[str, str] = {}
        for host in hosts:
            try:
                ssh_result = await AuditedSubprocessRunner().run(
                    [
                        "ssh",
                        "-o",
                        "ConnectTimeout=10",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "StrictHostKeyChecking=accept-new",
                        "-i",
                        self.ssh_key_path,
                        f"root@{host}",
                        f"sed -i '/{self.PROVISIONING_KEY_COMMENT}/d' /root/.ssh/authorized_keys",
                    ],
                    mutating=True,
                )
                stderr = ssh_result.stderr
                results[host] = (
                    "cleaned"
                    if ssh_result.returncode == 0
                    else f"failed: {stderr.decode().strip()}"
                )
            except Exception as e:
                results[host] = f"failed: {e}"
        return {
            "status": "success"
            if all("cleaned" in v for v in results.values())
            else "partial",
            "hosts": results,
        }

    @staticmethod
    def _validate_ssh_hostname(host: str) -> None:
        if (
            not isinstance(host, str)
            or len(host) > 253
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host)
        ):
            raise ValueError("Invalid QUADS host identity for SSH")

    async def _copy_ssh_key(self, host: str, pubkey: str) -> str:
        self._validate_ssh_hostname(host)
        if not pubkey or "\n" in pubkey or "\r" in pubkey:
            raise ValueError("Invalid SSH public key")
        fields = pubkey.split()
        if len(fields) < 2:
            raise ValueError("Invalid SSH public key")
        key_material = shlex.quote(" ".join(fields[:2]))
        key = shlex.quote(f"{pubkey} {self.PROVISIONING_KEY_COMMENT}")
        remote_cmd = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
            f"if awk '{{print $1\" \"$2}}' ~/.ssh/authorized_keys | "
            f"grep -Fqx -- {key_material}; then echo KEY_PRESENT; "
            f"else printf '%s\\n' {key} >> ~/.ssh/authorized_keys; "
            "echo KEY_ADDED; fi"
        )
        proc = await AuditedSubprocessRunner().run(
            [
                "python3",
                "-c",
                (
                    "import pexpect, sys\n"
                    "password = sys.stdin.readline().rstrip('\\n')\n"
                    "try:\n"
                    " child = pexpect.spawn('/usr/bin/ssh', sys.argv[1:], timeout=30, echo=False)\n"
                    " child.expect('[Pp]assword')\n"
                    " child.sendline(password)\n"
                    " child.expect(pexpect.EOF)\n"
                    " text = child.before.decode(errors='replace')\n"
                    " print('added' if 'KEY_ADDED' in text else "
                    "'existing' if 'KEY_PRESENT' in text else 'failed')\n"
                    "except Exception:\n"
                    " print('failed')\n"
                ),
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "PreferredAuthentications=password",
                "-o",
                "ConnectTimeout=10",
                f"root@{host}",
                remote_cmd,
            ],
            stdin=self.default_root_password.encode() + b"\n",
            mutating=True,
        )
        output = proc.stdout.decode(errors="replace").strip()
        if proc.returncode == 0 and output in {"added", "existing"}:
            return output
        return "failed: SSH key bootstrap failed"
