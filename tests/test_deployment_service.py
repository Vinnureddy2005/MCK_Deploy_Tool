"""Deployment stages, port detection and failure handling.

Every test here uses FakeSSH or DRY_RUN. Nothing connects to the McKesson app
server and no destructive command is ever issued.
"""

from __future__ import annotations

import pytest

from app.config import ValidationError
from app.services.deployment_service import STAGES, DeploymentService
from app.services.ssh_service import CommandResult
from tests.conftest import UNIT_FILE, VALID_CHECKSUM, FakeSSH

LSOF_OUTPUT = """COMMAND   PID   USER   FD   TYPE DEVICE SIZE/OFF NODE NAME
java    12345  aiden  120u  IPv6 981234      0t0  TCP *:8096 (LISTEN)
java    12345  aiden  121u  IPv6 981235      0t0  TCP 10.15.128.5:8096->10.1.1.9:5512 (ESTABLISHED)
nginx    9876   root    6u  IPv4 771222      0t0  TCP *:8096 (LISTEN)
"""


def build(settings, ssh: FakeSSH | None = None) -> DeploymentService:
    service = DeploymentService(settings)
    if ssh is not None:
        service.ssh = ssh
        service.sftp.ssh = ssh
        service.backups.ssh = ssh
        service.streamer.ssh = ssh
    return service


# --- validation stage ------------------------------------------------------


async def test_validation_reports_every_check(dry_settings):
    service = build(dry_settings)
    result = await service.validate("tx-test-mgmt", VALID_CHECKSUM)
    names = [check["name"] for check in result["checks"]]
    assert names == ["Service", "Checksum", "JAR", "Systemd unit", "Configuration"]
    assert result["service"]["unit"] == "aiTXTTestMgmt.service"
    assert result["service"]["port"] == 8096


async def test_validation_rejects_a_bad_checksum(dry_settings):
    service = build(dry_settings)
    with pytest.raises(ValidationError):
        await service.validate("tx-test-mgmt", "nope")


async def test_validation_rejects_an_unknown_service(dry_settings):
    service = build(dry_settings)
    with pytest.raises(ValidationError):
        await service.validate("sshd", VALID_CHECKSUM)


# --- full dry run ----------------------------------------------------------


async def test_dry_run_completes_every_stage(dry_settings):
    service = build(dry_settings)
    state = await service.deploy("tx-test-mgmt", VALID_CHECKSUM)

    assert state.status == "success"
    assert state.jar == "tx-test-mgmt-1.6.0.jar"
    assert state.unit == "aiTXTTestMgmt.service"
    for stage in STAGES:
        assert state.stages[stage]["status"] == "completed", stage


async def test_dry_run_announces_actions_without_performing_them(dry_settings):
    service = build(dry_settings)
    await service.deploy("tx-test-mgmt", VALID_CHECKSUM)
    messages = [e["message"] for e in service.broadcaster.history if e["type"] == "log"]
    joined = "\n".join(messages)

    assert "DRY RUN" in joined
    assert "Would download: tx-test-mgmt-1.6.0.jar" in joined
    assert "Would upload tx-test-mgmt-1.6.0.jar" in joined
    assert "Would update APP_CHECKSUM" in joined
    assert "Would execute: systemctl daemon-reload" in joined
    assert "Would execute: systemctl restart aiTXTTestMgmt.service" in joined


async def test_a_failed_stage_stops_the_deployment(dry_settings):
    service = build(dry_settings)
    state = await service.deploy("tx-test-mgmt", "not-a-valid-checksum")

    assert state.status == "failed"
    assert state.error_stage == "validate"
    assert state.stages["validate"]["status"] == "failed"
    for stage in STAGES[1:]:
        assert state.stages[stage]["status"] == "skipped", stage


# --- checksum stage against a fake server ---------------------------------


async def test_checksum_stage_writes_and_verifies(live_settings):
    ssh = FakeSSH(live_settings, existing={"/etc/systemd/system/aiTXTTestMgmt.service"})
    service = build(live_settings, ssh)

    result = await service.update_checksum("tx-test-mgmt", VALID_CHECKSUM)
    written = ssh.written["/etc/systemd/system/aiTXTTestMgmt.service"]

    assert result["new"] == VALID_CHECKSUM
    assert f'Environment="APP_CHECKSUM={VALID_CHECKSUM}"' in written
    assert "ExecStart=/usr/bin/java" in written
    assert "aiDAPApp.service" not in " ".join(ssh.commands)


async def test_unit_file_is_read_without_sudo_when_possible(live_settings):
    """Unit files are world-readable; demanding sudo breaks read-only lookups."""
    ssh = FakeSSH(live_settings, existing={"/etc/systemd/system/aiTXIntegrationAgent.service"})
    service = build(live_settings, ssh)

    result = await service.get_current_checksum("tx-integration-agent")

    assert result["found"] is True
    assert result["checksum"] == "aaaabbbbccccddddeeeeffff00001111222233334444555566667777888899990"


async def test_current_checksum_is_read_only(live_settings):
    ssh = FakeSSH(live_settings)
    service = build(live_settings, ssh)

    await service.get_current_checksum("tx-integration-agent")

    for command in ssh.commands:
        assert not any(w in command for w in ("cp ", "tee ", "restart", "kill", "mkdir")), command


async def test_checksum_stage_refuses_an_unexpected_unit_file(live_settings):
    ssh = FakeSSH(
        live_settings,
        responses={"cat /etc/systemd/system/aiTXTTestMgmt.service": CommandResult("", 0, "hello\n", "")},
    )
    service = build(live_settings, ssh)

    from app.services.checksum_service import ChecksumError

    with pytest.raises(ChecksumError):
        await service.update_checksum("tx-test-mgmt", VALID_CHECKSUM)
    assert ssh.written == {}


# --- systemd operations ----------------------------------------------------


async def test_only_the_selected_service_is_restarted(live_settings):
    ssh = FakeSSH(live_settings)
    service = build(live_settings, ssh)
    await service.restart_service("ai-dap-app")

    restarts = [c for c in ssh.commands if "systemctl restart" in c]
    assert restarts == ["sudo systemctl restart aiDAPApp.service"]


async def test_daemon_reload_is_issued(live_settings):
    ssh = FakeSSH(live_settings)
    service = build(live_settings, ssh)
    assert await service.daemon_reload() == "systemd daemon reloaded"
    assert "sudo systemctl daemon-reload" in ssh.commands


async def test_service_status_reports_running(live_settings):
    ssh = FakeSSH(
        live_settings,
        responses={
            "systemctl is-active": CommandResult("", 0, "active\n", ""),
            "systemctl status": CommandResult("", 0, "Active: active (running)\n", ""),
        },
    )
    service = build(live_settings, ssh)
    status = await service.get_service_status("tx-test-mgmt")
    assert status["is_running"] is True


# --- port conflict ---------------------------------------------------------


def test_lsof_output_is_parsed_into_unique_processes():
    processes = DeploymentService._parse_lsof(LSOF_OUTPUT)
    assert [p["pid"] for p in processes] == [12345, 9876]
    assert processes[0]["command"] == "java"
    assert processes[0]["user"] == "aiden"
    assert processes[1]["command"] == "nginx"


def test_empty_lsof_output_means_the_port_is_free():
    assert DeploymentService._parse_lsof("") == []
    assert DeploymentService._parse_lsof("COMMAND PID USER FD TYPE DEVICE SIZE NODE NAME\n") == []


async def test_port_lookup_is_read_only(live_settings):
    ssh = FakeSSH(live_settings, responses={"lsof": CommandResult("", 0, LSOF_OUTPUT, "")})
    service = build(live_settings, ssh)

    info = await service.find_port_process(8096)
    assert info["occupied"] is True
    assert len(info["processes"]) == 2
    assert not any("kill" in command for command in ssh.commands)


async def test_a_failed_start_raises_a_conflict_but_kills_nothing(live_settings):
    ssh = FakeSSH(live_settings, responses={"lsof": CommandResult("", 0, LSOF_OUTPUT, "")})
    service = build(live_settings, ssh)
    service.state.port = 8096

    await service._diagnose_port_conflict()

    assert service.state.status == "awaiting_confirmation"
    assert service.state.port_conflict["port"] == 8096
    assert any(event["type"] == "port_conflict" for event in service.broadcaster.history)
    assert not any("kill" in command for command in ssh.commands)


async def test_kill_requires_explicit_confirmation(live_settings):
    ssh = FakeSSH(live_settings)
    service = build(live_settings, ssh)

    with pytest.raises(ValidationError, match="confirmation"):
        await service.kill_process(12345, confirmed=False)
    assert not any("kill" in command for command in ssh.commands)


async def test_confirmed_kill_targets_only_that_pid(live_settings):
    ssh = FakeSSH(live_settings)
    service = build(live_settings, ssh)

    result = await service.kill_process(12345, confirmed=True)
    kills = [c for c in ssh.commands if c.startswith("sudo kill")]
    assert kills == ["sudo kill 12345"]
    assert result["pid"] == 12345


async def test_init_process_is_never_killable(live_settings):
    ssh = FakeSSH(live_settings)
    service = build(live_settings, ssh)

    with pytest.raises(ValidationError):
        await service.kill_process(1, confirmed=True)
    assert ssh.commands == []


# --- events ----------------------------------------------------------------


async def test_stage_events_are_broadcast_in_order(dry_settings):
    service = build(dry_settings)
    await service.deploy("tx-test-mgmt", VALID_CHECKSUM)

    running = [e["stage"] for e in service.broadcaster.history if e.get("status") == "running"]
    assert running == STAGES
    assert service.broadcaster.history[-1]["type"] == "complete"
