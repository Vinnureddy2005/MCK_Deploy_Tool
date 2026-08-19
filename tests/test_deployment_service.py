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
    assert result["service"]["unit"] == "aiTXTestMgmt.service"
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
    assert state.unit == "aiTXTestMgmt.service"
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
    assert "Would execute: systemctl restart aiTXTestMgmt.service" in joined


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
    ssh = FakeSSH(live_settings, existing={"/etc/systemd/system/aiTXTestMgmt.service"})
    service = build(live_settings, ssh)

    result = await service.update_checksum("tx-test-mgmt", VALID_CHECKSUM)
    written = ssh.written["/etc/systemd/system/aiTXTestMgmt.service"]

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


def test_app_log_paths_match_the_files_on_the_server(dry_settings):
    """Verified with `ls /var/www/webdav` - the names do not follow the JAR."""
    from app.config import service_log_file

    assert service_log_file("tx-integration-agent") == "/var/www/webdav/tx-integration-agent.log"
    assert service_log_file("tx-test-mgmt") == "/var/www/webdav/txTestMgmt.log"
    assert service_log_file("ai-dap-app") == "/var/www/webdav/aiDAPApp.log"


async def test_mismatched_checksum_stops_before_the_server_is_touched(monkeypatch, tmp_path):
    """A checksum that is not the JAR's digest guarantees a failed startup."""
    from app import config

    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DRY_RUN_CONNECT", "true")
    monkeypatch.setenv("TEMP_DIR", str(tmp_path))
    monkeypatch.setenv("INSTALLATION_HUB_URL", "http://hub.example.test/api/installation-hubs/path")
    monkeypatch.setenv("INSTALLATION_CODE", "TEST-CODE-123")
    monkeypatch.setenv("SSH_PASSWORD", "test-only")
    monkeypatch.setenv("HEALTH_CHECK_DELAY", "0")
    settings = config.reload_settings()
    try:
        ssh = FakeSSH(settings)
        service = build(settings, ssh)

        async def fake_download(service_key, version=None):
            # the hub served a different build from the one the checksum names
            return {
                "filename": "tx-integration-agent-1.6.0.jar",
                "path": str(tmp_path / "x.jar"),
                "size_mb": 193.42,
                "sha256": "f9bc3cbd2496f6157c35a6c9b2789516568f4e03cb28e67416399ff3456280d8",
                "simulated": False,
            }

        service.download_jar = fake_download
        state = await service.deploy("tx-integration-agent", VALID_CHECKSUM)

        assert state.status == "failed"
        assert state.error_stage == "download"
        assert "does not match" in state.error
        # the whole point: nothing on the server may have been touched
        assert not any(
            word in command
            for command in ssh.commands
            for word in ("cp ", "tee ", "restart", "mkdir")
        ), ssh.commands
        assert ssh.written == {}
        for stage in ("upload_to_copydata", "backup", "update_checksum", "restart"):
            assert state.stages[stage]["status"] == "skipped", stage
    finally:
        config.reload_settings()


async def test_matching_checksum_is_confirmed_in_the_log(monkeypatch, tmp_path):
    from app import config

    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DRY_RUN_CONNECT", "true")
    monkeypatch.setenv("TEMP_DIR", str(tmp_path))
    monkeypatch.setenv("INSTALLATION_HUB_URL", "http://hub.example.test/api/installation-hubs/path")
    monkeypatch.setenv("INSTALLATION_CODE", "TEST-CODE-123")
    monkeypatch.setenv("SSH_PASSWORD", "test-only")
    settings = config.reload_settings()
    try:
        service = build(settings, FakeSSH(settings))

        async def fake_download(service_key, version=None):
            return {
                "filename": "tx-integration-agent-1.6.0.jar",
                "path": str(tmp_path / "x.jar"),
                "size_mb": 1.0,
                "sha256": VALID_CHECKSUM,
                "simulated": False,
            }

        service.download_jar = fake_download
        service.state.checksum = VALID_CHECKSUM
        await service._stage_download("tx-integration-agent", None)

        messages = [e["message"] for e in service.broadcaster.history if e["type"] == "log"]
        assert any("Checksum matches the downloaded JAR" in m for m in messages)
    finally:
        config.reload_settings()


async def test_the_check_can_be_disabled(monkeypatch, tmp_path):
    """An override exists, but it warns loudly rather than staying silent."""
    from app import config

    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DRY_RUN_CONNECT", "true")
    monkeypatch.setenv("TEMP_DIR", str(tmp_path))
    monkeypatch.setenv("INSTALLATION_HUB_URL", "http://hub.example.test/api/installation-hubs/path")
    monkeypatch.setenv("INSTALLATION_CODE", "TEST-CODE-123")
    monkeypatch.setenv("SSH_PASSWORD", "test-only")
    monkeypatch.setenv("VERIFY_JAR_CHECKSUM", "false")
    settings = config.reload_settings()
    try:
        service = build(settings, FakeSSH(settings))

        async def fake_download(service_key, version=None):
            return {
                "filename": "tx-integration-agent-1.6.0.jar",
                "path": str(tmp_path / "x.jar"),
                "size_mb": 1.0,
                "sha256": "f9bc3cbd2496f6157c35a6c9b2789516568f4e03cb28e67416399ff3456280d8",
                "simulated": False,
            }

        service.download_jar = fake_download
        service.state.checksum = VALID_CHECKSUM
        await service._stage_download("tx-integration-agent", None)

        warnings = [
            e["message"] for e in service.broadcaster.history if e.get("level") == "warn"
        ]
        assert any("VERIFY_JAR_CHECKSUM is off" in m for m in warnings)
    finally:
        config.reload_settings()


def test_app_log_path_can_be_overridden(dry_settings, monkeypatch):
    from app import config

    monkeypatch.setitem(config.SERVICES["tx-test-mgmt"], "log_file", "/var/log/tx/app.log")
    assert config.service_log_file("tx-test-mgmt") == "/var/log/tx/app.log"


@pytest.mark.parametrize("bad", ["../../etc/passwd", "relative.log", "/var/log/../../etc/shadow", ""])
def test_app_log_path_rejects_traversal(dry_settings, monkeypatch, bad):
    from app import config

    monkeypatch.setitem(config.SERVICES["tx-test-mgmt"], "log_file", bad)
    if bad == "":
        # empty falls back to the default rather than failing
        assert config.service_log_file("tx-test-mgmt").endswith("tx-test-mgmt.log")
    else:
        with pytest.raises(ValidationError):
            config.service_log_file("tx-test-mgmt")


async def test_last_deployment_is_recorded_and_survives_a_restart(dry_settings):
    service = build(dry_settings)
    assert service.last_deployment() is None

    await service.deploy("tx-integration-agent", VALID_CHECKSUM)

    # a fresh instance reads it back from disk, as it would after a restart
    record = build(dry_settings).last_deployment()
    assert record["status"] == "success"
    assert record["display_name"] == "TX Integration Agent"
    assert record["jar"] == "tx-integration-agent-1.6.0.jar"
    assert record["checksum"] == VALID_CHECKSUM
    assert record["dry_run"] is True
    assert record["finished_at"]


async def test_a_failed_deployment_is_recorded_with_the_stage(dry_settings):
    service = build(dry_settings)
    await service.deploy("tx-integration-agent", "not-a-valid-checksum")

    record = service.last_deployment()
    assert record["status"] == "failed"
    assert record["error_stage"] == "validate"
    assert record["error"]


async def test_app_log_tail_is_read_only(live_settings):
    ssh = FakeSSH(live_settings, responses={"tail": CommandResult("", 0, "boot ok\n", "")})
    service = build(live_settings, ssh)

    out = await service.recent_app_log("tx-integration-agent", 20)

    assert out == "boot ok\n"
    assert ssh.commands == ["sudo tail -n 20 /var/www/webdav/tx-integration-agent.log"]


async def test_current_checksum_is_read_only(live_settings):
    ssh = FakeSSH(live_settings)
    service = build(live_settings, ssh)

    await service.get_current_checksum("tx-integration-agent")

    for command in ssh.commands:
        assert not any(w in command for w in ("cp ", "tee ", "restart", "kill", "mkdir")), command


async def test_checksum_stage_refuses_an_unexpected_unit_file(live_settings):
    ssh = FakeSSH(
        live_settings,
        responses={"cat /etc/systemd/system/aiTXTestMgmt.service": CommandResult("", 0, "hello\n", "")},
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
