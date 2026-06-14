from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SSHResult:
    stdout: str
    stderr: str
    exit_code: int


class SSHExecutor:
    def __init__(
        self,
        user: str = "root",
        key_path: str | None = None,
        connect_timeout: int = 10,
    ) -> None:
        self.user = user
        self.key_path = key_path
        self.connect_timeout = connect_timeout

    def _ssh_args(
        self, host: str, key_path: str | None = None, allocate_pty: bool = False,
    ) -> list[str]:
        args = [
            "ssh",
            "-o", f"ConnectTimeout={self.connect_timeout}",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
        ]
        if allocate_pty:
            args.append("-tt")
        effective_key = key_path or self.key_path
        if effective_key:
            args.extend(["-i", effective_key])
        args.append(f"{self.user}@{host}")
        return args

    async def run(
        self,
        host: str,
        command: str,
        timeout: int = 300,
        key_path: str | None = None,
        allocate_pty: bool = False,
    ) -> SSHResult:
        args = self._ssh_args(host, key_path=key_path, allocate_pty=allocate_pty) + [command]
        logger.info(f"[ssh] {self.user}@{host}: {command[:120]}")

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            coro = proc.communicate()
            if timeout > 0:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(coro, timeout=timeout)
            else:
                stdout_bytes, stderr_bytes = await coro
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return SSHResult(
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                exit_code=-1,
            )

        result = SSHResult(
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            exit_code=proc.returncode or 0,
        )

        if result.exit_code != 0:
            logger.warning(
                f"[ssh] {host} exit={result.exit_code}: {result.stderr[:200]}"
            )

        return result

    async def copy_to(
        self,
        host: str,
        local_path: str,
        remote_path: str,
        timeout: int = 120,
        key_path: str | None = None,
    ) -> SSHResult:
        args = [
            "scp", "-r",
            "-o", f"ConnectTimeout={self.connect_timeout}",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
        ]
        effective_key = key_path or self.key_path
        if effective_key:
            args.extend(["-i", effective_key])
        args.extend([local_path, f"{self.user}@{host}:{remote_path}"])

        logger.info(f"[scp] {local_path} -> {self.user}@{host}:{remote_path}")

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return SSHResult(stdout="", stderr=f"SCP timed out after {timeout}s", exit_code=-1)

        return SSHResult(
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            exit_code=proc.returncode or 0,
        )
