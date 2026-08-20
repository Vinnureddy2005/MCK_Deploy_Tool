"""AidenOps API. A separate router from the TX-PROJECTS endpoints.

Kept apart end to end. The Java flow pastes a checksum and replaces one JAR;
this one verifies an archive locally and then runs one or two independent
pipelines. A shared abstraction would spend its time being told which half to
skip.

As with the other router, there is no generic command endpoint. Each route is
one specific operation, and every filename that reaches a command line is
validated first.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import config
from app.config import ValidationError, validate_release_archive
from app.services import aidenops_release, config_validator
from app.services.aidenops_backend import (
    BackendConfirmation,
    BackendDeployer,
    BackendError,
)
from app.services.aidenops_frontend import FrontendDeployer, FrontendError
from app.services.aidenops_logs import AidenOpsLogStreamer
from app.services.aidenops_release import ReleaseError, ReleaseStager, extract_member
from app.services.deployment_service import deployment_service
from app.services.ssh_service import CommandFailed, SSHError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/aidenops", tags=["aidenops"])

class VerifyRequest(BaseModel):
    archive: str = Field(..., max_length=256)
    checksum: str = Field(..., max_length=512)


class DeployRequest(BaseModel):
    target: str = Field(..., max_length=16)
    # A destructive migration or a dependency change needs an explicit decision.
    # The client re-posts with this set after the operator has seen what changes.
    confirmed: bool = False


def _handle(exc: Exception) -> HTTPException:
    if isinstance(exc, (ValidationError, ReleaseError, config_validator.ConfigError)):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, (FrontendError, SSHError, CommandFailed)):
        return HTTPException(status_code=502, detail=str(exc))
    logger.exception("Unhandled AidenOps API error")
    return HTTPException(status_code=500, detail="Unexpected error. Check the application log.")


@router.get("/status")
async def status() -> dict:
    """What has been verified so far, and what this server expects."""
    release = aidenops_release.current()
    return {
        "release": release.public() if release else None,
        "dry_run": config.settings.dry_run,
        "server": config.settings.ssh_host,
        "paths": {
            "ops_dir": config.settings.aidenops_ops_dir,
            "web_root": config.settings.aidenops_web_root,
            "staging": config.settings.aidenops_staging_dir,
            "unit": config.settings.aidenops_unit,
        },
    }


@router.get("/archives")
async def archives() -> dict:
    """Release archives waiting in the incoming folder, newest first."""
    incoming = config.settings.aidenops_incoming_dir
    incoming.mkdir(parents=True, exist_ok=True)

    found = []
    for path in sorted(incoming.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        found.append({
            "name": path.name,
            "size": path.stat().st_size,
            "modified": int(path.stat().st_mtime),
        })
    return {"incoming_dir": str(incoming), "archives": found}


@router.post("/verify")
async def verify(request: VerifyRequest) -> dict:
    """Verify an archive from the incoming folder. Nothing touches the server.

    Both checks run here on the VDI: the archive against the hand-carried hash,
    and its members against the SHA256SUMS.txt inside it. A release that fails
    is not retained, so it cannot be reached by the deploy endpoint.
    """
    try:
        name = validate_release_archive(request.archive)
        incoming = config.settings.aidenops_incoming_dir.resolve()
        candidate = (incoming / name).resolve()
        # Belt and braces: the name is already validated, but resolving and
        # re-checking the parent means no symlink or edge case escapes the folder.
        if candidate.parent != incoming:
            raise ValidationError(f"{name} is not in the incoming folder.")
        if not candidate.is_file():
            raise ValidationError(f"{name} is not in {incoming}.")

        release = aidenops_release.verify(candidate, request.checksum)
    except Exception as exc:
        raise _handle(exc) from exc

    return {"release": release.public()}


@router.post("/preflight")
async def preflight() -> dict:
    """Read-only checks against the server, before anything is sent.

    config.yaml is read here and validated in the tool: the parse is what makes
    placeholder detection reliable, and doing it locally needs no PyYAML on the
    server and no assumptions about its locale.
    """
    ssh = deployment_service.ssh
    checks: list[dict] = []

    try:
        await deployment_service.connect()

        text = await _read(ssh, f"{config.settings.aidenops_ops_dir}/config.yaml")
        if text is None:
            checks.append({"name": "config.yaml", "ok": False,
                           "detail": "could not be read"})
        else:
            result = config_validator.validate(text)
            checks.append({
                "name": "config.yaml",
                "ok": result["ok"],
                "detail": ("placeholders that must be fixed: " + ", ".join(result["stop"]))
                if result["stop"] else "no blocking placeholders",
                "warn": result["warn"],
            })

        unit = await ssh.run(["systemctl", "is-active", config.settings.aidenops_unit],
                             sudo=True, check=False)
        checks.append({"name": config.settings.aidenops_unit, "ok": True,
                       "detail": (unit.stdout or "").strip() or "unknown"})

        health = await ssh.run(
            ["curl", "-sf", "-o", "/dev/null", "-w", "%{http_code}",
             config.settings.aidenops_health_url],
            sudo=False, check=False,
        )
        code = (health.stdout or "").strip()
        checks.append({"name": "backend /health", "ok": code in ("200", ""),
                       "detail": code or "(dry run)"})

        disk = await ssh.run(["df", "-Pk", config.settings.aidenops_backup_root], sudo=True,
                             check=False)
        checks.append({"name": "disk", "ok": disk.exit_code == 0,
                       "detail": (disk.stdout or "").strip().splitlines()[-1]
                       if (disk.stdout or "").strip() else "(dry run)"})
    except Exception as exc:
        raise _handle(exc) from exc

    return {"ok": all(c["ok"] for c in checks), "checks": checks}


async def _read(ssh, path: str) -> str | None:
    result = await ssh.run(["cat", path], sudo=True, check=False)
    if result.exit_code != 0 or result.simulated:
        return None
    return result.stdout


@router.post("/deploy")
async def deploy(request: DeployRequest) -> dict:
    """Deploy one half of a verified release.

    Split by target rather than doing both in one call: the UI ships without the
    backend and the reverse, and the two have opposite failure behaviour - this
    one reverts itself, the backend will stop and hand over.
    """
    release = aidenops_release.current()
    if release is None:
        raise HTTPException(
            status_code=409,
            detail="No verified release. Verify an archive first.",
        )

    if request.target not in ("ui", "backend"):
        raise HTTPException(status_code=400, detail=f"Unknown target: {request.target}")

    has = release.contents.get("has_ui" if request.target == "ui" else "has_backend")
    if not has:
        part = "UI bundle" if request.target == "ui" else "backend wheel"
        raise HTTPException(status_code=400, detail=f"This release contains no {part}.")

    ssh = deployment_service.ssh
    broadcaster = deployment_service.broadcaster
    try:
        await deployment_service.connect()

        if not release.staged_path:
            stager = ReleaseStager(ssh, config.settings, emit=broadcaster.log)
            await stager.stage(release)

        if request.target == "ui":
            deployer = FrontendDeployer(ssh, config.settings, emit=broadcaster.log)
            result = await deployer.deploy(release.contents["ui"])
            return {"target": "ui", "result": result}

        # The wheel is read locally for the migration scan; only the current
        # revision comes from the server.
        wheel = extract_member(release, release.contents["wheel"], config.settings.temp_dir)
        deployer = BackendDeployer(ssh, config.settings, emit=broadcaster.log)
        result = await deployer.deploy(
            release.contents["wheel"],
            release.contents.get("requirements"),
            wheel,
            confirmed=request.confirmed,
        )
        return {"target": "backend", "result": result}

    except BackendConfirmation as exc:
        # 409, not an error: the deployment is paused awaiting a decision, and
        # the client re-posts with confirmed once the operator has seen what
        # would change.
        raise HTTPException(
            status_code=409,
            detail={"needs_confirmation": True, "reason": exc.reason, **exc.detail},
        ) from exc
    except BackendError as exc:
        # past_the_line means the service was started, so recovery needs the
        # database and the runbook goes with the failure.
        raise HTTPException(
            status_code=502,
            detail={
                "message": str(exc),
                "stage": exc.stage,
                "past_the_line": exc.past_the_line,
                "runbook": exc.runbook,
            },
        ) from exc
    except FrontendError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"{exc} (stage: {exc.stage}, reverted: {exc.reverted})",
        ) from exc
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/logs/start")
async def start_logs() -> dict:
    """Follow the AidenOps journal and both nginx logs.

    Separate from a deployment on purpose: the most useful time to read these is
    while investigating, not only while deploying.
    """
    try:
        await deployment_service.connect()
        streamer = _streamer()
        await streamer.start()
    except Exception as exc:
        raise _handle(exc) from exc
    return {"streaming": streamer.active, "unit": config.settings.aidenops_unit}


@router.post("/logs/stop")
async def stop_logs() -> dict:
    await _streamer().stop()
    return {"streaming": False}


@router.get("/logs/recent")
async def recent_logs(lines: int = 200) -> dict:
    """A one-shot tail, for after a failure rather than during a deployment."""
    try:
        await deployment_service.connect()
        text = await _streamer().recent(lines=max(1, min(lines, 2000)))
    except Exception as exc:
        raise _handle(exc) from exc
    return {"text": text}


_log_streamer: AidenOpsLogStreamer | None = None


def _streamer() -> AidenOpsLogStreamer:
    """One streamer for the process, so a second start replaces the first
    rather than leaving orphaned channels following the same files."""
    global _log_streamer
    if _log_streamer is None:
        _log_streamer = AidenOpsLogStreamer(
            deployment_service.ssh, deployment_service.broadcaster, config.settings
        )
    return _log_streamer


@router.post("/clear")
async def clear() -> dict:
    """Forget the verified release, so the next deployment starts from scratch."""
    aidenops_release.clear()
    return {"release": None}
