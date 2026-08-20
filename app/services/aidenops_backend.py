"""Deploying the AidenOps backend wheel.

The irreversible pipeline. `auto_migrate: true` means starting the service runs
Alembic, so a backend deployment is a schema change whether or not anyone
intended one - and no amount of reinstalling wheels undoes a migration.

Everything that can refuse happens while the service is still running. Past the
start, recovery needs the database as well as the wheel, so this pipeline never
attempts it: it stops, and prints a runbook a person decides to run.

Two facts about this server shape most of the code:

  day6sio cannot read /home/AidenAI/ops1 or the database directory at all
  without sudo, so every command touching them is privileged - including the
  df that measures free space, which silently succeeds on the parent and would
  have measured the wrong thing.

  The wheel declares no dependencies, so requirements.txt is not a pinning
  convenience but the only mechanism that installs anything. Diffing it is the
  mechanism, not a safeguard around one.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

from app.config import Settings
from app.config import settings as default_settings
from app.services import migration_service, requirements_service
from app.services.ssh_service import CommandFailed, SSHService

log = logging.getLogger(__name__)

STAGES = (
    "preflight",
    "migrations",
    "dependencies",
    "dump",
    "backup",
    "stop",
    "install",
    "start",
    "health",
    "cleanup",
)


class BackendError(RuntimeError):
    """A stage failed.

    `past_the_line` says whether the service was started - which is the moment
    recovery stops being a wheel swap and starts needing the database.
    """

    def __init__(self, stage: str, message: str, past_the_line: bool = False,
                 runbook: list[str] | None = None):
        self.stage = stage
        self.past_the_line = past_the_line
        self.runbook = runbook or []
        super().__init__(message)


class BackendConfirmation(RuntimeError):
    """The deployment needs an explicit decision before it can continue."""

    def __init__(self, reason: str, detail: dict):
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


_WHEEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\.whl$")
_REQS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\.txt$")
_SHA256_LINE = re.compile(r"^[a-f0-9]{64}\b")

# pg_dump writes this as its final line. Its presence is the only cheap proof
# that a dump ran to completion rather than being cut off by a full disk.
_DUMP_TERMINATOR = "PostgreSQL database dump complete"


def stamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


class BackendDeployer:
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
                   run_as: str | None = None, check: bool = True,
                   timeout: int | None = None):
        """Every path under ops1 needs sudo on this server, so it is the default.

        `run_as` targets another user in the same sudo rather than nesting a
        second one - one privilege escalation, one line in /var/log/sudo.log.
        """
        try:
            return await self.ssh.run(argv, sudo=sudo, run_as=run_as, check=check,
                                      timeout=timeout)
        except CommandFailed as exc:
            raise BackendError(stage, f"{argv[0]} failed: {exc.result.output[:400]}") from exc

    @property
    def venv_pip(self) -> str:
        # Absolute, because sudoers sets secure_path and resets PATH.
        return f"{self.settings.aidenops_venv}/bin/pip"

    def _staged(self, name: str) -> str:
        return f"{self.settings.aidenops_staging_dir}/{name}"

    # ── the pipeline ──────────────────────────────────────────────────────

    async def deploy(
        self,
        wheel: str,
        requirements: str | None,
        local_wheel_path,
        *,
        confirmed: bool = False,
        now: datetime | None = None,
    ) -> dict:
        """Install a wheel. Raises BackendConfirmation when a decision is needed."""
        if not _WHEEL.match(wheel or ""):
            raise BackendError("install", f"Refusing to use unsafe wheel name: {wheel!r}")
        if requirements and not _REQS.match(requirements):
            raise BackendError("dependencies",
                               f"Refusing to use unsafe requirements name: {requirements!r}")

        suffix = stamp(now)
        report: dict = {"wheel": wheel, "stamp": suffix}

        await self._preflight(report)
        plan = await self._migrations(local_wheel_path, report)
        deps = await self._dependencies(requirements, report)

        # One decision point covering both reasons, so an operator is asked once
        # rather than twice for the same deployment.
        if not confirmed and (plan["needs_confirmation"] or deps["needs_confirmation"]):
            raise BackendConfirmation(
                "This deployment changes things that cannot be undone by "
                "reinstalling the previous wheel.",
                {"migrations": plan, "dependencies": deps},
            )

        if plan["needs_dump"]:
            report["dump"] = await self._dump(suffix)
        else:
            # Nothing will migrate, so a rollback is a wheel swap and 140 MB of
            # dump would protect against nothing.
            await self._log("No pending migrations - skipping the database dump")
            report["dump"] = None

        report["previous_version"] = await self._backup(suffix)
        await self._stop()

        if deps["needs_install"]:
            await self._install_dependencies(requirements)
        await self._install_wheel(wheel)

        await self._start(report)
        report["health"] = await self._health(report)
        await self._cleanup()
        return report

    # ── stages that can still refuse ──────────────────────────────────────

    async def _preflight(self, report: dict) -> None:
        await self._log("Preflight")
        settings = self.settings

        for directory in (settings.aidenops_staging_dir,
                          f"{settings.aidenops_backup_root}/db",
                          f"{settings.aidenops_backup_root}/wheels"):
            await self._run(["mkdir", "-p", directory], stage="preflight")

        # sudo, because day6sio cannot stat the data directory at all. Without
        # it df silently reports the parent filesystem, which is the wrong
        # answer that looks like the right one.
        data_dir = await self._run(
            ["psql", "-tAc", "SHOW data_directory;"],
            stage="preflight", run_as="postgres", check=False,
        )
        resolved = (data_dir.stdout or "").strip().splitlines()
        report["data_directory"] = resolved[0] if resolved else ""

        for path in filter(None, [report["data_directory"], settings.aidenops_web_root]):
            space = await self._run(["df", "-Pk", path], stage="preflight", check=False)
            line = (space.stdout or "").strip().splitlines()
            if len(line) > 1:
                await self._log(f"  df {path}: {line[-1]}")

    async def _migrations(self, local_wheel_path, report: dict) -> dict:
        """What Alembic will apply, read from the wheel and the database.

        The wheel is read locally; only the current revision comes from the
        server. That keeps the expensive part off the box and makes the whole
        computation testable without one.
        """
        await self._log("Reading migrations")
        revisions = migration_service.scan(local_wheel_path)

        current = await self._current_revision()
        plan = migration_service.summarise(migration_service.pending(revisions, current))

        report["migrations"] = {
            "current": current,
            "count": plan["count"],
            "destructive": [r["revision"] for r in plan["destructive"]],
        }
        await self._log(
            f"  {plan['count']} migration(s) will apply"
            + (f", {len(plan['destructive'])} destructive" if plan["destructive"] else "")
        )
        for revision in plan["migrations"]:
            mark = "  DESTRUCTIVE" if revision["destructive"] else ""
            await self._log(f"    {revision['revision']}  {revision['slug']}{mark}")
        return plan

    async def _current_revision(self) -> str | None:
        result = await self._run(
            ["psql", "-d", "aidenops", "-tAc", "SELECT version_num FROM alembic_version;"],
            stage="migrations", run_as="postgres", check=False,
        )
        value = (result.stdout or "").strip().splitlines()
        if result.simulated or not value:
            # An empty database applies everything, which is also what a dry run
            # should show rather than pretending nothing is pending.
            return None
        return value[0].strip() or None

    async def _dependencies(self, requirements: str | None, report: dict) -> dict:
        """Diff the pinned set, and prove it resolves while the service is up."""
        if not requirements:
            await self._log("No requirements file in this release")
            report["dependencies"] = {"changes": [], "needs_install": False}
            return {"changes": [], "needs_install": False, "needs_confirmation": False,
                    "needs_index": False, "probe": []}

        await self._log("Comparing pinned dependencies")
        installed = await self._run([self.venv_pip, "freeze"], stage="dependencies")
        required = await self._run(["cat", self._staged(requirements)], stage="dependencies")

        summary = requirements_service.summarise(
            requirements_service.diff(installed.stdout or "", required.stdout or "")
        )
        report["dependencies"] = {
            "changes": summary["changes"],
            "needs_install": summary["needs_install"],
        }

        for change in summary["changes"]:
            await self._log(f"  {change['change']}: {change['package']} "
                            f"{change.get('from', '')}{' -> ' if change.get('from') else ''}"
                            f"{change.get('to', '')}".rstrip())

        if summary["needs_install"]:
            # pip 26.2 on this server, so --dry-run is available. Run it while
            # the service is still up: a failure here costs no downtime.
            await self._log("Checking that the new pins resolve (nothing installed)")
            await self._run(
                [self.venv_pip, "install", "--dry-run", "-r", self._staged(requirements)],
                stage="dependencies", timeout=300,
            )
        else:
            await self._log("  no changes - no index access needed for this release")
        return summary

    async def _dump(self, suffix: str) -> dict:
        """Prune first, dump straight to gzip, then prove it is complete."""
        backups = f"{self.settings.aidenops_backup_root}/db"
        keep = max(1, self.settings.aidenops_keep_dumps)
        target = f"{backups}/aidenops-{suffix}.sql.gz"

        # Pruned before writing, so the peak on disk is `keep`, not keep + 1.
        await self._log(f"Pruning old dumps to {keep - 1}")
        await self._prune(f"{backups}/*.sql.gz", keep - 1, stage="dump")

        await self._log("Dumping the database")
        # pipefail matters: gzip succeeds on a truncated stream, so without it a
        # failed pg_dump produces a small valid .gz that would then be trusted.
        # The shell is needed for pipefail, and the whole pipeline runs as root
        # via the outer sudo - so pg_dump is reached with `-u postgres` inside it
        # only because the dump itself must run as the database owner.
        await self._run(
            ["sh", "-c",
             'set -o pipefail; sudo -u postgres pg_dump "$1" | gzip > "$2"',
             "sh", "aidenops", target],
            stage="dump", timeout=3600,
        )

        verified = await self._verify_dump(target)
        await self._log(f"  {target} ({verified['size']} bytes) verified")
        return {"path": target, **verified}

    async def _verify_dump(self, target: str) -> dict:
        """Three checks, while the old database still exists.

        This is the only window where the data has no second copy, so a dump
        that cannot be trusted must stop the deployment here rather than be
        discovered after the schema has moved.
        """
        await self._run(["gzip", "-t", target], stage="dump")

        tail = await self._run(
            ["sh", "-c", 'gunzip -c "$1" | tail -5', "sh", target],
            stage="dump", check=False,
        )
        if not tail.simulated and _DUMP_TERMINATOR not in (tail.stdout or ""):
            raise BackendError(
                "dump",
                "The dump does not end with pg_dump's completion marker, so it is "
                "truncated. Refusing to continue without a usable rollback point.",
            )

        size = await self._run(["stat", "-c", "%s", target], stage="dump", check=False)
        digits = (size.stdout or "").strip()
        return {"size": int(digits) if digits.isdigit() else None,
                "complete": True}

    async def _backup(self, suffix: str) -> str | None:
        """Keep the wheel being replaced - pip --force-reinstall discards it."""
        await self._log("Recording the installed version")
        shown = await self._run([self.venv_pip, "show", "aidenops-service"],
                                stage="backup", check=False)
        version = None
        for line in (shown.stdout or "").splitlines():
            if line.lower().startswith("version:"):
                version = line.split(":", 1)[1].strip()

        wheels = f"{self.settings.aidenops_backup_root}/wheels"
        await self._run(
            ["sh", "-c", 'cp "$1"/*.whl "$2"/ 2>/dev/null || true',
             "sh", self.settings.aidenops_ops_dir, wheels],
            stage="backup", check=False,
        )
        await self._prune(f"{wheels}/*.whl",
                          max(1, self.settings.aidenops_keep_dumps), stage="backup")
        await self._log(f"  replacing version {version or 'unknown'}")
        return version

    # ── past this point recovery needs the database ───────────────────────

    async def _stop(self) -> None:
        await self._log(f"Stopping {self.settings.aidenops_unit}")
        await self._run(["systemctl", "stop", self.settings.aidenops_unit], stage="stop")

    async def _install_dependencies(self, requirements: str) -> None:
        await self._log("Installing dependencies")
        await self._run(
            [self.venv_pip, "install", "--no-cache-dir", "-r", self._staged(requirements)],
            stage="install", timeout=1800,
        )

    async def _install_wheel(self, wheel: str) -> None:
        await self._log(f"Installing {wheel}")
        # --force-reinstall because two builds of one commit carry the same
        # version string. --no-deps because dependencies are their own step, and
        # stating it keeps this correct if the wheel ever declares any.
        await self._run(
            [self.venv_pip, "install", "--no-cache-dir", "--force-reinstall", "--no-deps",
             self._staged(wheel)],
            stage="install", timeout=1800,
        )

    async def _start(self, report: dict) -> None:
        await self._log(f"Starting {self.settings.aidenops_unit} - Alembic runs now")
        try:
            await self._run(["systemctl", "start", self.settings.aidenops_unit], stage="start")
        except BackendError as exc:
            raise BackendError("start", str(exc), past_the_line=True,
                               runbook=self.runbook(report)) from exc

    async def _health(self, report: dict) -> dict:
        """Poll until the port opens, tolerating refusals while Alembic runs."""
        settings = self.settings
        deadline = settings.aidenops_health_timeout
        interval = max(1, settings.aidenops_health_interval)
        await self._log(f"Waiting up to {deadline}s for /health")

        waited = 0
        last = ""
        while waited <= deadline:
            result = await self.ssh.run(
                ["curl", "-sf", "-o", "/dev/null", "-w", "%{http_code}",
                 settings.aidenops_health_url],
                sudo=False, check=False,
            )
            if result.simulated:
                return {"status": "(dry run)", "waited": 0}
            last = (result.stdout or "").strip()
            if last == "200":
                await self._log(f"  healthy after {waited}s")
                return {"status": "200", "waited": waited}

            # A refused connection is the expected answer while migrations run;
            # only the ceiling is a failure.
            if waited and waited % 30 == 0:
                await self._log(f"  still starting ({waited}s) - migrations may be running")
            await asyncio.sleep(interval)
            waited += interval

        raise BackendError(
            "health",
            f"The backend did not answer /health within {deadline}s "
            f"(last response: {last or 'no connection'}).",
            past_the_line=True,
            runbook=self.runbook(report),
        )

    async def _cleanup(self) -> None:
        """Only now. Until it is healthy, the staged files are what a retry uses."""
        await self._log("Clearing staging")
        await self._run(
            ["sh", "-c", 'rm -f "$1"/* 2>/dev/null || true',
             "sh", self.settings.aidenops_staging_dir],
            stage="cleanup", check=False,
        )

    # ── recovery is printed, never executed ───────────────────────────────

    def runbook(self, report: dict) -> list[str]:
        """The recovery sequence, with real values filled in.

        Never run by this tool. A restore destroys everything written since the
        dump, and only a person can weigh that against a broken service - and a
        tool that *can* drop a production database eventually will.
        """
        settings = self.settings
        dump = (report.get("dump") or {}).get("path")
        version = report.get("previous_version")

        lines = [
            f"systemctl stop {settings.aidenops_unit}",
        ]
        if dump:
            lines += [
                "# This DESTROYS everything written since the dump was taken.",
                'sudo -u postgres psql -c "SELECT pg_terminate_backend(pid) '
                "FROM pg_stat_activity WHERE datname='aidenops' "
                'AND pid <> pg_backend_pid();"',
                'sudo -u postgres psql -c "DROP DATABASE aidenops;"',
                'sudo -u postgres psql -c "CREATE DATABASE aidenops OWNER aidap;"',
                f'gunzip -c {dump} | sudo -u postgres psql aidenops',
            ]
        else:
            lines.append("# No dump was taken: this release had no pending migrations,")
            lines.append("# so reinstalling the previous wheel is a complete rollback.")

        lines += [
            f"{self.venv_pip} install --force-reinstall --no-deps "
            f"{settings.aidenops_backup_root}/wheels/<{version or 'previous'}>.whl",
            f"systemctl start {settings.aidenops_unit}",
        ]
        return lines

    async def _prune(self, pattern: str, keep: int, *, stage: str) -> None:
        """Keep the newest `keep` matches of a glob, delete the rest.

        The glob needs a shell to expand, so the pattern is passed positionally
        and the script body stays a fixed literal.
        """
        script = ('ls -1t $1 2>/dev/null | tail -n +$(($2 + 1)) | '
                  'while IFS= read -r p; do rm -f "$p"; done')
        await self._run(["sh", "-c", script, "sh", pattern, str(max(0, keep))],
                        stage=stage, check=False)
