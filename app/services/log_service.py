"""Live `journalctl -u <unit> -f` streaming, and the event bus the UI reads.

Both deployment stage events and service log lines are published through one
broadcaster so a single WebSocket carries everything the dashboard renders.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime
from typing import Any

from app.config import Settings, settings as default_settings, validate_unit_name
from app.services.ssh_service import SSHError, SSHService

logger = logging.getLogger(__name__)

MAX_QUEUE = 2000
HISTORY = 500


def timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


class Broadcaster:
    """Fan-out of events to every connected WebSocket client."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._history: list[dict[str, Any]] = []

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    def clear_history(self) -> None:
        self._history.clear()

    async def publish(self, event: dict[str, Any]) -> None:
        event.setdefault("time", timestamp())
        self._history.append(event)
        if len(self._history) > HISTORY:
            del self._history[: len(self._history) - HISTORY]
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A stalled client must never block a deployment.
                self._subscribers.discard(queue)

    async def log(self, message: str, level: str = "info") -> None:
        await self.publish({"type": "log", "level": level, "message": message})

    async def stage(self, stage: str, status: str, message: str = "", **extra: Any) -> None:
        await self.publish({"type": "stage", "stage": stage, "status": status, "message": message, **extra})


class LogStreamer:
    """Streams both log sources for one service at a time.

    Two are needed: journalctl carries systemd lifecycle events, while the
    application itself writes to a file (its unit does
    `ExecStartPre=... rm -f /var/www/webdav/<service>.log`), so startup errors
    and stack traces never reach journald.
    """

    def __init__(self, ssh: SSHService, broadcaster: Broadcaster, settings: Settings | None = None):
        self.ssh = ssh
        self.broadcaster = broadcaster
        self.settings = settings or default_settings
        self._streams: list[tuple[threading.Thread, Any]] = []
        self._stop = threading.Event()
        self._unit: str | None = None

    @property
    def active(self) -> bool:
        return any(thread.is_alive() for thread, _ in self._streams)

    @property
    def unit(self) -> str | None:
        return self._unit

    async def start(self, unit_name: str, log_path: str | None = None) -> None:
        unit_name = validate_unit_name(unit_name)
        await self.stop()

        if not self.ssh.connected:
            await self.broadcaster.log(
                f"Live logs unavailable: not connected to the app server (would run "
                f"journalctl -u {unit_name} -f)",
                level="warn",
            )
            return

        self._stop.clear()
        self._unit = unit_name
        loop = asyncio.get_running_loop()
        tail = str(self.settings.log_tail_lines)

        started = await self._spawn(
            ["journalctl", "-u", unit_name, "-n", tail, "-f", "--no-pager"],
            source="journal",
            loop=loop,
            label=f"journalctl -u {unit_name}",
        )
        if started:
            await self.broadcaster.log(f"Streaming journalctl -u {unit_name} -f  (systemd)")

        if log_path:
            # -F, not -f: the unit deletes and recreates this file on every
            # restart, and -f would stop following the moment that happens.
            started = await self._spawn(
                ["tail", "-n", tail, "-F", log_path],
                source="applog",
                loop=loop,
                label=f"tail -F {log_path}",
            )
            if started:
                await self.broadcaster.log(f"Streaming {log_path}  (application)")

    async def _spawn(self, argv: list[str], *, source: str, loop, label: str) -> bool:
        try:
            channel = self.ssh.open_log_channel(argv)
        except SSHError as exc:
            await self.broadcaster.log(f"Could not start {label}: {exc}", level="error")
            return False
        thread = threading.Thread(
            target=self._pump, args=(channel, loop, source, label), name=f"log-{source}", daemon=True
        )
        thread.start()
        self._streams.append((thread, channel))
        return True

    def _pump(self, channel, loop: asyncio.AbstractEventLoop, source: str, label: str) -> None:
        buffer = ""
        try:
            while not self._stop.is_set():
                if channel.recv_ready():
                    data = channel.recv(32768).decode("utf-8", errors="replace")
                    if not data:
                        break
                    buffer += data
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        self._emit(loop, source, line.rstrip("\r"))
                elif channel.exit_status_ready():
                    break
                else:
                    self._stop.wait(0.2)
        except Exception as exc:  # noqa: BLE001 - background thread must not die silently
            logger.warning("Log stream %s ended: %s", label, exc)
            self._emit(loop, source, f"[{label} ended: {exc}]")
        finally:
            try:
                channel.close()
            except Exception:
                pass

    def _emit(self, loop: asyncio.AbstractEventLoop, source: str, line: str) -> None:
        if not line.strip():
            return
        if "[sudo] password" in line:
            return
        # Safety net: never let a credential reach the browser, whatever the
        # remote end echoes back.
        for secret in (self.settings.sudo_password, self.settings.ssh_password):
            if secret and secret in line:
                return
        asyncio.run_coroutine_threadsafe(
            self.broadcaster.publish({"type": source, "message": line}), loop
        )

    async def stop(self) -> None:
        self._stop.set()
        for thread, channel in self._streams:
            try:
                channel.close()
            except Exception:
                pass
        for thread, _ in self._streams:
            await asyncio.to_thread(thread.join, 2.0)
        self._streams = []
        self._unit = None

    async def recent(self, unit_name: str, lines: int | None = None) -> str:
        """One-shot tail without following, used for failure diagnostics."""
        unit_name = validate_unit_name(unit_name)
        count = lines or self.settings.log_tail_lines
        result = await self.ssh.run(
            ["journalctl", "-u", unit_name, "-n", str(count), "--no-pager"], sudo=True, check=False
        )
        return result.stdout or result.stderr
