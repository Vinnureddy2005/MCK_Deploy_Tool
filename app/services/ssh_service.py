"""SSH transport.

This module is the *only* place a remote command is executed. There is
deliberately no public "run arbitrary command" surface reachable from HTTP:
callers pass an argv list that the higher-level services build from validated,
allowlisted values.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import paramiko

from app.config import Settings, settings as default_settings

logger = logging.getLogger(__name__)


class SSHError(RuntimeError):
    """SSH connection or command failure."""


class CommandFailed(SSHError):
    def __init__(self, result: "CommandResult"):
        self.result = result
        detail = (result.stderr or result.stdout or "").strip()
        super().__init__(f"`{result.command}` exited {result.exit_code}: {detail[:400]}")


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    simulated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        return (self.stdout or self.stderr).strip()


# Commands that only read state. In dry-run these still execute, so a dry run
# exercises connectivity, permissions and file discovery for real.
READ_ONLY = {
    "ls", "cat", "stat", "test", "date", "lsof", "sha256sum", "md5sum",
    "readlink", "id", "whoami", "find", "grep", "tail", "head", "journalctl",
}
READ_ONLY_SYSTEMCTL = {"status", "is-active", "is-enabled", "show", "cat", "list-units"}


def is_read_only(argv: Sequence[str]) -> bool:
    if not argv:
        return False
    head = argv[0]
    if head == "systemctl":
        return len(argv) > 1 and argv[1] in READ_ONLY_SYSTEMCTL
    return head in READ_ONLY


class SSHService:
    """A single SSH session against the McKesson app server."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or default_settings
        self._client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None
        self._lock = asyncio.Lock()

    # -- connection ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        if self._client is None:
            return False
        transport = self._client.get_transport()
        return bool(transport and transport.is_active())

    @property
    def offline(self) -> bool:
        """True when dry-run is configured to skip the SSH connection entirely."""
        return self.settings.dry_run and not self.settings.dry_run_connect

    async def connect(self) -> str:
        if self.offline:
            return f"DRY RUN (offline): skipped SSH connection to {self.settings.ssh_target}"
        if self.connected:
            return f"Already connected to {self.settings.ssh_target}"
        await asyncio.to_thread(self._connect_blocking)
        return f"Connected to {self.settings.ssh_target} as {self.settings.ssh_username}"

    def _connect_blocking(self) -> None:
        s = self.settings
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if s.ssh_known_hosts:
            known = Path(s.ssh_known_hosts).expanduser()
            if known.exists():
                client.load_host_keys(str(known))
        if s.ssh_host_key_policy == "auto_add":
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())

        kwargs: dict = {
            "port": s.ssh_port,
            "username": s.ssh_username,
            "timeout": s.ssh_connect_timeout,
            "banner_timeout": s.ssh_connect_timeout,
            "auth_timeout": s.ssh_connect_timeout,
            "allow_agent": True,
            "look_for_keys": True,
        }
        if s.ssh_key_path:
            key_path = Path(s.ssh_key_path).expanduser()
            if not key_path.exists():
                raise SSHError(f"SSH key not found: {key_path}")
            kwargs["key_filename"] = str(key_path)
            if s.ssh_key_passphrase:
                kwargs["passphrase"] = s.ssh_key_passphrase
        elif s.ssh_password:
            kwargs["password"] = s.ssh_password
            kwargs["look_for_keys"] = False

        hosts: list[str] = [h for h in (s.ssh_host, s.ssh_address) if h]
        last: Exception | None = None
        for host in hosts:
            try:
                client.connect(host, **kwargs)
                self._client = client
                transport = client.get_transport()
                if transport is not None:
                    # Long SFTP transfers look idle to some firewalls between
                    # the VDI and the server; keepalives stop them being cut.
                    transport.set_keepalive(30)
                logger.info("SSH connected to %s", host)
                return
            except paramiko.AuthenticationException as exc:
                raise SSHError(
                    f"Authentication failed for {s.ssh_username}@{host}. "
                    "Check SSH_KEY_PATH / SSH_PASSWORD in .env."
                ) from exc
            except paramiko.SSHException as exc:
                if "not found in known_hosts" in str(exc):
                    raise SSHError(
                        f"Host key for {host} is not in known_hosts. Connect once with "
                        "PuTTY/ssh to accept it, or set SSH_HOST_KEY_POLICY=auto_add."
                    ) from exc
                last = exc
            except OSError as exc:
                last = exc
        raise SSHError(f"Could not reach {' or '.join(hosts)}: {last}") from last

    async def close(self) -> None:
        def _close() -> None:
            if self._sftp is not None:
                try:
                    self._sftp.close()
                except Exception:
                    pass
                self._sftp = None
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None

        await asyncio.to_thread(_close)

    # -- command execution --------------------------------------------------

    def _build(self, argv: Sequence[str], sudo: bool) -> str:
        command = shlex.join(argv)
        if not sudo or not self.settings.use_sudo:
            return command
        if self.settings.sudo_password:
            # -S reads the password from stdin; -p '' suppresses the prompt.
            return f"sudo -S -p '' {command}"
        return f"sudo -n {command}"

    async def run(
        self,
        argv: Sequence[str],
        *,
        sudo: bool = False,
        stdin_data: str | None = None,
        timeout: int | None = None,
        check: bool = True,
    ) -> CommandResult:
        """Execute one command.

        Mutating commands are simulated when DRY_RUN is on; read-only commands
        still run so a dry run reflects the real server state.
        """
        argv = [str(a) for a in argv]
        command = self._build(argv, sudo)
        read_only = is_read_only(argv)

        if self.settings.dry_run and not read_only:
            return CommandResult(command=command, exit_code=0, stdout="", stderr="", simulated=True)
        if self.offline:
            return CommandResult(command=command, exit_code=0, stdout="", stderr="", simulated=True)
        if not self.connected:
            raise SSHError("Not connected to the app server")

        result = await asyncio.to_thread(
            self._run_blocking, command, stdin_data, timeout or self.settings.ssh_command_timeout, sudo
        )
        if check and not result.ok:
            raise CommandFailed(result)
        return result

    def _run_blocking(self, command: str, stdin_data: str | None, timeout: int, sudo: bool) -> CommandResult:
        assert self._client is not None
        try:
            stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        except paramiko.SSHException as exc:
            raise SSHError(f"Failed to execute command: {exc}") from exc

        try:
            if sudo and self.settings.use_sudo and self.settings.sudo_password:
                stdin.write(self.settings.sudo_password + "\n")
                stdin.flush()
            if stdin_data is not None:
                stdin.write(stdin_data)
                stdin.flush()
            stdin.channel.shutdown_write()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            code = stdout.channel.recv_exit_status()
        except TimeoutError as exc:
            raise SSHError(f"Command timed out after {timeout}s: {command}") from exc

        if "sudo: a password is required" in err or "no tty present" in err:
            raise SSHError(
                "sudo requires a password. Set SUDO_PASSWORD in .env or grant NOPASSWD sudo "
                f"to {self.settings.ssh_username}."
            )
        if "Permission denied" in err:
            logger.warning("Permission denied running: %s", command)
        return CommandResult(command=command, exit_code=code, stdout=out, stderr=err)

    # -- convenience readers (all read-only) --------------------------------

    async def file_exists(self, path: str) -> bool:
        """Try without sudo first.

        Important: a sudo failure here must not be mistaken for "file absent" -
        that would make a backup silently report nothing to back up. So the
        unprivileged answer wins when it succeeds, and sudo is only a fallback.
        """
        plain = await self.run(["test", "-e", path], check=False)
        if plain.simulated:
            return False
        if plain.ok:
            return True
        result = await self.run(["test", "-e", path], sudo=True, check=False)
        if not result.ok and result.stderr and "password" in result.stderr.lower():
            raise SSHError(
                f"Cannot determine whether {path} exists: sudo is not available. "
                "Set SUDO_PASSWORD in .env or grant NOPASSWD sudo."
            )
        return result.ok

    async def read_file(self, path: str) -> str:
        """Read a remote file, without sudo when possible.

        Unit files under /etc/systemd/system are world-readable, so this works
        even when sudo needs a password we do not have.
        """
        plain = await self.run(["cat", path], check=False)
        if plain.ok and plain.stdout:
            return plain.stdout
        # A missing file must not be reported as a sudo problem.
        if "no such file" in (plain.stderr or "").lower():
            raise SSHError(
                f"{path} does not exist on the server. "
                "This service may not be installed here - check the unit name."
            )
        result = await self.run(["cat", path], sudo=True)
        return result.stdout

    async def list_dir(self, path: str) -> list[str]:
        result = await self.run(["ls", "-1", path], sudo=True, check=False)
        if not result.ok:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    # -- SFTP ---------------------------------------------------------------

    async def ensure_sftp(self) -> paramiko.SFTPClient:
        """Return an SFTP client that is actually usable.

        Never assume a cached session is alive. An SFTP channel can die while
        the transport survives - exec_command opens a fresh channel per call,
        so shell commands keep working and hide the dead channel until the next
        transfer fails with "Socket is closed".
        """
        if self.offline:
            raise SSHError("SFTP is unavailable in offline dry-run mode")

        if not self.connected:
            logger.info("SSH transport is not active - reconnecting")
            await self.close()
            await self.connect()

        if self._sftp is not None:
            usable = await asyncio.to_thread(self._sftp_usable, self._sftp)
            if not usable:
                logger.info("SFTP channel is stale - discarding it")
                await asyncio.to_thread(self._close_sftp_quietly)

        if self._sftp is None:
            if self._client is None:
                raise SSHError("Not connected to the app server")
            self._sftp = await asyncio.to_thread(self._client.open_sftp)
        return self._sftp

    # Kept as the historic name; always goes through the liveness check.
    async def sftp(self) -> paramiko.SFTPClient:
        return await self.ensure_sftp()

    @staticmethod
    def _sftp_usable(sftp: paramiko.SFTPClient) -> bool:
        try:
            channel = sftp.get_channel()
            if channel is None or channel.closed:
                return False
            sftp.stat(".")  # a cheap round-trip proves the socket still works
            return True
        except Exception:
            return False

    def _close_sftp_quietly(self) -> None:
        if self._sftp is not None:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None

    async def close_sftp(self) -> None:
        """Close the SFTP session without dropping the SSH transport."""
        await asyncio.to_thread(self._close_sftp_quietly)

    async def reset_connection(self) -> str:
        """Tear down transport and SFTP, then dial again from scratch."""
        logger.info("Resetting the SSH connection")
        await self.close()
        return await self.connect()

    # -- log streaming ------------------------------------------------------

    def open_log_channel(self, argv: Sequence[str], sudo: bool = True) -> paramiko.Channel:
        """Open a long-lived channel for `journalctl -f`. Blocking; caller reads it."""
        if not self.connected:
            raise SSHError("Not connected to the app server")
        assert self._client is not None
        transport = self._client.get_transport()
        if transport is None:
            raise SSHError("SSH transport is not available")
        channel = transport.open_session()
        # No PTY here on purpose. A PTY echoes everything written to it, so the
        # sudo password would come back as journal output and be shown in the
        # browser. `sudo -S` reads it from stdin instead, which is not echoed.
        # stderr is merged so `tail -F` messages ("has appeared; following new
        # file") reach the user instead of vanishing.
        channel.set_combine_stderr(True)
        channel.exec_command(self._build([str(a) for a in argv], sudo))
        if sudo and self.settings.use_sudo and self.settings.sudo_password:
            channel.sendall(self.settings.sudo_password + "\n")
        return channel


def iter_lines(chunks: Iterable[str]) -> Iterable[str]:
    """Split an arbitrary chunk stream into complete lines."""
    buffer = ""
    for chunk in chunks:
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line.rstrip("\r")
    if buffer.strip():
        yield buffer.rstrip("\r")
