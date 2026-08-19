"""SFTP robustness and the CopyData-first stage order.

Covers the failure seen in production: an SFTP channel dies mid-transfer, the
SSH transport survives, and the next attempt reuses the dead channel and fails
instantly with "Socket is closed".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import copydata_dir
from app.services.deployment_service import STAGES, DeploymentService
from app.services.sftp_service import SFTPService, UploadError
from app.services.ssh_service import CommandResult, SSHError
from tests.conftest import VALID_CHECKSUM, FakeSFTP, FakeSSH

JAR = "tx-integration-agent-1.6.0.jar"
UNIT = "aiTXIntegrationAgent.service"
BIN_JAR = f"/home/AidenAI/binaries/{JAR}"
UNIT_PATH = f"/etc/systemd/system/{UNIT}"

LARGE = 202_820_077  # the real 193.42 MB tx-integration-agent JAR


def make_jar(tmp_path: Path, size: int = 1024) -> Path:
    jar = tmp_path / JAR
    jar.write_bytes(b"PK\x03\x04" + b"\0" * (size - 4))
    return jar


def build(settings, ssh) -> DeploymentService:
    service = DeploymentService(settings)
    service.ssh = ssh
    service.sftp.ssh = ssh
    service.backups.ssh = ssh
    service.streamer.ssh = ssh
    return service


# --- 7. ordering -----------------------------------------------------------


def test_copydata_upload_comes_before_backup():
    assert STAGES.index("upload_to_copydata") < STAGES.index("backup")


def test_stage_order_matches_the_manual_procedure():
    assert STAGES == [
        "validate",
        "download",
        "connect",
        "upload_to_copydata",
        "backup",
        "update_checksum",
        "daemon_reload",
        "copy_to_binaries",
        "restart",
        "health_check",
        "live_logs",
    ]


# --- 6. upload verification ------------------------------------------------


async def test_upload_goes_to_copydata_not_binaries(live_settings, tmp_path):
    sftp = FakeSFTP()
    ssh = FakeSSH(live_settings, sftp=sftp)
    jar = make_jar(tmp_path)

    result = await SFTPService(ssh, live_settings).upload_to_copydata(jar, JAR)

    assert result.staged_path == f"{copydata_dir()}/{JAR}"
    assert "/home/AidenAI/binaries" not in result.staged_path
    # the binaries directory must not be written to during this stage
    assert not any(c.startswith("sudo cp") for c in ssh.commands)


async def test_upload_uses_a_part_file_then_renames(live_settings, tmp_path):
    sftp = FakeSFTP()
    ssh = FakeSSH(live_settings, sftp=sftp)
    jar = make_jar(tmp_path)

    await SFTPService(ssh, live_settings).upload_to_copydata(jar, JAR)

    uploaded_to = sftp.put_calls[0][1]
    assert uploaded_to.endswith(".part"), "must upload to .part first"
    assert sftp.renames == [(f"{copydata_dir()}/{JAR}.part", f"{copydata_dir()}/{JAR}")]
    assert f"{copydata_dir()}/{JAR}" in sftp.files


async def test_size_mismatch_is_rejected(live_settings, tmp_path):
    class Truncating(FakeSFTP):
        def put(self, local, remote, callback=None):
            self.put_calls.append((local, remote))
            self.files[remote] = 10  # short write
            return None

    ssh = FakeSSH(live_settings, sftp=Truncating())
    jar = make_jar(tmp_path, 2048)

    with pytest.raises(UploadError, match="truncated"):
        await SFTPService(ssh, live_settings).upload_to_copydata(jar, JAR)


async def test_partial_file_is_never_promoted(live_settings, tmp_path):
    class Truncating(FakeSFTP):
        def put(self, local, remote, callback=None):
            self.put_calls.append((local, remote))
            self.files[remote] = 10
            return None

    sftp = Truncating()
    ssh = FakeSSH(live_settings, sftp=sftp)

    with pytest.raises(UploadError):
        await SFTPService(ssh, live_settings).upload_to_copydata(make_jar(tmp_path, 2048), JAR)

    assert sftp.renames == [], "a short transfer must never be renamed to the real name"
    assert f"{copydata_dir()}/{JAR}" not in sftp.files


# --- 1/2/3/5. stale connection, closed socket, reconnect, retry -----------


async def test_closed_socket_triggers_reconnect_and_one_retry(live_settings, tmp_path):
    sftp = FakeSFTP(fail_puts=1, fail_with=EOFError())
    ssh = FakeSSH(live_settings, sftp=sftp)
    jar = make_jar(tmp_path)

    result = await SFTPService(ssh, live_settings).upload_to_copydata(jar, JAR)

    assert result.attempts == 2
    assert ssh.reconnects == 1, "must reconnect before retrying"
    assert len(sftp.put_calls) == 2
    assert f"{copydata_dir()}/{JAR}" in sftp.files


async def test_socket_is_closed_error_is_retried(live_settings, tmp_path):
    sftp = FakeSFTP(fail_puts=1, fail_with=OSError("Socket is closed"))
    ssh = FakeSSH(live_settings, sftp=sftp)

    result = await SFTPService(ssh, live_settings).upload_to_copydata(make_jar(tmp_path), JAR)

    assert result.attempts == 2
    assert ssh.reconnects == 1


async def test_retry_happens_at_most_once(live_settings, tmp_path):
    sftp = FakeSFTP(fail_puts=5, fail_with=EOFError())
    ssh = FakeSSH(live_settings, sftp=sftp)

    with pytest.raises(UploadError):
        await SFTPService(ssh, live_settings).upload_to_copydata(make_jar(tmp_path), JAR)

    assert len(sftp.put_calls) == 2, "one attempt plus exactly one retry"
    assert ssh.reconnects == 1


async def test_second_failure_reports_the_real_error(live_settings, tmp_path):
    sftp = FakeSFTP(fail_puts=5, fail_with=OSError("Socket is closed"))
    ssh = FakeSSH(live_settings, sftp=sftp)

    with pytest.raises(UploadError, match="Socket is closed"):
        await SFTPService(ssh, live_settings).upload_to_copydata(make_jar(tmp_path), JAR)


async def test_partial_is_cleaned_up_between_attempts(live_settings, tmp_path):
    sftp = FakeSFTP(fail_puts=1, fail_with=EOFError())
    ssh = FakeSSH(live_settings, sftp=sftp)

    await SFTPService(ssh, live_settings).upload_to_copydata(make_jar(tmp_path), JAR)

    assert f"{copydata_dir()}/{JAR}.part" in sftp.removed


async def test_sftp_session_is_closed_after_a_successful_upload(live_settings, tmp_path):
    ssh = FakeSSH(live_settings, sftp=FakeSFTP())

    await SFTPService(ssh, live_settings).upload_to_copydata(make_jar(tmp_path), JAR)

    assert ssh.sftp_closes == 1, "the SFTP session must not be left open for the whole deployment"


async def test_stale_transport_is_reconnected_before_upload(live_settings, tmp_path):
    ssh = FakeSSH(live_settings, sftp=FakeSFTP())
    ssh.connected = False  # transport died since the last stage

    result = await SFTPService(ssh, live_settings).upload_to_copydata(make_jar(tmp_path), JAR)

    assert ssh.reconnects >= 1
    assert result.size_bytes > 0


# --- 4. large file ---------------------------------------------------------


async def test_large_jar_is_transferred_and_verified(live_settings, tmp_path):
    """The real failure was a 193 MB JAR; sizes must be carried exactly."""

    class BigSFTP(FakeSFTP):
        def put(self, local, remote, callback=None):
            self.put_calls.append((local, remote))
            if callback:
                callback(LARGE, LARGE)
            self.files[remote] = LARGE
            return None

    sftp = BigSFTP()
    ssh = FakeSSH(live_settings, sftp=sftp)

    jar = tmp_path / JAR
    jar.write_bytes(b"PK\x03\x04")

    class BigPath(type(jar)):
        pass

    # report the local file as the real 193 MB so the size check is meaningful
    import os

    real_stat = os.stat_result((0o100644, 0, 0, 1, 0, 0, LARGE, 0, 0, 0))
    orig = Path.stat
    Path.stat = lambda self, *a, **k: real_stat if self == jar else orig(self, *a, **k)
    try:
        result = await SFTPService(ssh, live_settings).upload_to_copydata(jar, JAR)
    finally:
        Path.stat = orig

    assert result.size_bytes == LARGE
    assert sftp.files[f"{copydata_dir()}/{JAR}"] == LARGE


async def test_upload_reports_progress(live_settings, tmp_path):
    ssh = FakeSSH(live_settings, sftp=FakeSFTP())
    messages: list[str] = []

    async def progress(message: str) -> None:
        messages.append(message)

    await SFTPService(ssh, live_settings).upload_to_copydata(make_jar(tmp_path), JAR, progress=progress)

    assert any("Uploading" in m for m in messages)


# --- 8. CopyData -> binaries ----------------------------------------------


async def test_copy_to_binaries_uses_the_staged_file(live_settings):
    ssh = FakeSSH(live_settings, responses={"stat -c %s": CommandResult("", 0, "1024\n", "")})

    result = await SFTPService(ssh, live_settings).copy_to_binaries(JAR, 1024)

    assert result["source"] == f"{copydata_dir()}/{JAR}"
    assert result["destination"] == BIN_JAR
    assert f"sudo cp {copydata_dir()}/{JAR} {BIN_JAR}" in ssh.commands


async def test_copy_to_binaries_verifies_the_size(live_settings):
    ssh = FakeSSH(live_settings, responses={"stat -c %s": CommandResult("", 0, "99\n", "")})

    with pytest.raises(UploadError, match="Size mismatch"):
        await SFTPService(ssh, live_settings).copy_to_binaries(JAR, 1024)


async def test_copy_to_binaries_fails_when_the_file_is_absent(live_settings):
    ssh = FakeSSH(live_settings, responses={"stat -c %s": CommandResult("", 1, "", "No such file")})

    with pytest.raises(UploadError, match="does not exist"):
        await SFTPService(ssh, live_settings).copy_to_binaries(JAR, 1024)


# --- 9/10. pipeline behaviour ---------------------------------------------


async def test_failed_copydata_upload_stops_before_any_backup(live_settings, tmp_path, monkeypatch):
    """If staging fails, nothing on the server may be touched."""
    sftp = FakeSFTP(fail_puts=5, fail_with=OSError("Socket is closed"))
    ssh = FakeSSH(live_settings, sftp=sftp, existing={BIN_JAR, UNIT_PATH})
    service = build(live_settings, ssh)

    jar = make_jar(tmp_path)

    async def fake_download(service_key, version=None):
        # must match the pasted checksum, or the download stage stops first
        return {
            "filename": JAR,
            "path": str(jar),
            "size_mb": 0.1,
            "sha256": VALID_CHECKSUM,
            "simulated": False,
        }

    service.download_jar = fake_download
    service._local_jar = jar
    monkeypatch.setattr(service.downloader, "download", lambda *a, **k: None)

    state = await service.deploy("tx-integration-agent", VALID_CHECKSUM)

    assert state.status == "failed"
    assert state.error_stage == "upload_to_copydata"
    assert state.stages["backup"]["status"] == "skipped"
    # no backup, no checksum write, no restart
    assert not any(c.startswith("sudo cp") for c in ssh.commands)
    assert ssh.written == {}
    assert not any("restart" in c for c in ssh.commands)


async def test_backup_runs_only_after_a_successful_upload(live_settings, tmp_path):
    sftp = FakeSFTP()
    ssh = FakeSSH(live_settings, sftp=sftp, existing={BIN_JAR, UNIT_PATH})
    service = build(live_settings, ssh)

    jar = make_jar(tmp_path)
    service._local_jar = jar
    # calling stages directly, so set up the state deploy() would have built
    service.state.jar = JAR
    service.state.unit = UNIT

    await service._stage_upload_to_copydata()
    upload_done = len(ssh.commands)
    await service._stage_backup(overwrite=False)

    assert sftp.put_calls, "upload must have happened first"
    backup_cmds = [c for c in ssh.commands[upload_done:] if c.startswith("sudo cp")]
    assert backup_cmds, "backup copies run after the upload"
