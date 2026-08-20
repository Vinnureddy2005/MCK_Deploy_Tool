"""Deploying the AidenOps UI bundle.

The safer of the two AidenOps pipelines: static files, no schema, and a rollback
that is a single move. That is why this one reverts itself on failure while the
backend stops and hands over.

Every step here exists because of something that actually broke on this server:

  the tarball unpacks owned by UID 4096              -> chown
  mkdir under root's umask 027 leaves the parent 0750,
  and nginx answers an untraversable parent with a 500 -> chmod 755
  RHEL 9 with SELinux enforcing                      -> restorecon
  the bundle ships API_URL: "" and the UI then loads
  but talks to nothing                               -> write-ui-config, then grep
  a plain extract into the web root overwrites the
  live dist, because the tarball is rooted at dist/  -> extract via .stage
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from app.config import Settings
from app.config import settings as default_settings
from app.services.ssh_service import CommandFailed, SSHService

log = logging.getLogger(__name__)

STAGES = (
    "archive",
    "extract",
    "own",
    "configure",
    "relabel",
    "swap",
    "verify",
    "retain",
)


class FrontendError(RuntimeError):
    """A stage failed. `reverted` says whether the previous bundle is back."""

    def __init__(self, stage: str, message: str, reverted: bool = False):
        self.stage = stage
        self.reverted = reverted
        super().__init__(message)


_TARBALL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\.tar\.gz$")
_STAMP = re.compile(r"^[0-9]{8}-[0-9]{6}$")
# The quoted value, captured so it can be tested for emptiness. Matching
# "an empty value" directly is what went wrong first: the pattern also
# matched a populated one by backtracking past the opening quote.
_API_URL_VALUE = re.compile(r"""API_URL\s*:\s*["']([^"']*)["']""")


def stamp(now: datetime | None = None) -> str:
    """A sortable suffix, so `ls -1dt` and lexical order agree when pruning."""
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def validate_tarball(name: str) -> str:
    """The name comes from an archive already verified locally, but it reaches a
    command line here, so it is checked again at the boundary that matters."""
    if not name or not _TARBALL.match(name):
        raise FrontendError("extract", f"Refusing to use unsafe tarball name: {name!r}")
    return name


class FrontendDeployer:
    """One UI deployment. Construct, call deploy(), read the result."""

    def __init__(self, ssh: SSHService, settings: Settings | None = None, emit=None):
        self.ssh = ssh
        self.settings = settings or default_settings
        self._emit = emit

    # ── plumbing ──────────────────────────────────────────────────────────

    async def _log(self, message: str) -> None:
        log.info("%s", message)
        if self._emit is not None:
            await self._emit(message)

    async def _run(self, argv: list[str], *, stage: str, sudo: bool = True,
                   check: bool = True):
        try:
            return await self.ssh.run(argv, sudo=sudo, check=check)
        except CommandFailed as exc:
            raise FrontendError(stage, f"{argv[0]} failed: {exc.result.output[:400]}") from exc

    @property
    def web(self) -> str:
        return self.settings.aidenops_web_root

    # ── the pipeline ──────────────────────────────────────────────────────

    async def deploy(self, tarball: str, *, now: datetime | None = None) -> dict:
        name = validate_tarball(tarball)
        suffix = stamp(now)
        staging = f"{self.settings.aidenops_staging_dir}/{name}"
        previous = f"{self.web}/dist.bak-{suffix}"

        await self._archive(name, staging, suffix)
        await self._extract(staging)
        await self._own()
        await self._configure()
        await self._relabel()
        await self._swap(previous)

        try:
            checks = await self._verify()
        except FrontendError as exc:
            # Static files and one move: reverting is cheap, safe and instant,
            # so it happens without asking. The backend never does this.
            await self._log("Verification failed - reverting to the previous bundle")
            await self._revert(previous, suffix)
            raise FrontendError(exc.stage, f"{exc}. The previous bundle has been restored.",
                                reverted=True) from exc

        await self._retain()
        return {"tarball": name, "stamp": suffix, "previous": previous, "checks": checks}

    async def _archive(self, name: str, staging: str, suffix: str) -> None:
        """Keep the artifact, not an unpacked copy.

        The immediately-previous dist stays in place as dist.bak-<stamp> for an
        instant revert, so archiving the unpacked tree as well would store the
        same bytes twice - and the tarball is a quarter of the size.
        """
        await self._log(f"Archiving {name}")
        target = f"{self.settings.aidenops_backup_root}/ui"
        await self._run(["mkdir", "-p", target], stage="archive")
        await self._run(["cp", staging, f"{target}/{name}"], stage="archive")

    async def _extract(self, staging: str) -> None:
        """Extract beside the live bundle, never over it.

        Every entry in the tarball is rooted at dist/, so `tar -xzf -C <web>`
        would create <web>/dist and overwrite what is being served. Extracting
        into a scratch directory and moving the result keeps the live copy
        untouched - and fails loudly if the tarball's layout ever changes.
        """
        listing = await self._run(["tar", "-tzf", staging], stage="extract")
        first = (listing.stdout or "").strip().splitlines()
        if first and not first[0].startswith("dist/") and not listing.simulated:
            raise FrontendError(
                "extract",
                f"The tarball is rooted at {first[0]!r}, not 'dist/'. Its layout has "
                "changed, so the extraction step is no longer correct.",
            )

        scratch = f"{self.web}/.stage"
        await self._log("Extracting into a scratch directory")
        await self._run(["rm", "-rf", scratch], stage="extract")
        await self._run(["mkdir", "-p", scratch], stage="extract")
        await self._run(["tar", "-xzf", staging, "-C", scratch], stage="extract")
        # Fails if dist/ is not there, which is the behaviour we want: a layout
        # change should stop the deployment, not silently leave dist-new missing.
        await self._run(["mv", f"{scratch}/dist", f"{self.web}/dist-new"], stage="extract")
        # rmdir refuses a non-empty directory, so anything unexpected surfaces.
        await self._run(["rmdir", scratch], stage="extract")

    async def _own(self) -> None:
        """The tarball unpacks as UID 4096, and nginx must be able to traverse."""
        await self._log("Setting ownership and permissions")
        await self._run(["chown", "-R", "root:root", f"{self.web}/dist-new"], stage="own")
        # 0750 from root's umask 027 makes nginx return 500, not 403 - which
        # sends you looking in entirely the wrong place.
        await self._run(["chmod", "755", self.web], stage="own")
        result = await self._run(
            ["stat", "-c", "%a %U:%G", self.web, f"{self.web}/dist-new"], stage="own"
        )
        await self._log(f"  {' | '.join((result.stdout or '').split(chr(10)))}".rstrip(" |"))

    async def _configure(self) -> None:
        """Write runtime-config.js before the swap, never after.

        The shipped bundle carries API_URL: "" - skip this and the UI loads and
        talks to nothing. Doing it on dist-new means the live dist is never
        briefly serving an unconfigured bundle.
        """
        await self._log("Writing runtime-config.js")
        await self._run(
            [
                "env",
                f"AIDENOPS_CONFIG_PATH={self.settings.aidenops_ops_dir}/config.yaml",
                f"{self.settings.aidenops_venv}/bin/aidenops-write-ui-config",
                "--dist",
                f"{self.web}/dist-new",
            ],
            stage="configure",
        )
        await self._assert_api_url(f"{self.web}/dist-new", stage="configure")

    async def _assert_api_url(self, dist: str, *, stage: str) -> str:
        result = await self._run(
            ["grep", "-o", "API_URL[^,}]*", f"{dist}/runtime-config.js"],
            stage=stage,
            check=False,
        )
        text = (result.stdout or "").strip()
        if result.simulated:
            return "(dry run)"

        # No API_URL line at all: the file was never written.
        if not text:
            raise FrontendError(stage, "runtime-config.js has no API_URL at all")

        # Fail only on a value we can positively identify as empty. A false
        # failure here would auto-revert a perfectly good deployment, so an
        # unrecognised shape is allowed through and logged rather than rejected.
        quoted = _API_URL_VALUE.search(text)
        if quoted is not None and not quoted.group(1).strip():
            raise FrontendError(
                stage,
                "runtime-config.js has an empty API_URL - the UI would load and "
                f"reach nothing: {text!r}",
            )
        return text

    async def _relabel(self) -> None:
        """RHEL 9, SELinux enforcing: correct Unix permissions are not enough."""
        await self._log("Restoring SELinux labels")
        await self._run(["restorecon", "-Rv", self.web], stage="relabel", check=False)

    async def _swap(self, previous: str) -> None:
        """Move the old bundle aside and the new one in, in one round trip.

        Two moves cannot be atomic - rename() will not replace a non-empty
        directory - so there is a brief moment with no dist. Chaining them in one
        command keeps that to local filesystem work rather than a network hop.

        The paths are passed as positional arguments, so the script body is a
        fixed literal and nothing is interpolated into shell text.
        """
        await self._log("Swapping the new bundle in")
        await self._run(
            [
                "sh", "-c", 'mv "$1" "$2" && mv "$3" "$4"', "sh",
                f"{self.web}/dist", previous,
                f"{self.web}/dist-new", f"{self.web}/dist",
            ],
            stage="swap",
        )

    async def _verify(self) -> dict:
        """A real check, not `systemctl is-active nginx`.

        nginx being up says nothing about whether the bundle it is serving works,
        which is how a broken UI passed as a successful deployment before.
        """
        await self._log("Verifying the live bundle")
        result = await self._run(
            ["curl", "-sf", "-o", "/dev/null", "-w", "%{http_code}",
             self.settings.aidenops_ui_url],
            stage="verify",
            sudo=False,
            check=False,
        )
        status = (result.stdout or "").strip()
        if not result.simulated and status != "200":
            raise FrontendError("verify", f"The UI returned {status or 'no response'}, not 200")

        api_url = await self._assert_api_url(f"{self.web}/dist", stage="verify")
        return {"http_status": status or "(dry run)", "api_url": api_url}

    async def _revert(self, previous: str, suffix: str) -> None:
        """Put the previous bundle back. One move each way.

        No nginx reload: it resolves `root` per request, which is why both real
        redeployments here worked without touching it.
        """
        await self._run(
            [
                "sh", "-c", 'mv "$1" "$2" && mv "$3" "$4"', "sh",
                f"{self.web}/dist", f"{self.web}/dist.failed-{suffix}",
                previous, f"{self.web}/dist",
            ],
            stage="revert",
        )
        await self._run(["restorecon", "-Rv", self.web], stage="revert", check=False)

    async def _retain(self) -> None:
        """Prune what this tool wrote.

        Runs only after verification passes: until then the previous bundle and
        the failed one are both worth keeping. Unbounded backups on the volume
        that already filled once is how this tool would become the outage.
        """
        keep_dist = max(1, self.settings.aidenops_keep_previous_dist)
        keep_archives = max(1, self.settings.aidenops_keep_archives)
        await self._log(
            f"Pruning to {keep_dist} previous bundle(s) and {keep_archives} archived tarball(s)"
        )

        for pattern in (f"{self.web}/dist.bak-*", f"{self.web}/dist.failed-*"):
            await self._prune(pattern, keep_dist, directories=True)
        await self._prune(f"{self.settings.aidenops_backup_root}/ui/*.tar.gz", keep_archives)

    async def _prune(self, pattern: str, keep: int, directories: bool = False) -> None:
        """Delete all but the newest `keep` matches of a glob.

        The glob is expanded by the remote shell, so this is one of the few
        places a shell is required. The pattern is built from configuration and
        a fixed suffix - never from anything a caller supplies - and is passed
        positionally so it is not interpolated into the script text.
        """
        remove = "rm -rf" if directories else "rm -f"
        script = (
            f'ls -1dt $1 2>/dev/null | tail -n +$(($2 + 1)) | '
            f'while IFS= read -r p; do {remove} "$p"; done'
        )
        await self._run(["sh", "-c", script, "sh", pattern, str(keep)],
                        stage="retain", check=False)
