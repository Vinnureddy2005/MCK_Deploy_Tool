"""Backup creation and installation-hub download."""

from __future__ import annotations

from datetime import date

import pytest

from app.services.backup_service import BackupError, BackupExistsError, BackupService
from app.services.download_service import DownloadError, DownloadService
from tests.conftest import FakeSSH

JAR = "tx-test-mgmt-1.6.0.jar"
UNIT = "aiTXTTestMgmt.service"
BIN_JAR = f"/home/AidenAI/binaries/{JAR}"
UNIT_PATH = f"/etc/systemd/system/{UNIT}"
BACKUP = "/home/AidenAI/binaries/backups"


# --- backups ---------------------------------------------------------------


async def test_backup_directory_is_created(fake_ssh, live_settings):
    service = BackupService(fake_ssh, live_settings)
    directory = await service.create_backup_dir(date(2026, 8, 14))
    assert directory == f"{BACKUP}/Aug14"
    assert f"sudo mkdir -p {directory}" in fake_ssh.commands


async def test_existing_jar_is_copied_into_the_backup(live_settings):
    ssh = FakeSSH(live_settings, existing={BIN_JAR, UNIT_PATH})
    service = BackupService(ssh, live_settings)
    result = await service.backup_jar(JAR, when=date(2026, 8, 14))
    assert result["source"] == BIN_JAR
    assert result["destination"] == f"{BACKUP}/Aug14/{JAR}"
    assert f"sudo cp -p {BIN_JAR} {BACKUP}/Aug14/{JAR}" in ssh.commands


async def test_first_deployment_has_no_jar_to_back_up(live_settings):
    ssh = FakeSSH(live_settings, existing={UNIT_PATH})
    service = BackupService(ssh, live_settings)
    assert await service.backup_jar(JAR) is None
    assert not any(command.startswith("sudo cp") for command in ssh.commands)


async def test_existing_backup_is_never_silently_overwritten(live_settings):
    destination = f"{BACKUP}/Aug14/{JAR}"
    ssh = FakeSSH(live_settings, existing={BIN_JAR, UNIT_PATH, destination})
    service = BackupService(ssh, live_settings)

    with pytest.raises(BackupExistsError):
        await service.backup_jar(JAR, when=date(2026, 8, 14))
    assert not any(command.startswith("sudo cp") for command in ssh.commands)

    result = await service.backup_jar(JAR, when=date(2026, 8, 14), overwrite=True)
    assert result["destination"] == destination


async def test_only_the_selected_unit_file_is_backed_up(live_settings):
    ssh = FakeSSH(live_settings, existing={BIN_JAR, UNIT_PATH})
    service = BackupService(ssh, live_settings)
    result = await service.backup_unit_file(UNIT, when=date(2026, 8, 14))
    assert result["destination"] == f"{BACKUP}/Aug14/{UNIT}"
    copies = [c for c in ssh.commands if c.startswith("sudo cp")]
    assert len(copies) == 1
    assert "aiDAPApp.service" not in " ".join(ssh.commands)


async def test_missing_unit_file_stops_the_backup(live_settings):
    ssh = FakeSSH(live_settings, existing={BIN_JAR})
    service = BackupService(ssh, live_settings)
    with pytest.raises(BackupError, match="does not exist"):
        await service.backup_unit_file(UNIT)


async def test_run_backs_up_jar_and_unit_together(live_settings):
    ssh = FakeSSH(live_settings, existing={BIN_JAR, UNIT_PATH})
    service = BackupService(ssh, live_settings)
    result = await service.run(JAR, UNIT, when=date(2026, 8, 14))
    assert {item["type"] for item in result.items} == {"jar", "unit"}


async def test_connected_dry_run_reports_a_missing_jar(monkeypatch, tmp_path):
    """Phase 2: a dry run WITH a connection must still verify the paths."""
    from app import config

    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("DRY_RUN_CONNECT", "true")
    settings = config.reload_settings()
    try:
        # unit file present, JAR absent -> wrong REMOTE_BINARIES_DIR
        ssh = FakeSSH(settings, existing={UNIT_PATH})
        result = await BackupService(ssh, settings).run(JAR, UNIT, when=date(2026, 8, 14))
        assert result.skipped and "No existing" in result.skipped[0]
        # the JAR must never be "backed up" when it is not there
        assert not any(c.startswith("sudo cp") and JAR in c for c in ssh.commands)
    finally:
        config.reload_settings()


async def test_connected_dry_run_still_refuses_a_missing_unit_file(monkeypatch, tmp_path):
    from app import config

    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("DRY_RUN_CONNECT", "true")
    settings = config.reload_settings()
    try:
        ssh = FakeSSH(settings, existing={BIN_JAR})
        with pytest.raises(BackupError, match="does not exist"):
            await BackupService(ssh, settings).backup_unit_file(UNIT)
    finally:
        config.reload_settings()


async def test_dry_run_backup_touches_nothing(dry_settings):
    ssh = FakeSSH(dry_settings, existing={BIN_JAR, UNIT_PATH})
    service = BackupService(ssh, dry_settings)
    result = await service.run(JAR, UNIT, when=date(2026, 8, 14))
    assert result.simulated
    assert not any(command.startswith("sudo cp") for command in ssh.commands)


# --- download --------------------------------------------------------------


def test_download_url_is_built_from_env(live_settings):
    service = DownloadService(live_settings)
    url = service.build_url(JAR)
    assert url.startswith("http://hub.example.test:8081/api/installation-hubs/path?")
    assert f"filename={JAR}" in url
    assert "code=TEST-CODE-123" in url


def test_hub_filename_defaults_to_the_jar_name(dry_settings):
    from app.config import hub_filename

    assert hub_filename("tx-test-mgmt") == JAR


def test_hub_filename_override_is_used_when_set(live_settings, monkeypatch):
    """Some hubs use their own identifier instead of the JAR name."""
    from app import config

    monkeypatch.setitem(config.SERVICES["tx-test-mgmt"], "hub_filename", "opsBinaries")

    assert config.hub_filename("tx-test-mgmt") == "opsBinaries"
    # the JAR we store locally and upload is unaffected by the hub identifier
    assert config.jar_filename("tx-test-mgmt") == JAR

    url = DownloadService(live_settings).build_url(config.hub_filename("tx-test-mgmt"))
    assert "filename=opsBinaries" in url


@pytest.mark.parametrize("bad", ["../../etc/passwd", "ops/Binaries", "ops Binaries", "", "ops;rm -rf /"])
def test_hub_filename_rejects_unsafe_values(dry_settings, bad):
    from app.config import ValidationError, validate_hub_filename

    with pytest.raises(ValidationError):
        validate_hub_filename(bad)


def test_installation_code_is_redacted_in_logs(live_settings):
    service = DownloadService(live_settings)
    assert "TEST-CODE-123" not in service.redact(service.build_url(JAR))


def test_download_url_rejects_a_crafted_filename(live_settings):
    from app.config import ValidationError

    service = DownloadService(live_settings)
    with pytest.raises(ValidationError):
        service.build_url("../../../etc/passwd")


def test_missing_installation_code_is_reported(monkeypatch, tmp_path, live_settings):
    monkeypatch.setenv("INSTALLATION_CODE", "")
    from app import config

    settings = config.reload_settings()
    try:
        with pytest.raises(DownloadError, match="INSTALLATION_CODE"):
            DownloadService(settings).build_url(JAR)
    finally:
        config.reload_settings()


async def test_dry_run_download_makes_no_request(dry_settings):
    result = await DownloadService(dry_settings).download("tx-test-mgmt")
    assert result.simulated
    assert result.filename == JAR
    assert not result.path.exists()


def test_html_error_page_is_not_accepted_as_a_jar(tmp_path, live_settings):
    fake = tmp_path / "fake.jar"
    fake.write_bytes(b"<html><body>Not found</body></html>")
    assert DownloadService._looks_like_jar(fake) is False

    real = tmp_path / "real.jar"
    real.write_bytes(b"PK\x03\x04rest-of-archive")
    assert DownloadService._looks_like_jar(real) is True
