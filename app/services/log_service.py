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
    """Streams journalctl output for exactly one unit at a time."""

    def __init__(self, ssh: SSHService, broadcaster: Broadcaster, settings: Settings | None = None):
        self.ssh = ssh
        self.broadcaster = broadcaster
        self.settings = settings or default_settings
        self._thread: threading.Thread | None = None
        self._channel = None
        self._stop = threading.Event()
        self._unit: str | None = None

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def unit(self) -> str | None:
        return self._unit

    async def start(self, unit_name: str) -> None:
        unit_name = validate_unit_name(unit_name)
        await self.stop()

        if not self.ssh.connected:
            await self.broadcaster.log(
                f"Live logs unavailable: not connected to the app server (would run "
                f"journalctl -u {unit_name} -f)",
                level="warn",
            )
            return

        argv = ["journalctl", "-u", unit_name, "-n", str(self.settings.log_tail_lines), "-f", "--no-pager"]
        try:
            channel = self.ssh.open_log_channel(argv)
        except SSHError as exc:
            await self.broadcaster.log(f"Could not start live logs: {exc}", level="error")
            return

        self._channel = channel
        self._unit = unit_name
        self._stop.clear()
        loop = asyncio.get_running_loop()
        self._thread = threading.Thread(
            target=self._pump, args=(channel, loop, unit_name), name=f"journal-{unit_name}", daemon=True
        )
        self._thread.start()
        await self.broadcaster.log(f"Streaming journalctl -u {unit_name} -f")

    def _pump(self, channel, loop: asyncio.AbstractEventLoop, unit_name: str) -> None:
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
                        self._emit(loop, line.rstrip("\r"))
                elif channel.exit_status_ready():
                    break
                else:
                    self._stop.wait(0.2)
        except Exception as exc:  # noqa: BLE001 - background thread must not die silently
            logger.warning("Log stream for %s ended: %s", unit_name, exc)
            self._emit(loop, f"[log stream ended: {exc}]")
        finally:
            try:
                channel.close()
            except Exception:
                pass

    def _emit(self, loop: asyncio.AbstractEventLoop, line: str) -> None:
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
            self.broadcaster.publish({"type": "journal", "message": line}), loop
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:
                pass
            self._channel = None
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, 2.0)
            self._thread = None
        self._unit = None

    async def recent(self, unit_name: str, lines: int | None = None) -> str:
        """One-shot tail without following, used for failure diagnostics."""
        unit_name = validate_unit_name(unit_name)
        count = lines or self.settings.log_tail_lines
        result = await self.ssh.run(
            ["journalctl", "-u", unit_name, "-n", str(count), "--no-pager"], sudo=True, check=False
        )
        return result.stdout or result.stderr
