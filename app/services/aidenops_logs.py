"""Live logs for AidenOps.

Three sources, not two, and for a specific reason each:

  journalctl -u aidenops    the app writes to stdout only - there is no file log
                            the way the Java services have one
  nginx error.log           both real UI failures on this server were diagnosed
                            here and were invisible in the AidenOps journal
  nginx access.log          confirms requests are actually reaching the bundle,
                            which a 200 from one curl does not

Implemented as a subclass rather than by extending LogStreamer, because that
class validates its unit against the Java service registry - aidenops.service is
not in it, and widening that validator would change the behaviour of a
deployment path that is in daily use. Nothing in the TX-PROJECTS flow is touched
by anything here.
"""

from __future__ import annotations

import asyncio
import logging
import re

from app.config import Settings
from app.config import settings as default_settings
from app.services.log_service import Broadcaster, LogStreamer
from app.services.ssh_service import SSHService

logger = logging.getLogger(__name__)

# Shape check only. These values come from configuration rather than a request,
# but they reach a command line, so they are checked at that boundary anyway.
_UNIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]*\.service$")
_LOG_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")


class AidenOpsLogStreamer(LogStreamer):
    """Streams the AidenOps journal and both nginx logs together."""

    def __init__(self, ssh: SSHService, broadcaster: Broadcaster,
                 settings: Settings | None = None):
        super().__init__(ssh, broadcaster, settings or default_settings)

    def _sources(self) -> list[tuple[str, list[str], str]]:
        """(source, argv, label) for each stream, validated."""
        settings = self.settings
        unit = settings.aidenops_unit
        if not _UNIT.match(unit):
            raise ValueError(f"Refusing to stream an unsafe unit name: {unit!r}")

        tail = str(settings.log_tail_lines)
        sources: list[tuple[str, list[str], str]] = [
            (
                "journal",
                ["journalctl", "-u", unit, "-n", tail, "-f", "--no-pager"],
                f"journalctl -u {unit}",
            )
        ]

        for source, path in (
            ("nginx-error", settings.aidenops_nginx_error_log),
            ("nginx-access", settings.aidenops_nginx_access_log),
        ):
            if not path:
                continue
            if not _LOG_PATH.match(path):
                raise ValueError(f"Refusing to stream an unsafe log path: {path!r}")
            # -F, not -f: these files are rotated, and -f stops following the
            # moment logrotate moves the inode out from under it.
            sources.append((source, ["tail", "-n", tail, "-F", path], f"tail -F {path}"))
        return sources

    async def start(self, unit_name: str | None = None, log_path: str | None = None) -> None:
        """Start every stream.

        The signature matches the base class so the two are interchangeable, but
        both arguments are ignored: what to stream for AidenOps is fixed, not
        chosen per call.
        """
        await self.stop()

        if not self.ssh.connected:
            await self.broadcaster.log(
                "Live logs unavailable: not connected to the app server (would run "
                f"journalctl -u {self.settings.aidenops_unit} -f)",
                level="warn",
            )
            return

        self._stop.clear()
        self._unit = self.settings.aidenops_unit
        loop = asyncio.get_running_loop()

        for source, argv, label in self._sources():
            if await self._spawn(argv, source=source, loop=loop, label=label):
                await self.broadcaster.log(f"Streaming {label}")

    async def recent(self, unit_name: str | None = None, lines: int | None = None) -> str:
        """One-shot tail of the journal and the nginx error log.

        Used after a failure. The error log is included because a UI problem is
        usually only visible there - reading the journal alone is how a broken
        deployment looks healthy.
        """
        count = str(lines or self.settings.log_tail_lines)
        unit = self.settings.aidenops_unit

        journal = await self.ssh.run(
            ["journalctl", "-u", unit, "-n", count, "--no-pager"], sudo=True, check=False
        )
        errors = await self.ssh.run(
            ["tail", "-n", count, self.settings.aidenops_nginx_error_log],
            sudo=True, check=False,
        )
        return (
            f"--- journalctl -u {unit} ---\n{journal.stdout or journal.stderr}\n"
            f"--- {self.settings.aidenops_nginx_error_log} ---\n{errors.stdout or errors.stderr}"
        )
