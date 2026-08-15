"""SFTP transfer of exactly one JAR, mirroring the manual WinSCP step.

The JAR always goes to /home/day6sio/CopyData/<date>/ first - never straight
into the binaries directory. A later stage copies it across with sudo, which is
the PuTTY half of the manual procedure.

Uploads land on a .part file and are renamed only after the remote size has
been verified, so a dropped connection can never leave something that looks
like a finished artifact.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import paramiko

from app.config import (
    Settings,
    binaries_path,
    copydata_dir,
    remote_path,
    settings as default_settings,
    validate_jar_filename,
)
from app.services.ssh_service import CommandFailed, SSHError, SSHService

logger = logging.getLogger(__name__)

ProgressHook = Callable[[str], Awaitable[None]] | None

# One automatic retry after a dropped socket, then stop. Never loop forever.
UPLOAD_RETRIES = 1

# Errors that mean "the connection died", as opposed to "the server said no".
CONNECTION_ERRORS = (EOFError, OSError, SSHError, paramiko.SSHException)


class UploadError(RuntimeError):
    """The JAR could not be transferred."""


@dataclass
class UploadResult:
    filename: str
    staged_path: str
    remote_path: str
    size_bytes: int
    simulated: bool = False
    attempts: int = 1


class SFTPService:
    def __init__(self, ssh: SSHService, settings: Settings | None = None):
        self.ssh = ssh
        self.settings = settings or default_settings

    # -- directories --------------------------------------------------------

    async def ensure_user_dir(self, path: str) -> None:
        """mkdir -p for a directory owned by the login user (no sudo needed)."""
        if self.settings.dry_run:
            return
        sftp = await self.ssh.ensure_sftp()
        parts = [p for p in path.strip("/").split("/") if p]
        current = ""
        for part in parts:
            current = f"{current}/{part}"
            try:
                await asyncio.to_thread(sftp.stat, current)
            except FileNotFoundError:
                try:
                    await asyncio.to_thread(sftp.mkdir, current)
                    logger.info("Created remote directory %s", current)
                except OSError as exc:
                    raise UploadError(f"Could not create {current}: {exc}") from exc
            except PermissionError as exc:
                raise UploadError(f"Permission denied on {current}: {exc}") from exc

    # -- stage 4: upload into CopyData/<date>/ ------------------------------

    async def upload_to_copydata(
        self,
        local_path: Path,
        filename: str,
        *,
        progress: ProgressHook = None,
    ) -> UploadResult:
        """Upload one JAR into CopyData/<date>/ and verify it landed intact.

        This is the automated equivalent of the manual WinSCP upload. Nothing
        is written to the binaries directory here.
        """
        filename = validate_jar_filename(filename)
        staging_dir = copydata_dir()
        remote = remote_path(staging_dir, filename)
        partial = f"{remote}.part"
        destination = binaries_path(filename)

        if self.settings.dry_run:
            return UploadResult(
                filename=filename,
                staged_path=remote,
                remote_path=destination,
                size_bytes=local_path.stat().st_size if Path(local_path).exists() else 0,
                simulated=True,
            )

        local_path = Path(local_path)
        if not local_path.is_file():
            raise UploadError(f"Local JAR not found: {local_path}")
        local_size = local_path.stat().st_size

        last_error: Exception | None = None
        for attempt in range(1, UPLOAD_RETRIES + 2):
            try:
                await self.ensure_user_dir(staging_dir)
                sftp = await self.ssh.ensure_sftp()

                await self._put(sftp, local_path, partial, local_size, progress)
                await self._verify_size(partial, local_size)
                await self._promote(partial, remote)
                await self._verify_size(remote, local_size)

                await self.ssh.close_sftp()
                logger.info("Uploaded %s -> %s (%d bytes)", local_path.name, remote, local_size)
                return UploadResult(
                    filename=filename,
                    staged_path=remote,
                    remote_path=destination,
                    size_bytes=local_size,
                    attempts=attempt,
                )
            except CONNECTION_ERRORS as exc:
                last_error = exc
                logger.warning("Upload attempt %d failed: %s", attempt, exc)
                await self._discard_partial(partial)
                if attempt > UPLOAD_RETRIES:
                    break
                if progress:
                    await progress(f"Upload failed ({exc}) - reconnecting and retrying once")
                try:
                    await self.ssh.reset_connection()
                except SSHError as reconnect_error:
                    last_error = reconnect_error
                    break

        raise UploadError(f"Could not upload {filename} to {remote}: {last_error}")

    async def _put(
        self,
        sftp: paramiko.SFTPClient,
        local_path: Path,
        remote: str,
        size: int,
        progress: ProgressHook,
    ) -> None:
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

        await asyncio.to_thread(sftp.put, str(local_path), remote, _callback)

    async def _promote(self, partial: str, remote: str) -> None:
        """Rename .part to the real filename - only after the size checks pass."""
        sftp = await self.ssh.ensure_sftp()

        def _rename() -> None:
            try:
                sftp.posix_rename(partial, remote)  # atomic, replaces the target
            except (AttributeError, OSError):
                try:
                    sftp.remove(remote)
                except OSError:
                    pass
                sftp.rename(partial, remote)

        await asyncio.to_thread(_rename)

    async def _verify_size(self, remote: str, expected: int) -> None:
        sftp = await self.ssh.ensure_sftp()
        try:
            attrs = await asyncio.to_thread(sftp.stat, remote)
        except OSError as exc:
            raise UploadError(f"Uploaded file not found at {remote}: {exc}") from exc
        if attrs.st_size != expected:
            raise UploadError(
                f"Size mismatch for {remote}: expected {expected} bytes, found {attrs.st_size}. "
                "The transfer was truncated."
            )

    async def _discard_partial(self, partial: str) -> None:
        """A half-written .part file must never survive into the next attempt."""
        try:
            sftp = await self.ssh.ensure_sftp()
            await asyncio.to_thread(sftp.remove, partial)
            logger.info("Removed partial upload %s", partial)
        except Exception:
            pass  # the connection is probably gone - the retry overwrites it anyway

    # -- stage 8: CopyData -> binaries --------------------------------------

    async def copy_to_binaries(self, filename: str, expected_size: int | None = None) -> dict[str, Any]:
        """`cp CopyData/<date>/<jar> /home/AidenAI/binaries/` - the PuTTY step."""
        filename = validate_jar_filename(filename)
        source = remote_path(copydata_dir(), filename)
        destination = binaries_path(filename)

        if self.settings.dry_run:
            return {"source": source, "destination": destination, "size": expected_size or 0, "simulated": True}

        try:
            await self.ssh.run(["mkdir", "-p", self.settings.remote_binaries_dir], sudo=True)
            await self.ssh.run(["cp", source, destination], sudo=True)
        except (CommandFailed, SSHError) as exc:
            raise UploadError(f"Could not copy {source} -> {destination}: {exc}") from exc

        result = await self.ssh.run(["stat", "-c", "%s", destination], sudo=True, check=False)
        if not result.ok:
            raise UploadError(f"{destination} does not exist after the copy")
        try:
            actual = int(result.stdout.strip())
        except ValueError as exc:
            raise UploadError(f"Could not determine the size of {destination}") from exc
        if expected_size is not None and actual != expected_size:
            raise UploadError(
                f"Size mismatch for {destination}: expected {expected_size} bytes, found {actual}"
            )

        logger.info("Copied %s -> %s (%d bytes)", source, destination, actual)
        return {"source": source, "destination": destination, "size": actual}

    # -- root-owned writes --------------------------------------------------

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
