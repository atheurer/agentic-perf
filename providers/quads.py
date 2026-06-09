from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class QuadsClient:
    """Async client for the QUADS self-service REST API."""

    def __init__(
        self,
        api_host: str,
        email: str,
        password: str,
        owner: str,
        ssh_key_path: str = "~/.ssh/id_ed25519_quads",
        default_root_password: str = "100yard-",
    ) -> None:
        self.api_host = api_host
        self.email = email
        self.password = password
        self.owner = owner
        self.ssh_key_path = str(Path(ssh_key_path).expanduser())
        self.default_root_password = default_root_password
        self._client = httpx.AsyncClient(timeout=30.0)

    @classmethod
    async def from_secrets(cls, secrets_provider) -> QuadsClient:
        raw = await secrets_provider.get_secret("quads/config.json")
        if not raw:
            raise ValueError("QUADS config not found at secrets/quads/config.json")
        config = json.loads(raw)
        return cls(
            api_host=config["api_host"],
            email=config["email"],
            password=config["password"],
            owner=config["owner"],
            ssh_key_path=config.get("ssh_key_path", "~/.ssh/id_ed25519_quads"),
            default_root_password=config.get("default_root_password", "100yard-"),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _login(self) -> str:
        r = await self._client.post(
            f"http://{self.api_host}/api/v3/login/",
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
            f"http://{self.api_host}{path}",
            headers=headers,
            **kwargs,
        )
        r.raise_for_status()
        return r

    async def get_available(
        self,
        model_filter: str | None = None,
        vendor_filter: str | None = None,
        speed_filter: int | None = None,
    ) -> list[dict[str, Any]]:
        r = await self._client.get(
            f"http://{self.api_host}/api/v3/available",
            params={"can_self_schedule": "true"},
        )
        r.raise_for_status()
        hostnames: list[str] = r.json()

        if model_filter:
            hostnames = [h for h in hostnames if model_filter.lower() in h.lower()]

        if not (vendor_filter or speed_filter):
            return [{"hostname": h} for h in hostnames]

        results = []
        for hostname in hostnames:
            try:
                detail_r = await self._client.get(
                    f"http://{self.api_host}/api/v3/hosts/{hostname}"
                )
                detail_r.raise_for_status()
                details = detail_r.json()
            except Exception:
                logger.warning(f"Failed to fetch details for {hostname}")
                continue

            ifaces = details.get("interfaces", [])
            matched = ifaces

            if vendor_filter:
                matched = [
                    i for i in matched
                    if vendor_filter.lower() in i.get("vendor", "").lower()
                ]
            if speed_filter:
                matched = [
                    i for i in matched if i.get("speed") == speed_filter
                ]

            if matched:
                proc = (details.get("processors") or [{}])[0]
                results.append({
                    "hostname": hostname,
                    "model": details.get("model", "unknown"),
                    "cpu": proc.get("product", "unknown"),
                    "cores": proc.get("cores", "unknown"),
                    "memory_gb": sum(
                        m.get("size_gb", 0) for m in details.get("memory", [])
                    ),
                    "matching_nics": [
                        {
                            "name": i.get("name"),
                            "vendor": i.get("vendor"),
                            "speed": i.get("speed"),
                            "mac": i.get("mac_address"),
                        }
                        for i in matched
                    ],
                })

        return results

    async def create_assignment(
        self, description: str, owner: str | None = None
    ) -> dict[str, Any]:
        r = await self._authed_request(
            "POST",
            "/api/v3/assignments/self",
            json={
                "description": description,
                "owner": owner or self.owner,
                "qinq": 0,
                "wipe": "true",
            },
        )
        data = r.json()
        return {
            "id": data["id"],
            "cloud_name": data["cloud"]["name"],
            "ticket": data.get("ticket"),
        }

    async def schedule_host(
        self, cloud_name: str, hostname: str
    ) -> dict[str, Any]:
        r = await self._authed_request(
            "POST",
            "/api/v3/schedules",
            json={"cloud": cloud_name, "hostname": hostname},
        )
        data = r.json()
        return {
            "hostname": hostname,
            "cloud": cloud_name,
            "end": data.get("end", "unknown"),
        }

    async def get_assignment_status(
        self, assignment_id: int
    ) -> dict[str, Any]:
        r = await self._client.get(
            f"http://{self.api_host}/api/v3/assignments/{assignment_id}"
        )
        r.raise_for_status()
        data = r.json()
        return {
            "id": assignment_id,
            "description": data.get("description"),
            "cloud": data.get("cloud", {}).get("name"),
            "owner": data.get("owner"),
            "validated": data.get("validated", False),
            "provisioned": data.get("provisioned", False),
        }

    async def poll_until_validated(
        self,
        assignment_id: int,
        interval: int = 120,
        timeout: int = 3600,
    ) -> dict[str, Any]:
        elapsed = 0
        while elapsed < timeout:
            status = await self.get_assignment_status(assignment_id)
            logger.info(
                f"[quads] Assignment {assignment_id}: "
                f"validated={status['validated']}, "
                f"provisioned={status['provisioned']} "
                f"({elapsed}s elapsed)"
            )
            if status["validated"]:
                return status
            await asyncio.sleep(interval)
            elapsed += interval

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

    async def setup_ssh(self, hosts: list[str]) -> dict[str, Any]:
        key_path = Path(self.ssh_key_path)
        if not key_path.exists():
            proc = await asyncio.create_subprocess_exec(
                "ssh-keygen", "-t", "ed25519",
                "-f", str(key_path), "-N", "", "-q",
                "-C", "host-key-do-not-remove",
            )
            await proc.wait()

        pubkey_path = Path(f"{self.ssh_key_path}.pub")
        if not pubkey_path.exists():
            return {"status": "failed", "message": f"Public key not found: {pubkey_path}"}
        pubkey = pubkey_path.read_text().strip()

        for host in hosts:
            proc = await asyncio.create_subprocess_exec(
                "ssh-keygen", "-R", host,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()

        results: dict[str, str] = {}
        for host in hosts:
            try:
                result = await self._copy_ssh_key(host, pubkey)
                results[host] = result
            except Exception as e:
                results[host] = f"failed: {e}"

        return {
            "status": "success" if all("ok" in v for v in results.values()) else "partial",
            "ssh_key_path": self.ssh_key_path,
            "hosts": results,
        }

    async def _copy_ssh_key(self, host: str, pubkey: str) -> str:
        remote_cmd = (
            f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"echo '{pubkey}' >> ~/.ssh/authorized_keys && "
            f"chmod 600 ~/.ssh/authorized_keys && echo KEY_COPIED"
        )
        ssh_cmd = (
            f"ssh -o StrictHostKeyChecking=no "
            f"-o PreferredAuthentications=password "
            f"root@{host} \"{remote_cmd}\""
        )

        proc = await asyncio.create_subprocess_exec(
            "python3", "-c",
            (
                "import pexpect, sys\n"
                f"child = pexpect.spawn('/bin/bash', ['-c', {ssh_cmd!r}], timeout=30)\n"
                "child.expect('[Pp]assword')\n"
                f"child.sendline({self.default_root_password!r})\n"
                "child.expect(pexpect.EOF)\n"
                "out = child.before.decode()\n"
                "print('ok' if 'KEY_COPIED' in out else f'unexpected: {out}')\n"
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode().strip()
        if "ok" in output:
            return "ok"
        return f"failed: {output} {stderr.decode().strip()}"
