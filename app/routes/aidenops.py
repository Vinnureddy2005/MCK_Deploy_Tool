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
from app.services.aidenops_frontend import FrontendDeployer, FrontendError
from app.services.aidenops_release import ReleaseError, ReleaseStager
from app.services.deployment_service import deployment_service
from app.services.ssh_service import CommandFailed, SSHError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/aidenops", tags=["aidenops"])

class VerifyRequest(BaseModel):
    archive: str = Field(..., max_length=256)
    checksum: str = Field(..., max_length=512)


class DeployRequest(BaseModel):
    target: str = Field(..., max_length=16)


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

    if request.target == "ui":
        if not release.contents.get("has_ui"):
            raise HTTPException(status_code=400,
                                detail="This release contains no UI bundle.")
    elif request.target == "backend":
        # Deliberately explicit rather than a stub that appears to work.
        raise HTTPException(
            status_code=501,
            detail="The backend pipeline is not implemented yet. UI releases can "
                   "be deployed now; the backend needs the dump and migration "
                   "steps, which are still being built.",
        )
    else:
        raise HTTPException(status_code=400, detail=f"Unknown target: {request.target}")

    ssh = deployment_service.ssh
    broadcaster = deployment_service.broadcaster
    try:
        await deployment_service.connect()

        if not release.staged_path:
            stager = ReleaseStager(ssh, settings, emit=broadcaster.log)
            await stager.stage(release)

        deployer = FrontendDeployer(ssh, settings, emit=broadcaster.log)
        result = await deployer.deploy(release.contents["ui"])
    except FrontendError as exc:
        # A reverted failure is reported as one: the previous bundle is back, so
        # this is a failed deployment rather than a broken server.
        raise HTTPException(
            status_code=502,
            detail=f"{exc} (stage: {exc.stage}, reverted: {exc.reverted})",
        ) from exc
    except Exception as exc:
        raise _handle(exc) from exc

    return {"target": "ui", "result": result}


@router.post("/clear")
async def clear() -> dict:
    """Forget the verified release, so the next deployment starts from scratch."""
    aidenops_release.clear()
    return {"release": None}
