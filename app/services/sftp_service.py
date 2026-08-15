"""SFTP upload of exactly one JAR.

Mirrors the manual WinSCP step: the file is staged into
/home/day6sio/CopyData/<date>/ (writable by the login user) and then copied
into the binaries directory with sudo. Only the selected service's JAR is ever
transferred.
"""

from __future__ import annotations

import logging
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from app.config import (
    Settings,
    binaries_path,
    copydata_dir,
    remote_path,
    settings as default_settings,
    validate_jar_filename,
)
from app.services.ssh_service import SSHError, SSHService

logger = logging.getLogger(__name__)

ProgressHook = Callable[[str], Awaitable[None]] | None


class UploadError(RuntimeError):
    """The JAR could not be uploaded."""


@dataclass
class UploadResult:
    filename: str
    staged_path: str
    remote_path: str
    size_bytes: int
    simulated: bool = False


class SFTPService:
    def __init__(self, ssh: SSHService, settings: Settings | None = None):
        self.ssh = ssh
        self.settings = settings or default_settings

    async def ensure_user_dir(self, path: str) -> None:
        """mkdir -p for a directory owned by the login user (no sudo needed)."""
        if self.settings.dry_run:
            return
        sftp = await self.ssh.sftp()
        parts = [p for p in path.strip("/").split("/") if p]
        current = ""
        import asyncio

        for part in parts:
            current = f"{current}/{part}"
            try:
                attrs = await asyncio.to_thread(sftp.stat, current)
                if not stat.S_ISDIR(attrs.st_mode):
                    raise UploadError(f"{current} exists but is not a directory")
            except FileNotFoundError:
                try:
                    await asyncio.to_thread(sftp.mkdir, current)
                except OSError as exc:
                    raise UploadError(f"Could not create {current}: {exc}") from exc
            except PermissionError as exc:
                raise UploadError(f"Permission denied creating {current}: {exc}") from exc

    async def upload_jar(
        self,
        local_path: Path,
        filename: str,
        *,
        progress: ProgressHook = None,
    ) -> UploadResult:
        """Upload one JAR and place it in the binaries directory."""
        filename = validate_jar_filename(filename)
        destination = binaries_path(filename)

        if self.settings.dry_run:
            staged = remote_path(copydata_dir(), filename) if self.settings.stage_in_copydata else destination
            return UploadResult(
                filename=filename,
                staged_path=staged,
                remote_path=destination,
                size_bytes=local_path.stat().st_size if local_path.exists() else 0,
                simulated=True,
            )

        local_path = Path(local_path)
        if not local_path.is_file():
            raise UploadError(f"Local JAR not found: {local_path}")
        local_size = local_path.stat().st_size

        if self.settings.stage_in_copydata:
            staging_dir = copydata_dir()
            await self.ensure_user_dir(staging_dir)
            staged = remote_path(staging_dir, filename)
        else:
            staged = destination

        await self._put(local_path, staged, local_size, progress)
        await self._verify_size(staged, local_size)

        if staged != destination:
            await self.ssh.run(["mkdir", "-p", self.settings.remote_binaries_dir], sudo=True)
            await self.ssh.run(["cp", staged, destination], sudo=True)
            await self._verify_size(destination, local_size)
            logger.info("Copied %s -> %s", staged, destination)

        return UploadResult(
            filename=filename,
            staged_path=staged,
            remote_path=destination,
            size_bytes=local_size,
        )

    async def _put(self, local_path: Path, remote: str, size: int, progress: ProgressHook) -> None:
        import asyncio

        sftp = await self.ssh.sftp()
        loop = asyncio.get_running_loop()
        last_pct = -1

        def _callback(transferred: int, total: int) -> None:
            nonlocal last_pct
            if not progress or not total:
                return
            pct = int(transferred * 100 / total)
            if pct >= last_pct + 10:
                last_pct = pct
                mb = transferred / (1024 * 1024)
                asyncio.run_coroutine_threadsafe(
                    progress(f"Uploading {local_path.name}: {pct}% ({mb:.1f} MB)"), loop
                )

        try:
            await asyncio.to_thread(sftp.put, str(local_path), remote, _callback)
        except PermissionError as exc:
            raise UploadError(
                f"Permission denied writing {remote}. Check that {self.settings.ssh_username} "
                "can write to the staging directory."
            ) from exc
        except OSError as exc:
            raise UploadError(f"SFTP upload of {local_path.name} failed: {exc}") from exc
        logger.info("Uploaded %s (%d bytes) -> %s", local_path.name, size, remote)

    async def _verify_size(self, remote: str, expected: int) -> None:
        result = await self.ssh.run(["stat", "-c", "%s", remote], sudo=True, check=False)
        if result.simulated:
            return
        if not result.ok:
            raise UploadError(f"Uploaded file not found at {remote}")
        try:
            actual = int(result.stdout.strip())
        except ValueError as exc:
            raise UploadError(f"Could not determine size of {remote}") from exc
        if actual != expected:
            raise UploadError(
                f"Size mismatch for {remote}: expected {expected} bytes, found {actual}. Upload was truncated."
            )

    async def write_remote_file(self, path: str, content: str) -> None:
        """Write a root-owned file via `sudo tee` (SFTP cannot write to /etc)."""
        if self.settings.dry_run:
            return
        if not content.endswith("\n"):
            content += "\n"
        try:
            await self.ssh.run(["tee", path], sudo=True, stdin_data=content)
        except SSHError as exc:
            raise UploadError(f"Could not write {path}: {exc}") from exc
