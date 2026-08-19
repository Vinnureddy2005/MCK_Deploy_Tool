"""Deployment orchestration.

One deployment runs at a time. Every stage publishes its state to the
broadcaster so the dashboard pipeline and the log pane stay in sync. Any
failure in a critical stage stops the deployment - nothing continues blindly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import (
    Settings,
    ValidationError,
    binaries_path,
    copydata_dir,
    get_service,
    jar_filename,
    service_log_file,
    settings as default_settings,
    systemd_path,
    validate_checksum,
    validate_pid,
    validate_port,
    validate_unit_name,
)
from app.services.backup_service import BackupError, BackupExistsError, BackupService
from app.services.checksum_service import (
    ChecksumError,
    extract_checksum,
    replace_checksum,
    validate_for_deployment,
    verify_unit_is_expected,
)
from app.services.download_service import DownloadError, DownloadService
from app.services.log_service import Broadcaster, LogStreamer
from app.services.sftp_service import SFTPService, UploadError
from app.services.ssh_service import CommandFailed, SSHError, SSHService

logger = logging.getLogger(__name__)

# Mirrors the manual procedure exactly:
#   download -> WinSCP into CopyData/<date>/ -> PuTTY: back up, edit checksum,
#   daemon-reload, cp into binaries, restart, check, tail logs.
# The JAR is staged and verified BEFORE anything on the server is modified.
STAGES = [
    "validate",
    "download",
    "connect",
    "upload_to_copydata",
    "backup",
    "update_checksum",
    "daemon_reload",
    "copy_to_binaries",
    "restart",
    "health_check",
    "live_logs",
]

WAITING, RUNNING, COMPLETED, FAILED, SKIPPED = "waiting", "running", "completed", "failed", "skipped"


class DeploymentError(RuntimeError):
    """A deployment stage failed and the deployment must stop."""

    def __init__(self, stage: str, message: str, detail: str = ""):
        self.stage = stage
        self.detail = detail
        super().__init__(message)


@dataclass
class DeploymentState:
    deployment_id: str = ""
    service_key: str = ""
    display_name: str = ""
    version: str = ""
    jar: str = ""
    unit: str = ""
    port: int | None = None
    checksum: str = ""
    previous_checksum: str = ""
    dry_run: bool = True
    status: str = "idle"  # idle | running | success | failed | awaiting_confirmation
    current_stage: str = ""
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    error_stage: str = ""
    backup_dir: str = ""
    staged_path: str = ""
    uploaded_size: int = 0
    port_conflict: dict[str, Any] | None = None
    stages: dict[str, dict[str, str]] = field(
        default_factory=lambda: {s: {"status": WAITING, "message": ""} for s in STAGES}
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "deployment_id": self.deployment_id,
            "service_key": self.service_key,
            "display_name": self.display_name,
            "version": self.version,
            "jar": self.jar,
            "unit": self.unit,
            "port": self.port,
            "checksum": self.checksum,
            "previous_checksum": self.previous_checksum,
            "dry_run": self.dry_run,
            "status": self.status,
            "current_stage": self.current_stage,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "error_stage": self.error_stage,
            "backup_dir": self.backup_dir,
            "staged_path": self.staged_path,
            "uploaded_size": self.uploaded_size,
            "port_conflict": self.port_conflict,
            "stages": self.stages,
            "stage_order": STAGES,
        }


class DeploymentService:
    """Holds the SSH session, the event bus and the current deployment state."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or default_settings
        self.ssh = SSHService(self.settings)
        self.broadcaster = Broadcaster()
        self.streamer = LogStreamer(self.ssh, self.broadcaster, self.settings)
        self.downloader = DownloadService(self.settings)
        self.sftp = SFTPService(self.ssh, self.settings)
        self.backups = BackupService(self.ssh, self.settings)
        self.state = DeploymentState(dry_run=self.settings.dry_run)
        self._lock = asyncio.Lock()
        self._local_jar: Path | None = None
        self._counter = 0

    # -- event helpers ------------------------------------------------------

    async def _log(self, message: str, level: str = "info") -> None:
        await self.broadcaster.log(message, level=level)
        self._audit(f"{level.upper()} {message}")

    async def _set_stage(self, stage: str, status: str, message: str = "") -> None:
        if stage in self.state.stages:
            self.state.stages[stage] = {"status": status, "message": message}
        if status == RUNNING:
            self.state.current_stage = stage
        await self.broadcaster.stage(stage, status, message, state=self.state.to_dict())

    def _record_last_deployment(self) -> None:
        """Persist a summary of the run that just finished.

        Written on success and on failure, so the dashboard can show what
        happened last even after the app is restarted.
        """
        record = {
            "deployment_id": self.state.deployment_id,
            "service_key": self.state.service_key,
            "display_name": self.state.display_name,
            "jar": self.state.jar,
            "unit": self.state.unit,
            "checksum": self.state.checksum,
            "previous_checksum": self.state.previous_checksum,
            "status": self.state.status,
            "error": self.state.error,
            "error_stage": self.state.error_stage,
            "started_at": self.state.started_at,
            "finished_at": self.state.finished_at,
            "dry_run": self.state.dry_run,
            "backup_dir": self.state.backup_dir,
        }
        try:
            path = self.settings.last_deployment_file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not record the last deployment: %s", exc)

    def last_deployment(self) -> dict[str, Any] | None:
        """The most recent run, or None if this tool has not deployed yet."""
        try:
            path = self.settings.last_deployment_file
            if not path.is_file():
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Could not read the last deployment record: %s", exc)
            return None

    def _audit(self, line: str) -> None:
        try:
            path = self.settings.audit_log
            path.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().isoformat(timespec="seconds")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{stamp} [{self.state.deployment_id or '-'}] {line}\n")
        except OSError:
            pass

    # -- individual operations (also exposed as REST endpoints) -------------

    async def validate(self, service_key: str, checksum: str, version: str | None = None) -> dict[str, Any]:
        """Stage 1. Validates service, checksum, JAR mapping and configuration."""
        checks: list[dict[str, str]] = []
        cfg = get_service(service_key)
        checks.append({"name": "Service", "detail": cfg["display_name"], "ok": "true"})

        value = validate_for_deployment(checksum, service_key)
        checks.append({"name": "Checksum", "detail": f"{value[:12]}... ({len(value)} chars)", "ok": "true"})

        jar = jar_filename(service_key, version)
        checks.append({"name": "JAR", "detail": jar, "ok": "true"})

        unit = validate_unit_name(cfg["systemd_service"])
        checks.append({"name": "Systemd unit", "detail": unit, "ok": "true"})

        if not self.settings.dry_run:
            if not self.settings.installation_hub_url or not self.settings.installation_code:
                raise ValidationError(
                    "INSTALLATION_HUB_URL and INSTALLATION_CODE must be set in .env before deploying."
                )
            if not (self.settings.ssh_key_path or self.settings.ssh_password):
                raise ValidationError(
                    "No SSH credentials configured. Set SSH_KEY_PATH (preferred) or SSH_PASSWORD in .env."
                )
        checks.append({"name": "Configuration", "detail": "server + installation hub configured", "ok": "true"})

        return {
            "valid": True,
            "checks": checks,
            "service": {
                "key": service_key,
                "display_name": cfg["display_name"],
                "jar": jar,
                "unit": unit,
                "port": cfg["default_port"],
            },
        }

    async def download_jar(self, service_key: str, version: str | None = None) -> dict[str, Any]:
        result = await self.downloader.download(service_key, version)
        self._local_jar = result.path
        return {
            "filename": result.filename,
            "path": str(result.path),
            "size_mb": result.size_mb,
            "sha256": result.sha256,
            "simulated": result.simulated,
        }

    async def connect(self) -> str:
        return await self.ssh.connect()

    async def get_service_status(self, service_key: str) -> dict[str, Any]:
        cfg = get_service(service_key)
        unit = validate_unit_name(cfg["systemd_service"])
        active = await self.ssh.run(["systemctl", "is-active", unit], sudo=True, check=False)
        status = await self.ssh.run(["systemctl", "status", unit, "--no-pager"], sudo=True, check=False)
        return {
            "unit": unit,
            "active": active.stdout.strip() or active.stderr.strip(),
            "is_running": active.stdout.strip() == "active",
            "status": status.stdout or status.stderr,
        }

    async def daemon_reload(self) -> str:
        await self.ssh.run(["systemctl", "daemon-reload"], sudo=True)
        return "systemd daemon reloaded"

    async def restart_service(self, service_key: str) -> str:
        cfg = get_service(service_key)
        unit = validate_unit_name(cfg["systemd_service"])
        await self.ssh.run(["systemctl", "restart", unit], sudo=True, timeout=180)
        return f"Restarted {unit}"

    async def update_checksum(self, service_key: str, checksum: str) -> dict[str, Any]:
        """Stage 7. Read, verify, replace, verify again, write back."""
        cfg = get_service(service_key)
        unit = validate_unit_name(cfg["systemd_service"])
        path = systemd_path(unit)
        new_value = validate_checksum(checksum)

        content = await self.ssh.read_file(path)
        if self.settings.dry_run and not content.strip():
            return {
                "unit": unit,
                "path": path,
                "previous": "",
                "new": new_value,
                "simulated": True,
            }

        verify_unit_is_expected(content, unit)
        updated, previous = replace_checksum(content, new_value)
        if previous == new_value:
            await self._log(f"APP_CHECKSUM in {unit} is already {new_value[:12]}... - no change needed")
            return {"unit": unit, "path": path, "previous": previous, "new": new_value, "unchanged": True}

        await self.sftp.write_remote_file(path, updated)

        if not self.settings.dry_run:
            written = await self.ssh.read_file(path)
            if extract_checksum(written) != new_value:
                raise ChecksumError(
                    f"Wrote {path} but the checksum did not persist. "
                    f"Restore from the backup in {self.state.backup_dir or 'the backup directory'}."
                )
        return {"unit": unit, "path": path, "previous": previous, "new": new_value}

    async def get_current_checksum(self, service_key: str) -> dict[str, Any]:
        """Read the APP_CHECKSUM currently in the unit file. Read-only."""
        cfg = get_service(service_key)
        unit = validate_unit_name(cfg["systemd_service"])
        path = systemd_path(unit)

        if self.ssh.offline:
            return {
                "unit": unit,
                "path": path,
                "checksum": "",
                "found": False,
                "message": "Offline dry-run: set DRY_RUN_CONNECT=true to read the server",
            }

        await self.ssh.connect()
        content = await self.ssh.read_file(path)
        if not content.strip():
            return {
                "unit": unit,
                "path": path,
                "checksum": "",
                "found": False,
                "message": f"Could not read {path}",
            }

        value = extract_checksum(content)
        return {
            "unit": unit,
            "path": path,
            "checksum": value or "",
            "found": bool(value),
            "message": "" if value else f"No APP_CHECKSUM line found in {path}",
        }

    async def find_port_process(self, port: int) -> dict[str, Any]:
        """Read-only lsof lookup. Never kills anything."""
        port = validate_port(port)
        result = await self.ssh.run(["lsof", "-i", f":{port}", "-P", "-n"], sudo=True, check=False)
        processes = self._parse_lsof(result.stdout)
        return {
            "port": port,
            "occupied": bool(processes),
            "processes": processes,
            "raw": result.stdout.strip(),
        }

    @staticmethod
    def _parse_lsof(output: str) -> list[dict[str, Any]]:
        processes: dict[int, dict[str, Any]] = {}
        for line in (output or "").splitlines():
            parts = line.split()
            if len(parts) < 9 or parts[0] == "COMMAND":
                continue
            if not re.fullmatch(r"\d+", parts[1]):
                continue
            pid = int(parts[1])
            if pid in processes:
                continue
            processes[pid] = {
                "pid": pid,
                "command": parts[0],
                "user": parts[2],
                "type": parts[4] if len(parts) > 4 else "",
                "name": parts[8],
            }
        return list(processes.values())

    async def kill_process(self, pid: int, confirmed: bool) -> dict[str, Any]:
        """Terminate a PID. Requires explicit user confirmation."""
        pid = validate_pid(pid)
        if not confirmed:
            raise ValidationError("Killing a process requires explicit confirmation")

        info = await self.ssh.run(["cat", f"/proc/{pid}/comm"], sudo=True, check=False)
        name = info.stdout.strip() or "unknown"

        await self._log(f"User confirmed termination of PID {pid} ({name})", level="warn")
        await self.ssh.run(["kill", str(pid)], sudo=True, check=False)

        if not self.settings.dry_run:
            await asyncio.sleep(2)
            still = await self.ssh.run(["test", "-d", f"/proc/{pid}"], sudo=True, check=False)
            if still.ok:
                return {
                    "pid": pid,
                    "process": name,
                    "killed": False,
                    "message": f"PID {pid} did not exit after SIGTERM. It may need manual intervention.",
                }
        self.state.port_conflict = None
        if self.state.status == "awaiting_confirmation":
            self.state.status = "running"
        return {"pid": pid, "process": name, "killed": True, "message": f"PID {pid} ({name}) terminated"}

    async def stream_logs(self, service_key: str) -> None:
        cfg = get_service(service_key)
        await self.streamer.start(cfg["systemd_service"], service_log_file(service_key))

    async def recent_app_log(self, service_key: str, lines: int = 60) -> str:
        """One-shot tail of the application's own log file. Read-only."""
        path = service_log_file(service_key)
        result = await self.ssh.run(["tail", "-n", str(lines), path], sudo=True, check=False)
        if not result.ok:
            return ""
        return result.stdout

    async def check_webdav(self) -> str:
        entries = await self.ssh.list_dir(self.settings.remote_webdav_dir)
        if not entries:
            return f"{self.settings.remote_webdav_dir}: empty or not readable"
        return f"{self.settings.remote_webdav_dir}: {len(entries)} entries"

    # -- orchestration ------------------------------------------------------

    async def deploy(
        self,
        service_key: str,
        checksum: str,
        version: str | None = None,
        overwrite_backup: bool = False,
    ) -> DeploymentState:
        if self._lock.locked():
            raise DeploymentError("validate", "A deployment is already running")
        async with self._lock:
            return await self._deploy(service_key, checksum, version, overwrite_backup)

    async def _deploy(
        self, service_key: str, checksum: str, version: str | None, overwrite_backup: bool
    ) -> DeploymentState:
        cfg = get_service(service_key)
        self._counter += 1
        self.broadcaster.clear_history()
        await self.streamer.stop()

        self.state = DeploymentState(
            deployment_id=f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{self._counter}",
            service_key=service_key,
            display_name=cfg["display_name"],
            version=version or cfg["default_version"],
            jar=jar_filename(service_key, version),
            unit=cfg["systemd_service"],
            port=cfg["default_port"],
            dry_run=self.settings.dry_run,
            status="running",
            started_at=datetime.now().isoformat(timespec="seconds"),
        )

        if self.settings.dry_run:
            await self._log("DRY RUN - no server file, service or process will be modified", level="warn")
        await self._log(f"Starting deployment {self.state.deployment_id} for {cfg['display_name']}")

        try:
            await self._stage_validate(service_key, checksum, version)
            await self._stage_download(service_key, version)
            await self._stage_connect()
            # Stage the JAR first: if this fails, the server is untouched.
            await self._stage_upload_to_copydata()
            await self._stage_backup(overwrite_backup)
            await self._stage_update_checksum(service_key)
            await self._stage_daemon_reload()
            await self._stage_copy_to_binaries()
            await self._stage_restart(service_key)
            await self._stage_health_check(service_key)
            await self._stage_live_logs(service_key)
        except DeploymentError as exc:
            await self._fail(exc.stage, str(exc), exc.detail)
            return self.state
        except (ValidationError, ChecksumError, SSHError, BackupError, UploadError, DownloadError) as exc:
            await self._fail(self.state.current_stage or "validate", str(exc))
            return self.state
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected deployment failure")
            await self._fail(self.state.current_stage or "validate", f"Unexpected error: {exc}")
            return self.state

        self.state.status = "success"
        self.state.finished_at = datetime.now().isoformat(timespec="seconds")
        self.downloader.cleanup(self._local_jar)
        self._local_jar = None
        verb = "DRY RUN COMPLETE" if self.settings.dry_run else "DEPLOYMENT SUCCESSFUL"
        # Audit only - the browser draws its own banner from the `complete` event,
        # so logging it here as well would print the line twice.
        self._audit(f"SUCCESS {verb}")
        self._record_last_deployment()
        await self.broadcaster.publish({"type": "complete", "status": "success", "state": self.state.to_dict()})
        return self.state

    async def _fail(self, stage: str, message: str, detail: str = "") -> None:
        self.state.status = "failed"
        self.state.error = message
        self.state.error_stage = stage
        self.state.finished_at = datetime.now().isoformat(timespec="seconds")
        await self._set_stage(stage, FAILED, message)
        for name in STAGES[STAGES.index(stage) + 1 :] if stage in STAGES else []:
            if self.state.stages[name]["status"] == WAITING:
                self.state.stages[name] = {"status": SKIPPED, "message": "skipped - deployment stopped"}
        await self._log(f"DEPLOYMENT STOPPED at {stage.replace('_', ' ').upper()}: {message}", level="error")
        if detail:
            await self._log(detail, level="error")
        self._record_last_deployment()
        await self.broadcaster.publish({"type": "complete", "status": "failed", "state": self.state.to_dict()})

    # -- stages -------------------------------------------------------------

    async def _stage_validate(self, service_key: str, checksum: str, version: str | None) -> None:
        await self._set_stage("validate", RUNNING, "Validating request")
        try:
            result = await self.validate(service_key, checksum, version)
        except ValidationError as exc:
            raise DeploymentError("validate", str(exc)) from exc
        for check in result["checks"]:
            await self._log(f"OK  {check['name']}: {check['detail']}")
        self.state.checksum = validate_checksum(checksum)
        await self._set_stage("validate", COMPLETED, "All checks passed")

    async def _stage_download(self, service_key: str, version: str | None) -> None:
        await self._set_stage("download", RUNNING, f"Downloading {self.state.jar}")
        if self.settings.dry_run:
            await self._log(f"Would download: {self.state.jar} from the installation hub")
            await self._set_stage("download", COMPLETED, "simulated")
            return
        try:
            result = await self.download_jar(service_key, version)
        except DownloadError as exc:
            raise DeploymentError("download", str(exc)) from exc
        await self._log(f"Downloaded {result['filename']} ({result['size_mb']} MB)")
        await self._log(f"Local SHA-256: {result['sha256']}")

        # APP_CHECKSUM is the SHA-256 of the JAR (verified against the build's
        # APP_CHECKSUM.txt, the Aiden tool, and the live server). So a mismatch
        # is never expected - it means the checksum and the JAR come from
        # different builds, and the service will fail its startup integrity
        # check. Stop here, while the server is still untouched.
        mismatch = bool(
            self.state.checksum
            and result["sha256"]
            and result["sha256"].lower() != self.state.checksum.lower()
        )
        if mismatch and self.settings.verify_jar_checksum:
            raise DeploymentError(
                "download",
                "The pasted checksum does not match the downloaded JAR.",
                f"JAR digest   : {result['sha256']}\n"
                f"Pasted value : {self.state.checksum}\n"
                "Nothing on the server has been changed. The installation hub may be "
                "serving an older build than the checksum you were given - re-check it "
                "in the Aiden application before deploying.",
            )
        if mismatch:
            await self._log(
                "VERIFY_JAR_CHECKSUM is off: the JAR does not match the pasted checksum, "
                "and the service will fail its startup integrity check.",
                level="warn",
            )
        elif result["sha256"]:
            await self._log("Checksum matches the downloaded JAR")

        await self._set_stage("download", COMPLETED, f"{result['size_mb']} MB")

    async def _stage_connect(self) -> None:
        await self._set_stage("connect", RUNNING, f"Connecting to {self.settings.ssh_target}")
        try:
            message = await self.connect()
        except SSHError as exc:
            raise DeploymentError("connect", str(exc)) from exc
        await self._log(message)
        if self.ssh.connected:
            whoami = await self.ssh.run(["id", "-un"], check=False)
            if whoami.ok:
                await self._log(f"Authenticated as {whoami.stdout.strip()}")
        await self._set_stage("connect", COMPLETED, self.settings.ssh_target)

    async def _stage_backup(self, overwrite: bool) -> None:
        await self._set_stage("backup", RUNNING, "Creating backups")
        self.state.backup_dir = self.backups.directory()
        try:
            result = await self.backups.run(self.state.jar, self.state.unit, overwrite=overwrite)
        except BackupExistsError as exc:
            raise DeploymentError(
                "backup",
                str(exc),
                "Re-run the deployment with 'Overwrite existing backup' enabled to continue.",
            ) from exc
        except BackupError as exc:
            raise DeploymentError("backup", str(exc)) from exc

        prefix = "Would create" if self.settings.dry_run else "Created"
        await self._log(f"{prefix} backup directory: {result.directory}")
        for item in result.items:
            verb = "Would back up" if self.settings.dry_run else "Backed up"
            await self._log(f"{verb} {item['type']}: {item['source']} -> {item['destination']}")
        for note in result.skipped:
            await self._log(note, level="warn")
        await self._set_stage("backup", COMPLETED, result.directory)

    async def _stage_upload_to_copydata(self) -> None:
        """Stage 4 - the WinSCP equivalent. Nothing on the server is modified yet."""
        staging = copydata_dir()
        await self._set_stage("upload_to_copydata", RUNNING, f"Uploading {self.state.jar} to {staging}")
        if self.settings.dry_run:
            await self._log(f"Would upload {self.state.jar} to {staging}/{self.state.jar}")
            await self._set_stage("upload_to_copydata", COMPLETED, "simulated")
            return
        if self._local_jar is None or not self._local_jar.exists():
            raise DeploymentError("upload_to_copydata", "Downloaded JAR is missing from the temp directory")
        try:
            result = await self.sftp.upload_to_copydata(
                self._local_jar, self.state.jar, progress=lambda m: self.broadcaster.log(m)
            )
        except UploadError as exc:
            raise DeploymentError(
                "upload_to_copydata",
                str(exc),
                "Nothing on the server was modified - the JAR never left the staging directory.",
            ) from exc

        self.state.staged_path = result.staged_path
        self.state.uploaded_size = result.size_bytes
        if result.attempts > 1:
            await self._log(f"Upload succeeded on attempt {result.attempts} after reconnecting", level="warn")
        await self._log(f"Uploaded {result.filename} -> {result.staged_path}")
        await self._log(f"Verified {result.size_bytes} bytes on the server")
        await self._set_stage("upload_to_copydata", COMPLETED, result.staged_path)

    async def _stage_copy_to_binaries(self) -> None:
        """Stage 8 - the PuTTY `cp` from CopyData into the binaries directory."""
        destination = binaries_path(self.state.jar)
        await self._set_stage("copy_to_binaries", RUNNING, f"Copying {self.state.jar} to {destination}")
        if self.settings.dry_run:
            await self._log(f"Would copy {copydata_dir()}/{self.state.jar} -> {destination}")
            await self._set_stage("copy_to_binaries", COMPLETED, "simulated")
            return
        try:
            result = await self.sftp.copy_to_binaries(self.state.jar, self.state.uploaded_size or None)
        except UploadError as exc:
            raise DeploymentError(
                "copy_to_binaries",
                str(exc),
                f"Restore from the backup in {self.state.backup_dir} if the binary is now inconsistent.",
            ) from exc
        await self._log(f"Copied {result['source']} -> {result['destination']}")
        await self._log(f"Verified {result['size']} bytes in the binaries directory")
        await self._set_stage("copy_to_binaries", COMPLETED, result["destination"])

    async def _stage_update_checksum(self, service_key: str) -> None:
        await self._set_stage("update_checksum", RUNNING, f"Updating APP_CHECKSUM in {self.state.unit}")
        if self.ssh.offline:
            await self._log(f"Would update APP_CHECKSUM in {systemd_path(self.state.unit)}")
            await self._log(f"Would set APP_CHECKSUM={self.state.checksum}")
            await self._set_stage("update_checksum", COMPLETED, "simulated")
            return
        # When connected, run the real read/verify/replace even in dry-run. The
        # write itself is still a no-op, but a missing or malformed
        # APP_CHECKSUM line is caught here instead of during a live deployment.
        try:
            result = await self.update_checksum(service_key, self.state.checksum)
        except (ChecksumError, ValidationError) as exc:
            raise DeploymentError("update_checksum", str(exc)) from exc
        self.state.previous_checksum = result.get("previous", "")
        if result.get("unchanged"):
            await self._set_stage("update_checksum", COMPLETED, "already up to date")
            return
        if self.settings.dry_run:
            await self._log(f"Found APP_CHECKSUM line in {result['path']}")
            await self._log(f"Would change {result['previous'][:12]}... -> {result['new'][:12]}...")
            await self._set_stage("update_checksum", COMPLETED, "verified, not written")
            return
        await self._log(f"APP_CHECKSUM {result['previous'][:12]}... -> {result['new'][:12]}...")
        await self._log(f"Verified new checksum in {result['path']}")
        await self._set_stage("update_checksum", COMPLETED, "checksum updated and verified")

    async def _stage_daemon_reload(self) -> None:
        await self._set_stage("daemon_reload", RUNNING, "systemctl daemon-reload")
        if self.settings.dry_run:
            await self._log("Would execute: systemctl daemon-reload")
        else:
            try:
                await self.daemon_reload()
            except (CommandFailed, SSHError) as exc:
                raise DeploymentError("daemon_reload", f"systemctl daemon-reload failed: {exc}") from exc
        await self._log("systemd daemon reloaded")
        await self._set_stage("daemon_reload", COMPLETED, "daemon-reload done")

    async def _stage_restart(self, service_key: str) -> None:
        await self._set_stage("restart", RUNNING, f"Restarting {self.state.unit}")
        if self.settings.dry_run:
            await self._log(f"Would execute: systemctl restart {self.state.unit}")
            await self._set_stage("restart", COMPLETED, "simulated")
            return
        try:
            await self.restart_service(service_key)
        except (CommandFailed, SSHError) as exc:
            await self._diagnose_port_conflict()
            raise DeploymentError("restart", f"Could not restart {self.state.unit}: {exc}") from exc
        await self._log(f"Restarted {self.state.unit}")
        await self._set_stage("restart", COMPLETED, "restart issued")

    async def _stage_health_check(self, service_key: str) -> None:
        await self._set_stage("health_check", RUNNING, "Checking service health")
        if self.settings.dry_run:
            await self._log(f"Would execute: systemctl is-active {self.state.unit}")
            await self._set_stage("health_check", COMPLETED, "simulated")
            return

        await asyncio.sleep(self.settings.health_check_delay)
        status = await self.get_service_status(service_key)
        for line in status["status"].splitlines()[:12]:
            if line.strip():
                await self._log(line.rstrip())

        if not status["is_running"]:
            await self._log(f"Service is {status['active']}", level="error")
            tail = await self.streamer.recent(self.state.unit, 60)
            for line in tail.splitlines()[-40:]:
                await self.broadcaster.publish({"type": "journal", "message": line})
            # The application logs to a file, so the actual reason it refused to
            # start (checksum rejection, port bind failure) is only in there.
            app_tail = await self.recent_app_log(service_key, 60)
            if app_tail.strip():
                await self._log(f"Last lines of {service_log_file(service_key)}:", level="error")
                for line in app_tail.splitlines()[-40:]:
                    await self.broadcaster.publish({"type": "applog", "message": line})
            await self._diagnose_port_conflict()
            raise DeploymentError(
                "health_check",
                f"{self.state.unit} failed to start (state: {status['active']})",
                f"Roll back with the backup in {self.state.backup_dir}",
            )

        await self._log("Service is running")
        try:
            await self._log(await self.check_webdav())
        except SSHError:
            pass
        await self._set_stage("health_check", COMPLETED, "service is running")

    async def _stage_live_logs(self, service_key: str) -> None:
        await self._set_stage("live_logs", RUNNING, "Attaching to journalctl")
        await self.stream_logs(service_key)
        await self._set_stage("live_logs", COMPLETED, f"streaming {self.state.unit}")

    async def _diagnose_port_conflict(self) -> None:
        """Read-only check after a failed start. Never kills anything."""
        if self.state.port is None or self.settings.dry_run:
            return
        try:
            info = await self.find_port_process(self.state.port)
        except (SSHError, ValidationError) as exc:
            logger.warning("Port diagnosis failed: %s", exc)
            return
        if not info["occupied"]:
            return

        self.state.port_conflict = info
        self.state.status = "awaiting_confirmation"
        await self._log(f"Port {info['port']} is occupied:", level="warn")
        for proc in info["processes"]:
            await self._log(
                f"  PID {proc['pid']}  {proc['command']}  user={proc['user']}  {proc['name']}", level="warn"
            )
        await self._log("No process will be killed without your confirmation.", level="warn")
        await self.broadcaster.publish({"type": "port_conflict", "conflict": info, "state": self.state.to_dict()})

    async def restart_after_kill(self, service_key: str) -> dict[str, Any]:
        """Restart + verify after the user resolved a port conflict."""
        await self._set_stage("restart", RUNNING, f"Restarting {self.state.unit}")
        try:
            await self.restart_service(service_key)
        except (CommandFailed, SSHError) as exc:
            await self._fail("restart", f"Restart after killing the process failed: {exc}")
            return self.state.to_dict()
        await self._set_stage("restart", COMPLETED, "restart issued")
        try:
            await self._stage_health_check(service_key)
            await self._stage_live_logs(service_key)
        except DeploymentError as exc:
            await self._fail(exc.stage, str(exc), exc.detail)
            return self.state.to_dict()
        self.state.status = "success"
        self.state.error = ""
        self.state.error_stage = ""
        self.state.finished_at = datetime.now().isoformat(timespec="seconds")
        await self._log("DEPLOYMENT SUCCESSFUL", level="success")
        await self.broadcaster.publish({"type": "complete", "status": "success", "state": self.state.to_dict()})
        return self.state.to_dict()

    async def shutdown(self) -> None:
        await self.streamer.stop()
        await self.ssh.close()


deployment_service = DeploymentService()
