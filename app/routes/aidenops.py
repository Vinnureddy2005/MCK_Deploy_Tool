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
from app.services import aidenops_logs
from app.services.aidenops_logs import AidenOpsLogStreamer
from app.services.aidenops_release import ReleaseError, ReleaseStager, extract_member
from app.services.download_service import DownloadError, DownloadService
from app.services.deployment_service import deployment_service
from app.services.ssh_service import CommandFailed, SSHError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/aidenops", tags=["aidenops"])

class VerifyRequest(BaseModel):
    # The filename is not a parameter. There is one bundle name, so accepting a
    # name would only add a way to point this at the wrong file.
    checksum: str = Field(..., max_length=512)


class DeployRequest(BaseModel):
    target: str = Field(..., max_length=16)
    # A destructive migration or a dependency change needs an explicit decision.
    # The client re-posts with this set after the operator has seen what changes.
    confirmed: bool = False


class LogsRequest(BaseModel):
    # Which half to follow: "ui", "backend", or absent for both. Absent is the
    # investigating case, where nothing was deployed and everything is wanted.
    target: str | None = Field(default=None, max_length=16)


def _handle(exc: Exception) -> HTTPException:
    if isinstance(exc, (ValidationError, ReleaseError, DownloadError,
                        config_validator.ConfigError)):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, (FrontendError, SSHError, CommandFailed)):
        return HTTPException(status_code=502, detail=str(exc))
    logger.exception("Unhandled AidenOps API error")
    return HTTPException(status_code=500, detail="Unexpected error. Check the application log.")


@router.get("/status")
async def status() -> dict:
    """What has been verified so far, and what this server expects."""
    release = aidenops_release.current()
    # The streams are server-side and outlive the page: `journalctl -f` and
    # `tail -F` from an earlier deployment keep running for the life of the
    # process, so a fresh page - or a reload, or a second tab - starts receiving
    # lines without anything having been clicked. Reported here so the page can
    # say where they came from instead of looking as though it deployed
    # something on its own.
    streaming = sorted(_streamer().streaming)
    return {
        "release": release.public() if release else None,
        "streaming": streaming,
        "dry_run": config.settings.dry_run,
        "server": config.settings.ssh_host,
        "paths": {
            "ops_dir": config.settings.aidenops_ops_dir,
            "web_root": config.settings.aidenops_web_root,
            "staging": config.settings.aidenops_staging_dir,
            "unit": config.settings.aidenops_unit,
        },
    }


@router.get("/bundle")
async def bundle() -> dict:
    """The release bundle waiting to be verified, if it is there.

    Reported rather than chosen: the Aiden tool always publishes under one name,
    so there is nothing to pick. The size and timestamp are here so it is
    obvious whether the file was copied today or a fortnight ago.
    """
    incoming = config.settings.aidenops_incoming_dir
    incoming.mkdir(parents=True, exist_ok=True)
    path = incoming / config.settings.aidenops_bundle_name

    if not path.is_file():
        return {
            "incoming_dir": str(incoming),
            "name": config.settings.aidenops_bundle_name,
            "present": False,
        }
    stat = path.stat()
    return {
        "incoming_dir": str(incoming),
        "name": path.name,
        "present": True,
        "size": stat.st_size,
        "modified": int(stat.st_mtime),
    }


@router.post("/fetch")
async def fetch() -> dict:
    """Download the bundle from the installation hub into the incoming folder.

    The same request the JAR path makes - the hub takes a filename and a code,
    and only the filename differs. Optional: copying the file in by hand lands
    it in the same place, and everything after this point is identical either
    way.

    The hash is deliberately not checked here. This only puts bytes on disk; the
    gate is Verify, which is where the code you carried across is compared.
    """
    settings = config.settings
    incoming = settings.aidenops_incoming_dir
    destination = incoming / settings.aidenops_bundle_name

    try:
        result = await DownloadService(settings).download_bundle(destination)
    except Exception as exc:
        raise _handle(exc) from exc

    return {
        "name": result.filename,
        "size": result.size_bytes,
        "simulated": result.simulated,
        "incoming_dir": str(incoming),
    }


@router.post("/verify")
async def verify(request: VerifyRequest) -> dict:
    """Verify the bundle in the incoming folder. Nothing touches the server.

    Both checks run here on the VDI: the archive against the hand-carried hash,
    and its members against the SHA256SUMS.txt inside it. A release that fails
    is not retained, so the deploy endpoint has no path to it.
    """
    try:
        name = validate_release_archive(config.settings.aidenops_bundle_name)
        incoming = config.settings.aidenops_incoming_dir.resolve()
        candidate = (incoming / name).resolve()
        # The name comes from configuration rather than the request now, but it
        # is still resolved and re-checked: a symlink in the incoming folder
        # should not be able to point outside it.
        if candidate.parent != incoming:
            raise ValidationError(f"{name} does not resolve inside {incoming}.")
        if not candidate.is_file():
            raise ValidationError(
                f"{name} is not in {incoming}. Copy the release bundle there first."
            )

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
        state = (unit.stdout or "").strip() or "unknown"
        # Reported, never blocking. A stopped service is the situation you most
        # need this tool for.
        checks.append({"name": config.settings.aidenops_unit, "ok": True,
                       "detail": state, "info": True})

        health = await ssh.run(
            ["curl", "-sf", "-o", "/dev/null", "-w", "%{http_code}",
             config.settings.aidenops_health_url],
            sudo=False, check=False,
        )
        code = (health.stdout or "").strip()
        # Also informational. It was blocking, which refused a deployment
        # precisely when the backend was down - backwards. What the answer is
        # good for is attributing a later failure: if it was already broken
        # beforehand, the deployment did not break it. The real gate is the
        # health poll after the service starts.
        checks.append({
            "name": "backend /health",
            "ok": True,
            "info": True,
            "detail": ("200" if code == "200"
                       else f"not answering ({code or 'no response'}) - "
                            f"service is {state}"),
        })

        # Both volumes, because they are different ones and both matter: the
        # database and the dumps live on /home/AidenAI, while the UI bundle goes
        # to /var/www - and /var is the volume that has already filled once.
        margin = config.settings.aidenops_disk_margin_mb
        # Measured against directories that exist. The backup root is created by
        # the deployment itself, so df against it fails on a first run - and that
        # failure was being reported as "(dry run)", which is a different thing
        # entirely and hid it.
        for label, path in (("disk /home/AidenAI", "/home/AidenAI"),
                            ("disk /var/www", config.settings.aidenops_web_root)):
            disk = await ssh.run(["df", "-Pk", path], sudo=True, check=False)
            lines = (disk.stdout or "").strip().splitlines()
            fields = lines[-1].split() if len(lines) > 1 else []
            free_mb = (int(fields[3]) // 1024
                       if len(fields) > 3 and fields[3].isdigit() else None)

            if disk.simulated:
                detail, ok = "(dry run - not measured)", True
            elif free_mb is None:
                # Not "fine by default": a df that produced nothing is a real
                # failure, and calling it unmeasurable would hide it.
                detail, ok = f"could not measure {path}", False
            else:
                detail = f"{free_mb} MB free (need {margin} MB)"
                ok = free_mb >= margin
            checks.append({"name": label, "ok": ok, "detail": detail})
    except Exception as exc:
        raise _handle(exc) from exc

    # Informational rows never gate a deployment; only real failures do.
    return {
        "ok": all(c["ok"] for c in checks if not c.get("info")),
        "checks": checks,
    }


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
async def start_logs(request: LogsRequest | None = None) -> dict:
    """Follow the logs for one half of the application, or for both.

    Separate from a deployment on purpose: the most useful time to read these is
    while investigating, not only while deploying.

    The half matters. Following the service journal after a UI deployment says
    the backend was involved, and it was not - the frontend pipeline never
    restarts the service or touches the wheel.
    """
    target = request.target if request else None
    if target is not None and target not in aidenops_logs.HALVES:
        raise HTTPException(status_code=400, detail=f"Unknown target: {target!r}")
    try:
        await deployment_service.connect()
        streamer = _streamer()
        await streamer.start(half=target)
    except Exception as exc:
        raise _handle(exc) from exc
    streaming = streamer.streaming
    return {
        "streaming": streamer.active,
        "sources": sorted(streaming),
        # So the page knows whether to say anything about the service at all.
        "journal": "journal" in streaming,
        "unit": config.settings.aidenops_unit,
    }


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
