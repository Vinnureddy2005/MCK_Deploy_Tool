"""Download the selected service JAR from the installation API."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx

from app.config import (
    Settings,
    hub_filename,
    jar_filename,
    settings as default_settings,
    validate_hub_filename,
    validate_jar_filename,
)

logger = logging.getLogger(__name__)


class DownloadError(RuntimeError):
    """The JAR could not be downloaded."""


@dataclass
class DownloadResult:
    filename: str
    path: Path
    size_bytes: int
    sha256: str
    simulated: bool = False
    local: bool = False  # supplied by hand rather than downloaded

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)


class DownloadService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or default_settings

    def build_url(self, filename: str) -> str:
        """Construct the installation-hub URL. The code never leaves the backend.

        `filename` is the hub's `filename=` value - either the JAR name or a
        per-service identifier. Only this part differs between services.
        """
        s = self.settings
        if not s.installation_hub_url:
            raise DownloadError("INSTALLATION_HUB_URL is not configured in .env")
        if not s.installation_code:
            raise DownloadError("INSTALLATION_CODE is not configured in .env")
        filename = validate_hub_filename(filename)
        query = urlencode({"filename": filename, "code": s.installation_code})
        return f"{s.installation_hub_url.rstrip('/')}?{query}"

    @staticmethod
    def redact(url: str) -> str:
        """URL safe to show in logs - the installation code is stripped."""
        head, sep, _ = url.partition("code=")
        return f"{head}code=***" if sep else url

    def target_path(self, filename: str) -> Path:
        return self.settings.temp_dir / validate_jar_filename(filename)

    async def download(self, service_key: str, version: str | None = None) -> DownloadResult:
        # The JAR name is what we store locally and upload to the server; the
        # hub name is only what we ask the installation API for.
        filename = jar_filename(service_key, version)
        hub_name = hub_filename(service_key, version)
        destination = self.target_path(filename)
        destination.parent.mkdir(parents=True, exist_ok=True)

        if self.settings.dry_run:
            return DownloadResult(
                filename=filename,
                path=destination,
                size_bytes=0,
                sha256="",
                simulated=True,
            )

        if self.settings.use_local_jar:
            return self._use_local_jar(destination, filename)

        url = self.build_url(hub_name)
        partial = destination.with_suffix(destination.suffix + ".part")
        digest = hashlib.sha256()
        size = 0
        try:
            async with httpx.AsyncClient(timeout=self.settings.download_timeout, follow_redirects=True) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code == 404:
                        raise DownloadError(
                            f"filename={hub_name} was not found on the installation hub (HTTP 404). "
                            "Check the version number, or set 'hub_filename' for this service in "
                            "app/config.py if the hub uses a different identifier."
                        )
                    if response.status_code in (401, 403):
                        raise DownloadError(
                            f"Installation hub rejected the request (HTTP {response.status_code}). "
                            "Check INSTALLATION_CODE in .env."
                        )
                    if response.status_code >= 400:
                        raise DownloadError(
                            f"Installation hub returned HTTP {response.status_code} for {filename}"
                        )
                    with partial.open("wb") as handle:
                        async for chunk in response.aiter_bytes(64 * 1024):
                            handle.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
        except httpx.TimeoutException as exc:
            partial.unlink(missing_ok=True)
            raise DownloadError(
                f"Download timed out after {self.settings.download_timeout}s: {self.redact(url)}"
            ) from exc
        except httpx.HTTPError as exc:
            partial.unlink(missing_ok=True)
            raise DownloadError(f"Could not reach the installation hub: {exc}") from exc
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise DownloadError(f"Could not write {partial}: {exc}") from exc

        if size == 0:
            partial.unlink(missing_ok=True)
            raise DownloadError(f"Downloaded {filename} is empty")
        if not self._looks_like_jar(partial):
            partial.unlink(missing_ok=True)
            raise DownloadError(
                f"Downloaded {filename} is not a JAR archive. The hub likely returned an error page."
            )

        partial.replace(destination)
        logger.info("Downloaded %s (%d bytes)", filename, size)
        return DownloadResult(
            filename=filename,
            path=destination,
            size_bytes=size,
            sha256=digest.hexdigest(),
        )

    def _use_local_jar(self, path: Path, filename: str) -> DownloadResult:
        """Use a JAR placed in TEMP_DIR by hand instead of contacting the hub.

        For when the installation hub is unreachable. Opt-in via USE_LOCAL_JAR
        so a stale file can never be picked up by accident.
        """
        if not path.is_file():
            raise DownloadError(
                f"USE_LOCAL_JAR is enabled but {filename} was not found.\n"
                f"Copy it to: {path.parent}\n"
                f"Expected exact filename: {filename}"
            )
        if not self._looks_like_jar(path):
            raise DownloadError(f"{path} is not a JAR archive (missing the ZIP 'PK' header)")

        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(chunk)

        logger.warning("USE_LOCAL_JAR: using %s without contacting the installation hub", path)
        return DownloadResult(
            filename=filename,
            path=path,
            size_bytes=path.stat().st_size,
            sha256=digest.hexdigest(),
            local=True,
        )

    @staticmethod
    def _looks_like_jar(path: Path) -> bool:
        """A JAR is a ZIP: it must start with the local-file-header magic."""
        try:
            with path.open("rb") as handle:
                return handle.read(2) == b"PK"
        except OSError:
            return False

    def cleanup(self, path: Path | None) -> None:
        """Remove the temporary JAR after a successful deployment.

        A hand-placed JAR is never deleted - it is the user's file, not ours.
        """
        if path is None or self.settings.keep_temp_files or self.settings.use_local_jar:
            return
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove temp file %s: %s", path, exc)
