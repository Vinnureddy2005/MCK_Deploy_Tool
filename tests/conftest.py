"""Test fixtures.

Every test runs against a fake SSH transport. No test ever opens a connection
to the McKesson server, and no destructive command is executed anywhere.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Sequence

import pytest

from app import config
from app.services.ssh_service import CommandResult

VALID_CHECKSUM = "e2c4dd8ecf7d56927c3fb852684ba20672392448bbe04857a1565baaa16ad2a5"
OTHER_CHECKSUM = "5cebf705cd946065f2b018f1e719181f7eb0ed9ced56652ea74b2afbbfb450ad"

UNIT_FILE = """[Unit]
Description=AI TX Test Management
After=network.target

[Service]
User=aiden
WorkingDirectory=/home/AidenAI/binaries
Environment="APP_CHECKSUM=aaaabbbbccccddddeeeeffff00001111222233334444555566667777888899990"
Environment="SPRING_PROFILES_ACTIVE=prod"
ExecStart=/usr/bin/java -jar /home/AidenAI/binaries/tx-test-mgmt-1.6.0.jar
Restart=always

[Install]
WantedBy=multi-user.target
"""


class FakeStat:
    def __init__(self, size: int):
        self.st_size = size
        self.st_mode = 0o100644


class FakeChannel:
    def __init__(self) -> None:
        self.closed = False


class FakeSFTP:
    """In-memory SFTP that can be made to die like a real dropped socket.

    `fail_puts` makes the next N `put` calls raise, so retry and reconnect
    behaviour can be tested without a server.
    """

    def __init__(self, *, dirs: set[str] | None = None, fail_puts: int = 0, fail_with=None):
        self.files: dict[str, int] = {}
        self.dirs = dirs if dirs is not None else {"/home", "/home/day6sio", "/home/day6sio/CopyData"}
        self.fail_puts = fail_puts
        self.fail_with = fail_with or EOFError()
        self.put_calls: list[tuple[str, str]] = []
        self.renames: list[tuple[str, str]] = []
        self.removed: list[str] = []
        self.closed = False
        self.channel = FakeChannel()

    # -- paramiko surface ---------------------------------------------------

    def get_channel(self):
        return self.channel

    def stat(self, path: str):
        if path == ".":
            if self.closed or self.channel.closed:
                raise OSError("Socket is closed")
            return FakeStat(0)
        if path in self.dirs:
            return FakeStat(0)
        if path in self.files:
            return FakeStat(self.files[path])
        raise FileNotFoundError(2, "No such file", path)

    def mkdir(self, path: str) -> None:
        self.dirs.add(path)

    def put(self, local: str, remote: str, callback=None):
        self.put_calls.append((local, remote))
        if self.fail_puts > 0:
            self.fail_puts -= 1
            self.channel.closed = True  # a dead socket, as paramiko would leave it
            raise self.fail_with
        size = Path(local).stat().st_size
        if callback:
            callback(size, size)
        self.files[remote] = size
        return FakeStat(size)

    def posix_rename(self, old: str, new: str) -> None:
        if old not in self.files:
            raise OSError(2, "No such file", old)
        self.files[new] = self.files.pop(old)
        self.renames.append((old, new))

    def rename(self, old: str, new: str) -> None:
        self.posix_rename(old, new)

    def remove(self, path: str) -> None:
        self.removed.append(path)
        self.files.pop(path, None)

    def close(self) -> None:
        self.closed = True
        self.channel.closed = True


class FakeSSH:
    """Stand-in for SSHService that records commands instead of running them."""

    def __init__(
        self,
        settings,
        *,
        existing: set[str] | None = None,
        responses: dict | None = None,
        sftp: "FakeSFTP | None" = None,
    ):
        self.settings = settings
        self.connected = True
        self.commands: list[str] = []
        self.written: dict[str, str] = {}
        self.existing = existing if existing is not None else set()
        self.responses = responses or {}
        self.uploads: list[tuple[str, str]] = []
        self._sftp = sftp
        self.reconnects = 0
        self.sftp_sessions = 0
        self.sftp_closes = 0

    @property
    def offline(self) -> bool:
        """Mirrors SSHService.offline: dry-run with no connection at all."""
        return self.settings.dry_run and not self.settings.dry_run_connect

    def _key(self, argv: Sequence[str]) -> str:
        return " ".join(str(a) for a in argv)

    async def run(self, argv, *, sudo=False, stdin_data=None, timeout=None, check=True) -> CommandResult:
        argv = [str(a) for a in argv]
        key = self._key(argv)
        self.commands.append(("sudo " if sudo else "") + shlex.join(argv))

        if argv[0] == "tee" and stdin_data is not None:
            self.written[argv[1]] = stdin_data
            self.existing.add(argv[1])
            return CommandResult(key, 0, "", "")
        if argv[0] == "cp" and len(argv) >= 3:
            self.existing.add(argv[-1])
            return CommandResult(key, 0, "", "")
        if argv[0] == "mkdir":
            self.existing.add(argv[-1])
            return CommandResult(key, 0, "", "")
        if argv[0] == "test" and len(argv) == 3:
            return CommandResult(key, 0 if argv[2] in self.existing else 1, "", "")

        for prefix, result in self.responses.items():
            if key.startswith(prefix):
                if not result.ok and check:
                    from app.services.ssh_service import CommandFailed

                    raise CommandFailed(result)
                return result
        return CommandResult(key, 0, "", "")

    async def file_exists(self, path: str) -> bool:
        return path in self.existing

    async def read_file(self, path: str) -> str:
        if path in self.written:
            return self.written[path]
        return self.responses.get(f"cat {path}", CommandResult("", 0, UNIT_FILE, "")).stdout

    async def list_dir(self, path: str) -> list[str]:
        return []

    async def connect(self) -> str:
        return "connected (fake)"

    async def close(self) -> None:
        self.connected = False

    async def reset_connection(self) -> str:
        """Mirrors SSHService: drop everything and dial again."""
        self.reconnects += 1
        if self._sftp is not None:
            self._sftp.close()
            # a fresh session after reconnecting is a working one
            self._sftp.closed = False
            self._sftp.channel = FakeChannel()
        self.connected = True
        return "reconnected (fake)"

    async def ensure_sftp(self):
        if self._sftp is None:
            raise AssertionError("This test did not provide a FakeSFTP")
        if not self.connected:
            await self.reset_connection()
        if self._sftp.closed or self._sftp.channel.closed:
            self._sftp.closed = False
            self._sftp.channel = FakeChannel()
            self.sftp_sessions += 1
        return self._sftp

    async def sftp(self):
        return await self.ensure_sftp()

    async def close_sftp(self) -> None:
        self.sftp_closes += 1
        if self._sftp is not None:
            self._sftp.close()

    def open_log_channel(self, argv, sudo=True):
        raise AssertionError("Tests must not open a real log channel")


def _apply_env(monkeypatch, tmp_path, **overrides) -> config.Settings:
    base = {
        "DRY_RUN": "true",
        "DRY_RUN_CONNECT": "false",
        "TEMP_DIR": str(tmp_path / "deployments"),
        "AUDIT_LOG": str(tmp_path / "audit.log"),
        # keep every file the app writes inside the test's own directory
        "LAST_DEPLOYMENT_FILE": str(tmp_path / "last-deployment.json"),
        "INSTALLATION_HUB_URL": "http://hub.example.test:8081/api/installation-hubs/path",
        "INSTALLATION_CODE": "TEST-CODE-123",
        "SSH_KEY_PATH": str(tmp_path / "id_rsa"),
        "BACKUP_LAYOUT": "nested",
        "BACKUP_ROOT": "/home/AidenAI/binaries/backups",
        "BACKUP_DATE_FORMAT": "%b%d",
        "CHECKSUM_PATTERN": r"^[a-fA-F0-9]{64}$",
        "HEALTH_CHECK_DELAY": "0",
        "USE_SUDO": "true",
        "SUDO_PASSWORD": "",
    }
    base.update(overrides)
    for key, value in base.items():
        monkeypatch.setenv(key, value)
    return config.reload_settings()


@pytest.fixture
def dry_settings(monkeypatch, tmp_path):
    settings = _apply_env(monkeypatch, tmp_path)
    yield settings
    config.reload_settings()


@pytest.fixture
def live_settings(monkeypatch, tmp_path):
    """Settings with DRY_RUN off - only ever used together with FakeSSH."""
    settings = _apply_env(monkeypatch, tmp_path, DRY_RUN="false", DRY_RUN_CONNECT="true")
    yield settings
    config.reload_settings()


@pytest.fixture
def fake_ssh(live_settings):
    return FakeSSH(live_settings)
