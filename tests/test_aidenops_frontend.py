"""The AidenOps UI pipeline.

Every assertion here corresponds to something that actually broke on this
server: UID 4096 ownership, a 0750 parent that makes nginx return 500, SELinux
labels, a blank API_URL, and a tarball rooted at dist/ that would overwrite the
live bundle if extracted in place.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.services.aidenops_frontend import (
    FrontendDeployer,
    FrontendError,
    stamp,
    validate_tarball,
)
from app.services.ssh_service import CommandResult

TARBALL = "aidenops-ui-1.0.0+g635405c.tar.gz"
WHEN = datetime(2026, 8, 20, 17, 30, 0)
SUFFIX = "20260820-173000"

WEB = "/var/www/aidenops"


class RecordingSSH:
    """Records every command, and can be told to fail or answer specific ones."""

    def __init__(self, listing="dist/\ndist/index.html\n", http="200",
                 api_url='API_URL: "http://localhost:8000"', fail_on=None):
        self.commands: list[list[str]] = []
        self.listing = listing
        self.http = http
        self.api_url = api_url
        self.fail_on = fail_on or ()

    async def run(self, argv, *, sudo=False, stdin_data=None, timeout=None, check=True):
        argv = [str(a) for a in argv]
        self.commands.append(argv)
        joined = " ".join(argv)

        for marker in self.fail_on:
            if marker in joined:
                if check:
                    from app.services.ssh_service import CommandFailed

                    raise CommandFailed(CommandResult(joined, 1, "", f"failed: {marker}"))
                return CommandResult(joined, 1, "", f"failed: {marker}")

        if argv[0] == "tar" and "-tzf" in argv:
            return CommandResult(joined, 0, self.listing, "")
        if argv[0] == "curl":
            return CommandResult(joined, 0, self.http, "")
        if argv[0] == "grep" and "API_URL" in joined:
            return CommandResult(joined, 0, self.api_url, "")
        if argv[0] == "stat":
            return CommandResult(joined, 0, "755 root:root\n755 root:root", "")
        return CommandResult(joined, 0, "", "")

    def ran(self, *fragments) -> bool:
        """True when one recorded command contains every fragment."""
        return any(all(f in " ".join(c) for f in fragments) for c in self.commands)

    def index_of(self, *fragments) -> int:
        for index, command in enumerate(self.commands):
            if all(f in " ".join(command) for f in fragments):
                return index
        return -1


@pytest.fixture
def ssh():
    return RecordingSSH()


@pytest.fixture
def deployer(ssh, dry_settings):
    return FrontendDeployer(ssh, dry_settings)


# --- names and stamps ------------------------------------------------------


def test_a_valid_tarball_name_is_accepted():
    assert validate_tarball(TARBALL) == TARBALL


@pytest.mark.parametrize(
    "name",
    ["../../etc/passwd", "x.tar.gz; rm -rf /", "-rf.tar.gz", "", "x.whl", "a b.tar.gz"],
)
def test_unsafe_tarball_names_are_refused(name):
    with pytest.raises(FrontendError):
        validate_tarball(name)


def test_the_stamp_sorts_lexically():
    """Pruning uses `ls -1dt`; a stamp that sorts the same way keeps the two
    orderings from disagreeing."""
    early = stamp(datetime(2026, 8, 20, 9, 5, 0))
    late = stamp(datetime(2026, 8, 20, 17, 30, 0))
    assert early < late
    assert late == SUFFIX


# --- the happy path --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_full_deployment_runs_every_step(deployer, ssh):
    result = await deployer.deploy(TARBALL, now=WHEN)

    assert result["stamp"] == SUFFIX
    assert result["checks"]["http_status"] == "200"
    assert ssh.ran("tar", "-tzf")
    assert ssh.ran("chown", "-R", "root:root")
    assert ssh.ran("restorecon")


@pytest.mark.asyncio
async def test_the_artifact_is_archived_not_the_unpacked_tree(deployer, ssh):
    """The previous dist stays in place for the revert, so archiving the
    unpacked copy too would store the same bytes twice at four times the size."""
    await deployer.deploy(TARBALL, now=WHEN)

    assert ssh.ran("cp", "staging/" + TARBALL, "/backups/ui/" + TARBALL)
    assert not ssh.ran("cp", "-a", f"{WEB}/dist")


# --- extraction ------------------------------------------------------------


@pytest.mark.asyncio
async def test_extraction_never_targets_the_web_root_directly(deployer, ssh):
    """The tarball is rooted at dist/, so extracting into the web root would
    overwrite the live bundle this deployment is supposed to leave alone."""
    await deployer.deploy(TARBALL, now=WHEN)

    extract = ssh.commands[ssh.index_of("tar", "-xzf")]
    assert extract[-1] == f"{WEB}/.stage"
    assert extract[-1] != WEB


@pytest.mark.asyncio
async def test_the_scratch_directory_is_cleared_first_and_removed_after(deployer, ssh):
    await deployer.deploy(TARBALL, now=WHEN)

    assert ssh.index_of("rm", "-rf", f"{WEB}/.stage") < ssh.index_of("tar", "-xzf")
    assert ssh.ran("rmdir", f"{WEB}/.stage")


@pytest.mark.asyncio
async def test_a_tarball_with_an_unexpected_layout_stops_the_deployment(ssh, dry_settings):
    """A layout change must stop, not silently leave dist-new missing."""
    ssh.listing = "build/\nbuild/index.html\n"
    deployer = FrontendDeployer(ssh, dry_settings)

    with pytest.raises(FrontendError, match="rooted at"):
        await deployer.deploy(TARBALL, now=WHEN)
    assert not ssh.ran("mv")


# --- ownership, config, labels --------------------------------------------


@pytest.mark.asyncio
async def test_the_parent_is_made_traversable(deployer, ssh):
    """0750 from root's umask makes nginx answer 500, not 403."""
    await deployer.deploy(TARBALL, now=WHEN)
    assert ssh.ran("chmod", "755", WEB)


@pytest.mark.asyncio
async def test_runtime_config_is_written_before_the_swap(deployer, ssh):
    """So the live dist is never briefly serving a bundle with a blank API_URL."""
    await deployer.deploy(TARBALL, now=WHEN)

    configured = ssh.index_of("aidenops-write-ui-config")
    swapped = ssh.index_of("sh", "mv")
    assert configured < swapped
    assert ssh.commands[configured][-1] == f"{WEB}/dist-new"


@pytest.mark.asyncio
async def test_the_config_command_carries_its_config_path(deployer, ssh):
    await deployer.deploy(TARBALL, now=WHEN)
    written = " ".join(ssh.commands[ssh.index_of("aidenops-write-ui-config")])
    assert "AIDENOPS_CONFIG_PATH=/home/AidenAI/ops1/config.yaml" in written


@pytest.mark.asyncio
async def test_a_blank_api_url_stops_the_deployment(ssh, dry_settings):
    """The observed failure: the UI loads and talks to nothing."""
    ssh.api_url = 'API_URL: ""'
    deployer = FrontendDeployer(ssh, dry_settings)

    with pytest.raises(FrontendError, match="empty API_URL"):
        await deployer.deploy(TARBALL, now=WHEN)

    # Caught while still on dist-new, so the live bundle was never swapped.
    assert not ssh.ran("mv \"$1\" \"$2\"")


# --- the swap --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_swap_is_one_command(deployer, ssh):
    """Two moves cannot be atomic, so the window is kept to local filesystem
    work rather than a network round trip."""
    await deployer.deploy(TARBALL, now=WHEN)

    swap = ssh.commands[ssh.index_of("sh", "mv")]
    assert swap[0] == "sh"
    assert swap[2] == 'mv "$1" "$2" && mv "$3" "$4"'


@pytest.mark.asyncio
async def test_paths_are_passed_positionally_not_interpolated(deployer, ssh):
    """The script body is a fixed literal, so no path becomes shell text."""
    await deployer.deploy(TARBALL, now=WHEN)

    swap = ssh.commands[ssh.index_of("sh", "mv")]
    assert WEB not in swap[2]
    assert f"{WEB}/dist" in swap[4:]


# --- verification and revert ----------------------------------------------


@pytest.mark.asyncio
async def test_a_non_200_reverts_and_reports_it(ssh, dry_settings):
    ssh.http = "500"
    deployer = FrontendDeployer(ssh, dry_settings)

    with pytest.raises(FrontendError) as caught:
        await deployer.deploy(TARBALL, now=WHEN)

    assert caught.value.reverted is True
    assert "previous bundle has been restored" in str(caught.value)
    assert ssh.ran("dist.failed-" + SUFFIX)


@pytest.mark.asyncio
async def test_the_revert_puts_the_previous_bundle_back(ssh, dry_settings):
    ssh.http = "500"
    deployer = FrontendDeployer(ssh, dry_settings)

    with pytest.raises(FrontendError):
        await deployer.deploy(TARBALL, now=WHEN)

    # sh -c <script> sh  $1=live  $2=failed  $3=previous  $4=live
    revert = ssh.commands[-2]
    assert revert[4] == f"{WEB}/dist"
    assert revert[5] == f"{WEB}/dist.failed-{SUFFIX}"
    assert revert[6] == f"{WEB}/dist.bak-{SUFFIX}"
    assert revert[7] == f"{WEB}/dist"


@pytest.mark.asyncio
async def test_the_revert_does_not_reload_nginx(ssh, dry_settings):
    """nginx resolves `root` per request; both real redeployments here worked
    without touching it."""
    ssh.http = "500"
    deployer = FrontendDeployer(ssh, dry_settings)

    with pytest.raises(FrontendError):
        await deployer.deploy(TARBALL, now=WHEN)

    assert not ssh.ran("nginx")


@pytest.mark.asyncio
async def test_nothing_is_pruned_when_verification_fails(ssh, dry_settings):
    """The previous and the failed bundle are both worth keeping at that point."""
    ssh.http = "500"
    deployer = FrontendDeployer(ssh, dry_settings)

    with pytest.raises(FrontendError):
        await deployer.deploy(TARBALL, now=WHEN)

    assert not ssh.ran("ls -1dt")


# --- retention ------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_bundles_and_archives_are_pruned(deployer, ssh):
    """Three directories leaked before this existed, on the volume that has
    already filled once."""
    await deployer.deploy(TARBALL, now=WHEN)

    assert ssh.ran("dist.bak-*")
    assert ssh.ran("dist.failed-*")
    assert ssh.ran("/backups/ui/*.tar.gz")


@pytest.mark.asyncio
async def test_pruning_keeps_the_configured_number(deployer, ssh):
    await deployer.deploy(TARBALL, now=WHEN)

    prune = ssh.commands[ssh.index_of("ls -1dt", "dist.bak-*")]
    assert prune[-1] == "1"
    assert "dist.bak-*" not in prune[2]  # the pattern is positional, not inlined


@pytest.mark.asyncio
async def test_a_failing_step_names_the_stage(ssh, dry_settings):
    ssh.fail_on = ("chown",)
    deployer = FrontendDeployer(ssh, dry_settings)

    with pytest.raises(FrontendError) as caught:
        await deployer.deploy(TARBALL, now=WHEN)

    assert caught.value.stage == "own"
    assert caught.value.reverted is False


# --- modes are not taken on trust ----------------------------------------


@pytest.mark.asyncio
async def test_modes_are_normalised_after_extraction(deployer, ssh):
    """A bundle built on Windows carries no POSIX modes, so tarfile records
    something permissive - the first real deployment extracted as 777, which on
    this server means any local account could replace the UI."""
    await deployer.deploy(TARBALL, now=WHEN)

    assert ssh.ran("find", f"{WEB}/dist-new", "-type", "d", "chmod", "755")
    assert ssh.ran("find", f"{WEB}/dist-new", "-type", "f", "chmod", "644")


@pytest.mark.asyncio
async def test_modes_are_set_before_the_swap(deployer, ssh):
    """Otherwise the live directory is briefly world-writable."""
    await deployer.deploy(TARBALL, now=WHEN)

    assert ssh.index_of("chmod", "644") < ssh.index_of("sh", "mv")
