"""The verified release: what has been checked, and getting it onto the server.

A release is verified entirely locally first (see archive_service), and only
then staged. Keeping those apart is the point: an archive that fails
verification never reaches the server at all, so there is nothing to clean up
and nothing sitting in a staging directory waiting to be installed by hand.

The verified release is held in process. One operator deploys at a time from a
tool bound to loopback, so a shared store would be machinery without a purpose;
it becomes one the day this serves several people.
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import Settings, copydata_dir, validate_release_archive
from app.config import settings as default_settings
from app.services import archive_service
from app.services.sftp_service import SFTPService
from app.services.ssh_service import CommandFailed, SSHService

log = logging.getLogger(__name__)


class ReleaseError(RuntimeError):
    """The release could not be verified or staged."""


@dataclass
class VerifiedRelease:
    """An archive that has passed both local checks."""

    local_path: Path
    archive: str
    sha256: str
    size: int
    contents: dict[str, Any]
    manifest: dict[str, Any] | None
    members: list[dict[str, Any]] = field(default_factory=list)
    verified_at: str = ""
    staged_path: str = ""

    @property
    def commits(self) -> list[str]:
        """Commit shorts from the manifest, when the archive carries one."""
        if not self.manifest:
            return []
        return [b.get("commit_short", "?") for b in self.manifest.get("builds", [])]

    def public(self) -> dict:
        return {
            "archive": self.archive,
            "sha256": self.sha256,
            "size": self.size,
            "contents": self.contents,
            "commits": self.commits,
            "built_by": (self.manifest or {}).get("built_by"),
            "members": [
                {"name": m["name"], "size": m.get("size")} for m in self.members
            ],
            "verified_at": self.verified_at,
            "staged": bool(self.staged_path),
        }


_current: VerifiedRelease | None = None


def current() -> VerifiedRelease | None:
    return _current


def clear() -> None:
    global _current
    _current = None


def verify(local_path: str | Path, checksum: str) -> VerifiedRelease:
    """Verify an archive and hold it as the current release.

    Raises before anything is recorded, so a failed verification cannot leave a
    half-accepted release behind for a later step to pick up.
    """
    global _current

    path = Path(local_path)
    name = validate_release_archive(path.name)

    try:
        report = archive_service.inspect(path, checksum)
    except archive_service.ArchiveError as exc:
        # Deliberately not stored. A release that failed verification is not a
        # release, and must not be reachable by the deploy endpoints.
        clear()
        raise ReleaseError(str(exc)) from exc

    _current = VerifiedRelease(
        local_path=path,
        archive=name,
        sha256=report["sha256"],
        size=report["size"],
        contents=report["contents"],
        manifest=report["manifest"],
        members=report["verified_members"],
        verified_at=datetime.now().isoformat(timespec="seconds"),
    )
    log.info("Verified %s (%s)", name, report["sha256"][:12])
    return _current


def extract_member(release: VerifiedRelease, name: str, into) -> Path:
    """Extract one already-verified member to a local path.

    The migration scan needs the wheel as a file, and it reads it here rather
    than on the server: the expensive part stays off the box, and the whole
    computation is testable without one. Safe to extract because the member was
    hashed against the archive's own SHA256SUMS.txt before this point.
    """
    known = {m["name"] for m in release.members}
    if name not in known:
        raise ReleaseError(f"{name} is not a verified member of {release.archive}.")

    destination = Path(into) / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(release.local_path) as zf, destination.open("wb") as handle:
        with zf.open(name) as member:
            while chunk := member.read(1024 * 1024):
                handle.write(chunk)
    return destination


class ReleaseStager:
    """Puts a verified archive on the server and unpacks it in staging."""

    def __init__(self, ssh: SSHService, settings: Settings | None = None, emit=None):
        self.ssh = ssh
        self.settings = settings or default_settings
        self.sftp = SFTPService(ssh, self.settings)
        self._emit = emit

    async def _log(self, message: str) -> None:
        log.info("%s", message)
        if self._emit is not None:
            await self._emit(message)

    async def stage(self, release: VerifiedRelease) -> dict:
        """Upload into CopyData, then copy across into the root-owned staging.

        The same two hops the JAR flow makes, and for the same reason: SFTP
        cannot use sudo, so it can only write somewhere the SSH user owns.
        /home/AidenAI/ops1 is root-owned and closed to that account, so a direct
        upload fails with EACCES no matter what the mkdir did.

        Dated CopyData folder, matching the JAR convention - so everything that
        was ever uploaded to this server sits in one predictable place.
        """
        landing = copydata_dir()
        staging = self.settings.aidenops_staging_dir
        name = release.archive

        await self._log(f"Uploading {name} ({release.size} bytes) to {landing}")
        result = await self.sftp.upload_release(
            release.local_path, landing, name, progress=self._emit
        )

        # Hashed where it landed, before anything is copied. A transfer that
        # arrived wrong should be caught at the point it arrived.
        landed = await self._remote_sha256(f"{landing}/{name}")
        if landed and landed != release.sha256:
            raise ReleaseError(
                f"{name} uploaded but the copy on the server hashes differently.\n"
                f"  local:  {release.sha256}\n  server: {landed}\n"
                "Nothing has been copied onward. Re-run the upload."
            )
        await self._log("Upload verified against the local hash")

        await self._log(f"Copying into {staging}")
        await self._run(["mkdir", "-p", staging])
        await self._run(["cp", f"{landing}/{name}", f"{staging}/{name}"])

        await self._log("Unpacking")
        await self._run(["unzip", "-o", f"{staging}/{name}", "-d", staging])

        release.staged_path = f"{staging}/{name}"
        return {
            "landing_path": f"{landing}/{name}",
            "staged_path": release.staged_path,
            "size": result.size_bytes,
            "simulated": result.simulated,
            "server_sha256": landed,
        }

    async def _remote_sha256(self, path: str) -> str:
        """None-safe: a dry run has nothing to hash, and says so rather than
        inventing a value that would then be compared."""
        result = await self._run(["sha256sum", path])
        if result.simulated:
            return ""
        return (result.stdout or "").split(maxsplit=1)[0].strip().lower()

    async def _run(self, argv: list[str], *, sudo: bool = True):
        try:
            return await self.ssh.run(argv, sudo=sudo)
        except CommandFailed as exc:
            raise ReleaseError(f"{argv[0]} failed: {exc.result.output[:400]}") from exc
