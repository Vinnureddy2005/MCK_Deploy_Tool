"""Test fixtures.

Every test runs against a fake SSH transport. No test ever opens a connection
to the McKesson server, and no destructive command is executed anywhere.
"""

from __future__ import annotations

import shlex
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


class FakeSSH:
    """Stand-in for SSHService that records commands instead of running them."""

    def __init__(self, settings, *, existing: set[str] | None = None, responses: dict | None = None):
        self.settings = settings
        self.connected = True
        self.commands: list[str] = []
        self.written: dict[str, str] = {}
        self.existing = existing if existing is not None else set()
        self.responses = responses or {}
        self.uploads: list[tuple[str, str]] = []

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

    async def sftp(self):
        raise AssertionError("Tests must not open a real SFTP session")

    def open_log_channel(self, argv, sudo=True):
        raise AssertionError("Tests must not open a real log channel")


def _apply_env(monkeypatch, tmp_path, **overrides) -> config.Settings:
    base = {
        "DRY_RUN": "true",
        "DRY_RUN_CONNECT": "false",
        "TEMP_DIR": str(tmp_path / "deployments"),
        "AUDIT_LOG": str(tmp_path / "audit.log"),
        "INSTALLATION_HUB_URL": "http://hub.example.test:8081/api/installation-hubs/path",
        "INSTALLATION_CODE": "TEST-CODE-123",
        "SSH_KEY_PATH": str(tmp_path / "id_rsa"),
        "BACKUP_LAYOUT": "nested",
        "BACKUP_ROOT": "/home/AidenAI/binaries/backups",
        "BACKUP_DATE_FORMAT": "%Y-%m-%d",
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
