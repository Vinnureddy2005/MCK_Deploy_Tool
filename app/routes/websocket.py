"""WebSocket endpoint carrying deployment events and live journal output."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import settings
from app.services.deployment_service import deployment_service

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/logs")
async def logs(websocket: WebSocket) -> None:
    await websocket.accept()
    broadcaster = deployment_service.broadcaster
    queue = broadcaster.subscribe()

    try:
        await websocket.send_json(
            {
                "type": "connected",
                "dry_run": settings.dry_run,
                "host": settings.ssh_target,
                "state": deployment_service.state.to_dict(),
            }
        )
        # Replay this deployment's events so a reconnecting client is not blank.
        for event in broadcaster.history:
            await websocket.send_json(event)

        receiver = asyncio.create_task(_drain(websocket))
        try:
            while True:
                event = await queue.get()
                await websocket.send_json(event)
                if receiver.done():
                    break
        finally:
            receiver.cancel()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.info("WebSocket closed: %s", exc)
    finally:
        broadcaster.unsubscribe(queue)


async def _drain(websocket: WebSocket) -> None:
    """Consume client frames (keepalive pings) and detect disconnects."""
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        return
