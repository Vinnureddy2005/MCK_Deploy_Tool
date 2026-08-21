"""Live logs for AidenOps.

Three sources, not two, and for a specific reason each:

  journalctl -u aidenops    the app writes to stdout only - there is no file log
                            the way the Java services have one
  nginx error.log           both real UI failures on this server were diagnosed
                            here and were invisible in the AidenOps journal
  nginx access.log          confirms requests are actually reaching the bundle,
                            which a 200 from one curl does not

Which of the three are attached depends on what was deployed. A UI deployment
does not touch the service, so following its journal during one only makes the
page look as though it did; a backend deployment is the only thing that earns
those lines. Streaming is additive - deploying the UI and then the backend
leaves the nginx logs running rather than replacing them.

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
import threading

from app.config import Settings
from app.config import settings as default_settings
from app.services.log_service import Broadcaster, LogStreamer
from app.services.ssh_service import SSHService

logger = logging.getLogger(__name__)

# Shape check only. These values come from configuration rather than a request,
# but they reach a command line, so they are checked at that boundary anyway.
_UNIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]*\.service$")
_LOG_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")


# Which sources belong to which half of the application. The UI is served by
# nginx from a directory; the backend is a service that writes to stdout. So a
# UI deployment can be read entirely from the nginx logs and a backend one
# entirely from the journal.
HALVES: dict[str, tuple[str, ...]] = {
    "ui": ("nginx-error", "nginx-access"),
    "backend": ("journal",),
}


class AidenOpsLogStreamer(LogStreamer):
    """Streams the AidenOps journal and the nginx logs, per half."""

    def __init__(self, ssh: SSHService, broadcaster: Broadcaster,
                 settings: Settings | None = None):
        super().__init__(ssh, broadcaster, settings or default_settings)
        # The base class keeps threads but not what they are streaming, and its
        # stop flag is shared by all of them, so a second start() cannot stop
        # one stream and keep another. Keeping the threads by source name here
        # means a later start() can add what is missing and leave the rest
        # alone - and can tell the difference between a source that is running
        # and one whose stream has since died.
        self._threads: dict[str, threading.Thread] = {}

    @property
    def streaming(self) -> set[str]:
        """The sources actually being followed right now.

        Derived from the threads rather than remembered, so a stream that ended
        on its own - the service restarted under it, the channel dropped - stops
        counting as running and a later start() brings it back.
        """
        return {source for source, thread in self._threads.items() if thread.is_alive()}

    def _sources(self, want: set[str] | None = None) -> list[tuple[str, list[str], str]]:
        """(source, argv, label) for each stream, validated.

        `want` restricts the result to those source names; None means all.
        """
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

        if want is None:
            return sources
        return [entry for entry in sources if entry[0] in want]

    async def start(self, unit_name: str | None = None, log_path: str | None = None,
                    *, half: str | None = None) -> None:
        """Attach the streams for one half of the application.

        `half` is "ui", "backend", or None for everything - None is what the
        investigate-without-deploying case wants. The first two positional
        arguments exist only so the signature matches the base class; they are
        ignored, because what to stream is derived from the half, not chosen by
        the caller.

        Additive: sources already streaming are left alone rather than being
        stopped and restarted, which would re-print their last N lines and make
        it look as though something had happened twice.
        """
        if half is not None and half not in HALVES:
            raise ValueError(f"Unknown half: {half!r}")

        running = self.streaming
        wanted = set(HALVES[half]) if half else {name for names in HALVES.values() for name in names}
        missing = wanted - running
        if not missing:
            return

        # The stop flag is shared by every stream, so it can only be cleared
        # when nothing is left alive to be stopped by it. If something is still
        # running the flag is already clear, because that stream would have
        # exited otherwise.
        if not running:
            self._stop.clear()
            self._unit = None

        if not self.ssh.connected:
            await self.broadcaster.log(
                "Live logs unavailable: not connected to the app server (would run "
                f"journalctl -u {self.settings.aidenops_unit} -f)",
                level="warn",
            )
            return

        loop = asyncio.get_running_loop()
        for source, argv, label in self._sources(missing):
            if await self._spawn(argv, source=source, loop=loop, label=label):
                # _spawn appends the thread it started, so the last entry is it.
                self._threads[source] = self._streams[-1][0]
                if source == "journal":
                    self._unit = self.settings.aidenops_unit
                await self.broadcaster.log(f"Streaming {label}")

    async def stop(self) -> None:
        await super().stop()
        self._threads.clear()

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
