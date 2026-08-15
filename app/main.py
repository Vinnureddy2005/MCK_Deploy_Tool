"""McKesson Deployment Tool.

Runs locally on the McKesson VDI laptop. FastAPI serves the dashboard and
drives the SSH/SFTP deployment against the McKesson app server.

This application has no connection of any kind to the Aiden laptop or the
Aiden deployment tool - the checksum is pasted in by the user.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import BASE_DIR, ValidationError, settings
from app.routes import deployment as deployment_routes, websocket as websocket_routes
from app.services.deployment_service import deployment_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("mckesson")

STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    logger.info("McKesson Deployment Tool starting")
    logger.info("Target server : %s (%s)", settings.ssh_target, settings.ssh_username)
    logger.info("Binaries dir  : %s", settings.remote_binaries_dir)
    if settings.dry_run:
        logger.warning("DRY_RUN is ENABLED - no server changes will be made")
    else:
        logger.warning("DRY_RUN is DISABLED - deployments will modify the app server")
    yield
    await deployment_service.shutdown()
    logger.info("Shutdown complete")


app = FastAPI(
    title="McKesson Deployment Tool",
    description="Local deployment automation for the McKesson app server.",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(deployment_routes.router)
app.include_router(websocket_routes.router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", include_in_schema=False)
async def health() -> dict:
    return {
        "status": "ok",
        "dry_run": settings.dry_run,
        "ssh_connected": deployment_service.ssh.connected,
        "deployment_status": deployment_service.state.status,
    }
