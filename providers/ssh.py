from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_PID_SENTINEL = "__PID:"
_PID_RE = re.compile(r"__PID:(\d+)$", re.MULTILINE)


def parse_pid_sentinel(stdout: str) -> int | None:
    """Extract the PID from a sentinel line like ``__PID:12345``."""
    m = _PID_RE.search(stdout)
    return int(m.group(1)) if m else None


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
        strict_host_key: str = "accept-new",
    ) -> None:
        self.user = user
        self.key_path = key_path
        self.connect_timeout = connect_timeout
        self.strict_host_key = strict_host_key

    def _ssh_args(
        self,
        host: str,
        key_path: str | None = None,
        allocate_pty: bool = False,
    ) -> list[str]:
        args = [
            "ssh",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            "-o",
            "BatchMode=yes",
            "-o",
            f"StrictHostKeyChecking={self.strict_host_key}",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
        ]
        # When strict checking is disabled (Jumpstarter
        # boards get reflashed constantly), also ignore
        # the known_hosts file. Otherwise a stale entry
        # from a previous flash causes BatchMode=yes to
        # reject the connection even with
        # StrictHostKeyChecking=no.
        if self.strict_host_key == "no":
            args.extend(["-o", "UserKnownHostsFile=/dev/null"])
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
        args = self._ssh_args(host, key_path=key_path, allocate_pty=allocate_pty) + [
            command
        ]
        logger.info(f"[ssh] {self.user}@{host}: {command[:120]}")

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            coro = proc.communicate()
            if timeout > 0:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    coro, timeout=timeout
                )
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

    async def run_with_progress(
        self,
        host: str,
        command: str,
        progress_callback: Callable[[str, int], Awaitable[None]] | None = None,
        poll_interval: int = 30,
        key_path: str | None = None,
    ) -> SSHResult:
        """Run a long-running command with periodic progress callbacks.

        Launches the command in the background on the remote host,
        polls its output file periodically, and invokes the callback
        with the last new output line and elapsed seconds. Only calls
        the callback when output has changed since the last poll.

        Returns the same SSHResult as run() with the full output.
        """
        mkd = await self.run(
            host, "mktemp -d /tmp/run-XXXXXXXX", timeout=10, key_path=key_path
        )
        if mkd.exit_code != 0 or not mkd.stdout.strip():
            return SSHResult(
                stdout=mkd.stdout or "",
                stderr=mkd.stderr or "Failed to create temp directory",
                exit_code=mkd.exit_code or 1,
            )
        run_dir = mkd.stdout.strip()
        out_file = f"{run_dir}/out"
        rc_file = f"{run_dir}/rc"

        escaped = command.replace("'", "'\\''")
        bg_cmd = (
            f"nohup sh -c '{escaped}; echo $? > {rc_file}'"
            f" > {out_file} 2>&1 & echo {_PID_SENTINEL}$!"
        )
        launch = await self.run(host, bg_cmd, timeout=30, key_path=key_path)
        pid = parse_pid_sentinel(launch.stdout or "")
        if launch.exit_code != 0 or pid is None:
            return SSHResult(
                stdout=launch.stdout or "",
                stderr=launch.stderr or "Failed to launch background command",
                exit_code=launch.exit_code or 1,
            )
        logger.info(f"[ssh] {host}: background pid={pid} for: {command[:120]}")

        last_reported = ""
        elapsed = 0

        while True:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            done_check = await self.run(
                host,
                f"test -f {rc_file}",
                timeout=5,
                key_path=key_path,
            )
            finished = done_check.exit_code == 0

            if progress_callback:
                tail = await self.run(
                    host,
                    f"tail -5 {out_file} 2>/dev/null",
                    timeout=10,
                    key_path=key_path,
                )
                lines = [ln for ln in (tail.stdout or "").splitlines() if ln.strip()]
                last_line = lines[-1] if lines else ""
                if last_line and last_line != last_reported:
                    last_reported = last_line
                    try:
                        await progress_callback(last_line, elapsed)
                    except Exception:
                        pass

            if finished:
                break

        full_output = await self.run(
            host,
            f"cat {out_file}",
            timeout=60,
            key_path=key_path,
        )
        rc_output = await self.run(
            host,
            f"cat {rc_file}",
            timeout=5,
            key_path=key_path,
        )
        exit_code = int(rc_output.stdout.strip() or "1")

        await self.run(
            host,
            f"rm -rf {run_dir}",
            timeout=5,
            key_path=key_path,
        )

        return SSHResult(
            stdout=full_output.stdout or "",
            stderr="",
            exit_code=exit_code,
        )

    async def copy_to(
        self,
        host: str,
        local_path: str,
        remote_path: str,
        timeout: int = 120,
        key_path: str | None = None,
    ) -> SSHResult:
        args = [
            "scp",
            "-r",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            "-o",
            "BatchMode=yes",
            "-o",
            f"StrictHostKeyChecking={self.strict_host_key}",
        ]
        if self.strict_host_key == "no":
            args.extend(["-o", "UserKnownHostsFile=/dev/null"])
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
            return SSHResult(
                stdout="", stderr=f"SCP timed out after {timeout}s", exit_code=-1
            )

        return SSHResult(
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            exit_code=proc.returncode or 0,
        )
