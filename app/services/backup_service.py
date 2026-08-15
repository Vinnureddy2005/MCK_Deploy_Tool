"""Backups taken before anything on the server is modified.

Both the running JAR and the systemd unit file are copied into one dated
backup directory so a rollback only needs a single folder.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from app.config import (
    Settings,
    backup_dir,
    binaries_path,
    remote_path,
    settings as default_settings,
    systemd_path,
    validate_jar_filename,
    validate_unit_name,
)
from app.services.ssh_service import CommandFailed, SSHService

logger = logging.getLogger(__name__)


class BackupError(RuntimeError):
    """A backup could not be created."""


class BackupExistsError(BackupError):
    """A backup with this name already exists.

    Raised instead of silently overwriting. The caller may retry with
    overwrite=True after the user confirms.
    """


@dataclass
class BackupResult:
    directory: str
    items: list[dict[str, str]] = field(default_factory=list)
    simulated: bool = False
    skipped: list[str] = field(default_factory=list)


class BackupService:
    def __init__(self, ssh: SSHService, settings: Settings | None = None):
        self.ssh = ssh
        self.settings = settings or default_settings

    def directory(self, when: date | None = None) -> str:
        return backup_dir(when)

    async def create_backup_dir(self, when: date | None = None) -> str:
        target = self.directory(when)
        try:
            await self.ssh.run(["mkdir", "-p", target], sudo=True)
        except CommandFailed as exc:
            raise BackupError(f"Could not create backup directory {target}: {exc}") from exc
        return target

    async def _guard_existing(self, destination: str, overwrite: bool) -> None:
        if overwrite:
            return
        if await self.ssh.file_exists(destination):
            raise BackupExistsError(
                f"A backup already exists at {destination}. "
                "Confirm overwrite to continue, or deploy under a different date folder."
            )

    async def backup_jar(
        self,
        jar_name: str,
        *,
        when: date | None = None,
        overwrite: bool = False,
    ) -> dict[str, str] | None:
        """Copy the currently deployed JAR into the backup directory.

        Returns None when there is no existing JAR (first deployment).
        """
        jar_name = validate_jar_filename(jar_name)
        source = binaries_path(jar_name)
        target_dir = self.directory(when)
        destination = remote_path(target_dir, jar_name)

        # Only skip the checks when there is no connection at all. Connected
        # dry-runs still verify the file really exists - that is the point of
        # rehearsing against the real server.
        if self.ssh.offline:
            return {"type": "jar", "source": source, "destination": destination, "simulated": "true"}

        if not await self.ssh.file_exists(source):
            logger.info("No existing JAR at %s - nothing to back up", source)
            return None

        await self._guard_existing(destination, overwrite)
        try:
            await self.ssh.run(["cp", "-p", source, destination], sudo=True)
        except CommandFailed as exc:
            raise BackupError(f"Could not back up {source}: {exc}") from exc
        logger.info("Backed up %s -> %s", source, destination)
        return {"type": "jar", "source": source, "destination": destination}

    async def backup_unit_file(
        self,
        unit_name: str,
        *,
        when: date | None = None,
        overwrite: bool = False,
    ) -> dict[str, str]:
        """Copy only the selected service's unit file into the backup directory."""
        unit_name = validate_unit_name(unit_name)
        source = systemd_path(unit_name)
        target_dir = self.directory(when)
        destination = remote_path(target_dir, unit_name)

        if self.ssh.offline:
            return {"type": "unit", "source": source, "destination": destination, "simulated": "true"}

        if not await self.ssh.file_exists(source):
            raise BackupError(
                f"Unit file {source} does not exist on the server. "
                "The selected service may not be installed here."
            )

        await self._guard_existing(destination, overwrite)
        try:
            await self.ssh.run(["cp", "-p", source, destination], sudo=True)
        except CommandFailed as exc:
            raise BackupError(f"Could not back up {source}: {exc}") from exc
        logger.info("Backed up %s -> %s", source, destination)
        return {"type": "unit", "source": source, "destination": destination}

    async def run(
        self,
        jar_name: str,
        unit_name: str,
        *,
        when: date | None = None,
        overwrite: bool = False,
    ) -> BackupResult:
        directory = await self.create_backup_dir(when)
        result = BackupResult(directory=directory, simulated=self.settings.dry_run)

        jar_backup = await self.backup_jar(jar_name, when=when, overwrite=overwrite)
        if jar_backup:
            result.items.append(jar_backup)
        else:
            result.skipped.append(f"No existing {jar_name} in {self.settings.remote_binaries_dir}")

        result.items.append(await self.backup_unit_file(unit_name, when=when, overwrite=overwrite))
        return result
