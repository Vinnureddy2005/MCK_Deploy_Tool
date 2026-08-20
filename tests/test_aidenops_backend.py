"""The AidenOps backend pipeline: the irreversible one.

The tests that matter most here are the negative ones. Everything that can
refuse must refuse while the service is still running, the tool must never
execute a database restore, and a dump that cannot be trusted must stop the
deployment rather than be discovered after the schema has moved.
"""

from __future__ import annotations

import dataclasses
import zipfile
from datetime import datetime

import pytest

from app.services.aidenops_backend import (
    BackendConfirmation,
    BackendDeployer,
    BackendError,
)
from app.services.ssh_service import CommandFailed, CommandResult

WHEEL = "aidenops_service-1.1.0+gd00222c-py3-none-any.whl"
REQS = "requirements-1.1.0+gd00222c.txt"
WHEN = datetime(2026, 8, 20, 17, 30, 0)

CURRENT = "20260714_slow_endpoint_indexes"

ADDITIVE = '''"""add reference values
Revision ID: 20260818_reference_values
"""
revision = "20260818_reference_values"
down_revision = "20260714_slow_endpoint_indexes"

def upgrade() -> None:
    op.create_table("reference_values", sa.Column("id", sa.Integer()))

def downgrade() -> None:
    op.drop_table("reference_values")
'''

BASE = '''"""slow endpoint indexes
Revision ID: 20260714_slow_endpoint_indexes
"""
revision = "20260714_slow_endpoint_indexes"
down_revision = None

def upgrade() -> None:
    op.create_index("ix_slow", "endpoints", ["path"])
'''

DESTRUCTIVE = '''"""drop legacy notes
Revision ID: 20260819_drop_notes
"""
revision = "20260819_drop_notes"
down_revision = "20260818_reference_values"

def upgrade() -> None:
    op.drop_column("tickets", "legacy_notes")
'''

FREEZE = "fastapi==0.115.0\nasyncpg==0.30.0\n"
DUMP_TAIL = "-- PostgreSQL database dump complete\n"


def _wheel(tmp_path, revisions=(BASE, ADDITIVE)):
    path = tmp_path / WHEEL
    with zipfile.ZipFile(path, "w") as zf:
        for index, source in enumerate(revisions):
            zf.writestr(f"migrations/versions/{index:04d}_rev.py", source)
    return path


class RecordingSSH:
    def __init__(self, *, current=CURRENT, freeze=FREEZE, required=FREEZE,
                 health="200", dump_tail=DUMP_TAIL, fail_on=None,
                 avail_kb=33_554_432, previous_dump=""):
        self.commands: list[list[str]] = []
        self.sudo_users: list[str | None] = []
        self.current = current
        self.freeze = freeze
        self.required = required
        self.health = health
        self.dump_tail = dump_tail
        self.fail_on = fail_on or ()
        self.avail_kb = avail_kb
        self.previous_dump = previous_dump

    async def run(self, argv, *, sudo=False, run_as=None, stdin_data=None,
                  timeout=None, check=True):
        argv = [str(a) for a in argv]
        self.commands.append(argv)
        self.sudo_users.append(run_as)
        joined = " ".join(argv)

        for marker in self.fail_on:
            if marker in joined:
                result = CommandResult(joined, 1, "", f"failed: {marker}")
                if check:
                    raise CommandFailed(result)
                return result

        if "alembic_version" in joined:
            return CommandResult(joined, 0, self.current or "", "")
        if "SHOW data_directory" in joined:
            return CommandResult(joined, 0, "/home/AidenAI/pgsql/16/data", "")
        if argv[-1] == "freeze":
            return CommandResult(joined, 0, self.freeze, "")
        if argv[0] == "cat":
            return CommandResult(joined, 0, self.required, "")
        if argv[0] == "curl":
            return CommandResult(joined, 0, self.health, "")
        if "tail -5" in joined:
            return CommandResult(joined, 0, self.dump_tail, "")
        if argv[0] == "stat":
            return CommandResult(joined, 0, "146800640", "")  # ~140 MB
        if argv[0] == "df":
            # A real `df -Pk` layout: the fourth field is Available in 1K
            # blocks. The old fake returned "32G" there, which is not a digit,
            # so the space check read it as unmeasurable and skipped itself.
            return CommandResult(
                joined, 0,
                "Filesystem 1024-blocks Used Available Capacity Mounted-on\n"
                f"/dev/mapper/datavg 52428800 100000 {self.avail_kb} 38% /home/AidenAI",
                "",
            )
        if "head -1" in joined:
            return CommandResult(joined, 0, self.previous_dump, "")
        if "show" in joined and "aidenops-service" in joined:
            return CommandResult(joined, 0, "Name: aidenops-service\nVersion: 1.0.9\n", "")
        return CommandResult(joined, 0, "", "")

    def ran(self, *fragments) -> bool:
        return any(all(f in " ".join(c) for f in fragments) for c in self.commands)

    def ran_exact(self, *args) -> bool:
        """Every argument present as its own argv element.

        Substring matching is too loose here: "--force-reinstall" contains "-r",
        so a search for the requirements install matches the wheel install too.
        """
        return any(all(a in c for a in args) for c in self.commands)

    def index_of(self, *fragments) -> int:
        for index, command in enumerate(self.commands):
            if all(f in " ".join(command) for f in fragments):
                return index
        return -1

    @property
    def text(self) -> str:
        return "\n".join(" ".join(c) for c in self.commands)


@pytest.fixture
def fast(dry_settings):
    """A health poll that gives up quickly, so timeout tests stay fast."""
    return dataclasses.replace(
        dry_settings, aidenops_health_timeout=2, aidenops_health_interval=1
    )


def _deployer(ssh, settings):
    return BackendDeployer(ssh, settings)


# --- names ----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["../x.whl", "a.whl; id", "-rf.whl", "", "x.tar.gz"])
async def test_unsafe_wheel_names_are_refused(name, fast, tmp_path):
    ssh = RecordingSSH()
    with pytest.raises(BackendError, match="unsafe wheel name"):
        await _deployer(ssh, fast).deploy(name, None, _wheel(tmp_path), now=WHEN)
    assert ssh.commands == []


@pytest.mark.asyncio
async def test_an_unsafe_requirements_name_is_refused(fast, tmp_path):
    with pytest.raises(BackendError, match="unsafe requirements name"):
        await _deployer(RecordingSSH(), fast).deploy(WHEEL, "../x.txt", _wheel(tmp_path), now=WHEN)


# --- the dump decision ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_release_with_no_pending_migrations_takes_no_dump(fast, tmp_path):
    """Ten config-only deploys in a day should not write 1.4 GB for nothing."""
    ssh = RecordingSSH(current="20260818_reference_values")
    report = await _deployer(ssh, fast).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)

    assert report["dump"] is None
    assert not ssh.ran("pg_dump")


@pytest.mark.asyncio
async def test_pending_migrations_take_a_dump(fast, tmp_path):
    ssh = RecordingSSH(current=CURRENT)
    report = await _deployer(ssh, fast).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)

    assert report["migrations"]["count"] == 1
    assert ssh.ran("pg_dump")
    assert report["dump"]["path"].endswith("aidenops-20260820-173000.sql.gz")


@pytest.mark.asyncio
async def test_an_empty_database_is_treated_as_everything_pending(fast, tmp_path):
    ssh = RecordingSSH(current="")
    report = await _deployer(ssh, fast).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)
    assert report["migrations"]["count"] == 2


# --- confirmation ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_destructive_migration_asks_first(fast, tmp_path):
    ssh = RecordingSSH(current="20260818_reference_values")
    wheel = _wheel(tmp_path, revisions=(BASE, ADDITIVE, DESTRUCTIVE))

    with pytest.raises(BackendConfirmation) as caught:
        await _deployer(ssh, fast).deploy(WHEEL, None, wheel, now=WHEN)

    assert caught.value.detail["migrations"]["needs_confirmation"] is True
    # Nothing was stopped, dumped or installed.
    assert not ssh.ran("systemctl", "stop")
    assert not ssh.ran("pg_dump")


@pytest.mark.asyncio
async def test_a_dependency_change_asks_first(fast, tmp_path):
    ssh = RecordingSSH(required=FREEZE + "httpx==0.27.2\n")

    with pytest.raises(BackendConfirmation) as caught:
        await _deployer(ssh, fast).deploy(WHEEL, REQS, _wheel(tmp_path), now=WHEN)

    changes = caught.value.detail["dependencies"]["changes"]
    assert {"package": "httpx", "change": "added", "to": "0.27.2"} in changes
    assert not ssh.ran("systemctl", "stop")


@pytest.mark.asyncio
async def test_confirming_lets_it_proceed(fast, tmp_path):
    ssh = RecordingSSH(required=FREEZE + "httpx==0.27.2\n")
    await _deployer(ssh, fast).deploy(WHEEL, REQS, _wheel(tmp_path), confirmed=True, now=WHEN)

    assert ssh.ran("systemctl", "stop")
    assert ssh.ran_exact("install", "-r")


@pytest.mark.asyncio
async def test_an_additive_release_needs_no_confirmation(fast, tmp_path):
    """Asking on every schema change trains people to click through."""
    ssh = RecordingSSH(current=CURRENT)
    report = await _deployer(ssh, fast).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)
    assert report["migrations"]["destructive"] == []


# --- dependencies ---------------------------------------------------------


@pytest.mark.asyncio
async def test_unchanged_pins_need_no_index_access(fast, tmp_path):
    """What keeps a locked-down server deployable: nothing to install means the
    deployment does not need PyPI at all."""
    ssh = RecordingSSH(required=FREEZE)
    await _deployer(ssh, fast).deploy(WHEEL, REQS, _wheel(tmp_path), now=WHEN)

    assert not ssh.ran_exact("install", "-r")
    assert not ssh.ran("--dry-run")


@pytest.mark.asyncio
async def test_changed_pins_are_resolved_before_the_service_stops(fast, tmp_path):
    """A failure here must cost no downtime."""
    ssh = RecordingSSH(required=FREEZE + "httpx==0.27.2\n")
    await _deployer(ssh, fast).deploy(WHEEL, REQS, _wheel(tmp_path), confirmed=True, now=WHEN)

    assert ssh.index_of("--dry-run") < ssh.index_of("systemctl", "stop")


# --- the dump itself ------------------------------------------------------


@pytest.mark.asyncio
async def test_old_dumps_are_pruned_before_the_new_one_is_written(fast, tmp_path):
    """Peak on disk is the retention count, not one more than it."""
    ssh = RecordingSSH(current=CURRENT)
    await _deployer(ssh, fast).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)

    assert ssh.index_of("ls -1t", "*.sql.gz") < ssh.index_of("pg_dump")


@pytest.mark.asyncio
async def test_the_dump_pipeline_sets_pipefail(fast, tmp_path):
    """gzip succeeds on a truncated stream, so without pipefail a failed
    pg_dump produces a small valid .gz that would then be trusted."""
    ssh = RecordingSSH(current=CURRENT)
    await _deployer(ssh, fast).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)

    dump = ssh.commands[ssh.index_of("pg_dump")]
    assert "set -o pipefail" in dump[2]
    assert "gzip" in dump[2]


@pytest.mark.asyncio
async def test_a_truncated_dump_stops_the_deployment(fast, tmp_path):
    """The only window where the data has no second copy."""
    ssh = RecordingSSH(current=CURRENT, dump_tail="COPY public.tickets (id, subj\n")

    with pytest.raises(BackendError, match="truncated"):
        await _deployer(ssh, fast).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)

    assert not ssh.ran("systemctl", "stop")
    assert not ssh.ran("--force-reinstall")


@pytest.mark.asyncio
async def test_the_dump_runs_as_postgres_in_one_sudo(fast, tmp_path):
    """Nested sudo doubles every entry in /var/log/sudo.log, on a /var that is
    already 87% full."""
    ssh = RecordingSSH(current=CURRENT)
    await _deployer(ssh, fast).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)

    revision_call = ssh.index_of("alembic_version")
    assert ssh.sudo_users[revision_call] == "postgres"
    assert "sudo -u postgres sudo" not in ssh.text


# --- ordering across the irreversible line --------------------------------


@pytest.mark.asyncio
async def test_everything_reversible_happens_before_the_stop(fast, tmp_path):
    ssh = RecordingSSH(current=CURRENT)
    await _deployer(ssh, fast).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)

    stop = ssh.index_of("systemctl", "stop")
    assert ssh.index_of("pg_dump") < stop
    assert ssh.index_of("show", "aidenops-service") < stop
    assert stop < ssh.index_of("--force-reinstall")
    assert ssh.index_of("--force-reinstall") < ssh.index_of("systemctl", "start")


@pytest.mark.asyncio
async def test_the_wheel_install_states_no_deps(fast, tmp_path):
    """Correct today because the wheel declares none - stated so it stays
    correct if a future release declares some."""
    ssh = RecordingSSH(current=CURRENT)
    await _deployer(ssh, fast).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)

    install = " ".join(ssh.commands[ssh.index_of("--force-reinstall")])
    assert "--no-deps" in install
    assert "--no-cache-dir" in install


@pytest.mark.asyncio
async def test_staging_is_only_cleared_after_a_healthy_start(fast, tmp_path):
    """Until it is healthy, the staged files are what a retry uses."""
    ssh = RecordingSSH(current=CURRENT, health="500")

    with pytest.raises(BackendError):
        await _deployer(ssh, fast).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)

    assert not ssh.ran("rm -f", "staging")


# --- health ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_healthy_start_completes(fast, tmp_path):
    ssh = RecordingSSH(current=CURRENT, health="200")
    report = await _deployer(ssh, fast).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)

    assert report["health"]["status"] == "200"
    assert ssh.ran("rm -f", "staging")


@pytest.mark.asyncio
async def test_a_health_timeout_reports_being_past_the_line(fast, tmp_path):
    ssh = RecordingSSH(current=CURRENT, health="")

    with pytest.raises(BackendError) as caught:
        await _deployer(ssh, fast).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)

    assert caught.value.stage == "health"
    assert caught.value.past_the_line is True
    assert caught.value.runbook, "a runbook must be offered once past the line"


# --- recovery is printed, never executed ---------------------------------


@pytest.mark.asyncio
async def test_the_tool_never_executes_a_restore(fast, tmp_path):
    """A tool that can drop a production database eventually will."""
    ssh = RecordingSSH(current=CURRENT, health="")

    with pytest.raises(BackendError):
        await _deployer(ssh, fast).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)

    for forbidden in ("DROP DATABASE", "CREATE DATABASE", "pg_terminate_backend",
                      "gunzip -c /home/AidenAI/backups/db"):
        assert forbidden not in ssh.text, forbidden


@pytest.mark.asyncio
async def test_the_runbook_carries_real_values(fast, tmp_path):
    ssh = RecordingSSH(current=CURRENT, health="")

    with pytest.raises(BackendError) as caught:
        await _deployer(ssh, fast).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)

    runbook = "\n".join(caught.value.runbook)
    assert "aidenops-20260820-173000.sql.gz" in runbook
    assert "DROP DATABASE" in runbook
    assert "DESTROYS" in runbook
    assert "1.0.9" in runbook


@pytest.mark.asyncio
async def test_the_runbook_says_no_restore_is_needed_when_no_dump_was_taken(fast, tmp_path):
    """Without migrations, reinstalling the wheel is a complete rollback."""
    ssh = RecordingSSH(current="20260818_reference_values", health="")

    with pytest.raises(BackendError) as caught:
        await _deployer(ssh, fast).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)

    runbook = "\n".join(caught.value.runbook)
    assert "DROP DATABASE" not in runbook
    assert "complete rollback" in runbook


# --- disk space actually refuses ------------------------------------------


@pytest.mark.asyncio
async def test_a_nearly_full_volume_stops_the_deployment(fast, tmp_path):
    """Logging df output was not a check. /home/AidenAI holds the database, the
    dumps and the staged archive, so the tool filling it stops PostgreSQL."""
    ssh = RecordingSSH(current=CURRENT, avail_kb=200 * 1024)   # 200 MB free

    with pytest.raises(BackendError, match="Refusing to start"):
        await _deployer(ssh, fast).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)

    assert not ssh.ran("systemctl", "stop")
    assert not ssh.ran("pg_dump")


@pytest.mark.asyncio
async def test_the_message_names_the_volume_and_both_numbers(fast, tmp_path):
    ssh = RecordingSSH(current=CURRENT, avail_kb=200 * 1024)

    with pytest.raises(BackendError) as caught:
        await _deployer(ssh, fast).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)

    message = str(caught.value)
    assert "200 MB free" in message
    assert "1024 MB" in message
    assert "stops PostgreSQL" in message


@pytest.mark.asyncio
async def test_ample_space_proceeds(fast, tmp_path):
    ssh = RecordingSSH(current=CURRENT, avail_kb=33_554_432)   # 32 GB
    await _deployer(ssh, fast).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)
    assert ssh.ran("pg_dump")


@pytest.mark.asyncio
async def test_the_dump_headroom_is_sized_from_the_previous_dump(fast, tmp_path):
    """A 140 MB dump plus a 1 GB margin needs more than 1 GB free - so a volume
    with only 1.1 GB is enough for the margin alone but not for the dump."""
    ssh = RecordingSSH(
        current=CURRENT,
        avail_kb=1_150 * 1024,
        previous_dump="/home/AidenAI/backups/db/aidenops-20260819-100000.sql.gz",
    )

    with pytest.raises(BackendError, match="for the dump plus"):
        await _deployer(ssh, fast).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)

    # It got past the margin check and failed on the dump-specific one.
    assert ssh.ran("ls -1t", "*.sql.gz")
    assert not ssh.ran("pg_dump")


@pytest.mark.asyncio
async def test_pruning_happens_before_the_space_check(fast, tmp_path):
    """Pruning frees space, so checking first would refuse deployments that
    would actually fit."""
    ssh = RecordingSSH(current=CURRENT)
    await _deployer(ssh, fast).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)

    assert ssh.index_of("ls -1t", "*.sql.gz") < ssh.index_of("pg_dump")


@pytest.mark.asyncio
async def test_unmeasurable_space_is_not_treated_as_empty(dry_settings, tmp_path):
    """A rehearsal cannot read df, and refusing because of that would be a false
    alarm rather than a safeguard."""
    import dataclasses

    settings = dataclasses.replace(dry_settings, aidenops_health_timeout=2,
                                   aidenops_health_interval=1)

    class Unmeasurable(RecordingSSH):
        async def run(self, argv, **kwargs):
            result = await super().run(argv, **kwargs)
            if argv and argv[0] == "df":
                return CommandResult(" ".join(map(str, argv)), 0, "", "")
            return result

    ssh = Unmeasurable(current=CURRENT)
    await _deployer(ssh, settings).deploy(WHEEL, None, _wheel(tmp_path), now=WHEN)
    assert ssh.ran("pg_dump")
