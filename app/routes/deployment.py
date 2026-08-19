"""REST API.

There is deliberately no generic command-execution endpoint. Each route maps to
one specific, validated operation.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import ValidationError, get_service, jar_filename, list_services, settings
from app.services.backup_service import BackupError
from app.services.checksum_service import ChecksumError, validate_for_deployment
from app.services.deployment_service import DeploymentError, deployment_service
from app.services.download_service import DownloadError
from app.services.sftp_service import UploadError
from app.services.ssh_service import SSHError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["deployment"])

_deploy_task: asyncio.Task | None = None


class ServiceRequest(BaseModel):
    service_key: str = Field(..., max_length=64)
    version: str | None = Field(default=None, max_length=32)


class ChecksumRequest(ServiceRequest):
    checksum: str = Field(..., max_length=512)


class DeployRequest(ChecksumRequest):
    overwrite_backup: bool = False


class BackupRequest(ServiceRequest):
    overwrite: bool = False


class PortRequest(BaseModel):
    port: int


class KillRequest(BaseModel):
    pid: int
    confirmed: bool = False


def _handle(exc: Exception) -> HTTPException:
    if isinstance(exc, (ValidationError, ChecksumError)):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, (SSHError, BackupError, UploadError, DownloadError, DeploymentError)):
        return HTTPException(status_code=502, detail=str(exc))
    logger.exception("Unhandled API error")
    return HTTPException(status_code=500, detail=f"Unexpected error: {exc}")


@router.get("/services")
async def get_services() -> dict:
    return {"services": list_services()}


@router.get("/config")
async def get_config() -> dict:
    """Non-sensitive configuration for the UI. No credentials are ever returned."""
    return {
        "dry_run": settings.dry_run,
        "host": settings.ssh_target,
        "username": settings.ssh_username,
        "binaries_dir": settings.remote_binaries_dir,
        "systemd_dir": settings.remote_systemd_dir,
        "backup_layout": settings.backup_layout,
        "backup_dir": deployment_service.backups.directory(),
        "checksum_pattern": settings.checksum_pattern,
    }


@router.post("/checksum/verify")
async def verify_checksum(payload: ChecksumRequest) -> dict:
    """Verify the pasted checksum before the user commits to a deployment."""
    try:
        cfg = get_service(payload.service_key)
        value = validate_for_deployment(payload.checksum, payload.service_key)
    except (ValidationError, ChecksumError) as exc:
        raise _handle(exc) from exc
    return {
        "valid": True,
        "checksum": value,
        "length": len(value),
        "service": {
            "key": payload.service_key,
            "display_name": cfg["display_name"],
            "jar": jar_filename(payload.service_key, payload.version),
            "unit": cfg["systemd_service"],
            "port": cfg["default_port"],
        },
        "message": "Checksum format is valid and ready for deployment",
    }


@router.post("/deployment/validate")
async def validate(payload: ChecksumRequest) -> dict:
    try:
        return await deployment_service.validate(payload.service_key, payload.checksum, payload.version)
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/deployment/download")
async def download(payload: ServiceRequest) -> dict:
    try:
        return await deployment_service.download_jar(payload.service_key, payload.version)
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/deployment/connect")
async def connect() -> dict:
    try:
        return {"message": await deployment_service.connect(), "connected": deployment_service.ssh.connected}
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/deployment/backup")
async def backup(payload: BackupRequest) -> dict:
    try:
        cfg = get_service(payload.service_key)
        result = await deployment_service.backups.run(
            jar_filename(payload.service_key, payload.version),
            cfg["systemd_service"],
            overwrite=payload.overwrite,
        )
    except Exception as exc:
        raise _handle(exc) from exc
    return {"directory": result.directory, "items": result.items, "skipped": result.skipped}


@router.post("/deployment/upload")
async def upload(payload: ServiceRequest) -> dict:
    """Stage 4: upload the downloaded JAR into CopyData/<date>/ (WinSCP step)."""
    try:
        jar = jar_filename(payload.service_key, payload.version)
        local = deployment_service.downloader.target_path(jar)
        if not settings.dry_run and not local.exists():
            raise ValidationError(f"{jar} has not been downloaded yet - run download first")
        result = await deployment_service.sftp.upload_to_copydata(local, jar)
    except Exception as exc:
        raise _handle(exc) from exc
    return {
        "filename": result.filename,
        "staged_path": result.staged_path,
        "remote_path": result.remote_path,
        "size_bytes": result.size_bytes,
        "attempts": result.attempts,
        "simulated": result.simulated,
    }


@router.post("/deployment/copy-to-binaries")
async def copy_to_binaries(payload: ServiceRequest) -> dict:
    """Stage 8: copy the staged JAR from CopyData into the binaries directory."""
    try:
        jar = jar_filename(payload.service_key, payload.version)
        return await deployment_service.sftp.copy_to_binaries(jar)
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/deployment/update-checksum")
async def update_checksum(payload: ChecksumRequest) -> dict:
    try:
        return await deployment_service.update_checksum(payload.service_key, payload.checksum)
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/deployment/daemon-reload")
async def daemon_reload() -> dict:
    try:
        return {"message": await deployment_service.daemon_reload()}
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/deployment/restart")
async def restart(payload: ServiceRequest) -> dict:
    try:
        return {"message": await deployment_service.restart_service(payload.service_key)}
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/deployment/current-checksum")
async def current_checksum(service_key: str) -> dict:
    """The APP_CHECKSUM currently in the unit file on the server. Read-only."""
    try:
        return await deployment_service.get_current_checksum(service_key)
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/deployment/service-status")
async def service_status(service_key: str) -> dict:
    try:
        return await deployment_service.get_service_status(service_key)
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/deployment/status")
async def deployment_status() -> dict:
    return deployment_service.state.to_dict()


@router.get("/deployment/last")
async def last_deployment() -> dict:
    """Summary of the most recent run, surviving an app restart."""
    return {"last": deployment_service.last_deployment()}


@router.post("/deployment/deploy")
async def deploy(payload: DeployRequest) -> dict:
    """Start the full pipeline. Progress is delivered over /ws/logs."""
    global _deploy_task
    if _deploy_task and not _deploy_task.done():
        raise HTTPException(status_code=409, detail="A deployment is already running")
    try:
        await deployment_service.validate(payload.service_key, payload.checksum, payload.version)
    except Exception as exc:
        raise _handle(exc) from exc

    _deploy_task = asyncio.create_task(
        deployment_service.deploy(
            payload.service_key, payload.checksum, payload.version, payload.overwrite_backup
        )
    )
    return {"started": True, "dry_run": settings.dry_run, "message": "Deployment started - watch the live log"}


@router.post("/deployment/find-port-process")
async def find_port_process(payload: PortRequest) -> dict:
    try:
        return await deployment_service.find_port_process(payload.port)
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/deployment/kill-process")
async def kill_process(payload: KillRequest) -> dict:
    """Terminate a PID. Requires confirmed=true - the UI asks the user first."""
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="Killing a process requires explicit confirmation")
    try:
        return await deployment_service.kill_process(payload.pid, payload.confirmed)
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/deployment/restart-after-kill")
async def restart_after_kill(payload: ServiceRequest) -> dict:
    try:
        return await deployment_service.restart_after_kill(payload.service_key)
    except Exception as exc:
        raise _handle(exc) from exc
