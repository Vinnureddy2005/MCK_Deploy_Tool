"""Service selection, checksum validation, filename and path safety."""

from __future__ import annotations

from datetime import date

import pytest

from app import config
from app.config import (
    ValidationError,
    backup_dir,
    binaries_path,
    get_service,
    jar_filename,
    list_services,
    systemd_path,
    validate_checksum,
    validate_jar_filename,
    validate_pid,
    validate_port,
    validate_unit_name,
    validate_version,
)
from tests.conftest import VALID_CHECKSUM


# --- service selection -----------------------------------------------------


def test_all_configured_services_are_listed(dry_settings):
    keys = {service["key"] for service in list_services()}
    assert keys == {"tx-test-mgmt", "ai-dap-app", "tx-integration-agent"}


@pytest.mark.parametrize(
    "key,jar,unit",
    [
        ("tx-test-mgmt", "tx-test-mgmt-1.6.0.jar", "aiTXTTestMgmt.service"),
        ("ai-dap-app", "ai-dap-app-1.6.0.jar", "aiDAPApp.service"),
        ("tx-integration-agent", "tx-integration-agent-1.6.0.jar", "aiTXIntegrationAgent.service"),
    ],
)
def test_jar_and_unit_mapping(dry_settings, key, jar, unit):
    assert jar_filename(key) == jar
    assert get_service(key)["systemd_service"] == unit


@pytest.mark.parametrize("key", ["", "unknown", "../etc", "tx-test-mgmt ", None, 5])
def test_unknown_service_is_rejected(dry_settings, key):
    with pytest.raises(ValidationError):
        get_service(key)


def test_version_override_changes_the_jar(dry_settings):
    assert jar_filename("tx-test-mgmt", "1.7.1") == "tx-test-mgmt-1.7.1.jar"


@pytest.mark.parametrize("version", ["1.6.0-SNAPSHOT", "2.0", "10.11.12.13"])
def test_valid_versions(dry_settings, version):
    assert validate_version(version) == version


@pytest.mark.parametrize("version", ["1.6.0; rm -rf /", "../1.0", "v1.6.0", "1.6.0/../2.0", "1.6.0 1.7.0"])
def test_invalid_versions_are_rejected(dry_settings, version):
    with pytest.raises(ValidationError):
        jar_filename("tx-test-mgmt", version)


@pytest.mark.parametrize("version", ["", "   ", None])
def test_blank_version_falls_back_to_the_default(dry_settings, version):
    assert jar_filename("tx-test-mgmt", version) == "tx-test-mgmt-1.6.0.jar"


def test_surrounding_whitespace_is_trimmed(dry_settings):
    assert jar_filename("tx-test-mgmt", " 1.7.0 ") == "tx-test-mgmt-1.7.0.jar"


# --- checksum --------------------------------------------------------------


def test_valid_checksum_passes(dry_settings):
    assert validate_checksum(f"  {VALID_CHECKSUM}  ") == VALID_CHECKSUM


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        None,
        "not-a-checksum",
        "abc123",  # too short
        VALID_CHECKSUM[:-1],  # 63 chars
        VALID_CHECKSUM + "a",  # 65 chars
        "zzzz" + VALID_CHECKSUM[4:],  # non-hex
    ],
)
def test_invalid_checksums_are_rejected(dry_settings, value):
    with pytest.raises(ValidationError):
        validate_checksum(value)


@pytest.mark.parametrize(
    "payload",
    [
        f'{VALID_CHECKSUM}"\nExecStart=/bin/sh',   # break out of the quoted value
        f"{VALID_CHECKSUM}$(whoami)",
        f"{VALID_CHECKSUM}`id`",
        f"{VALID_CHECKSUM} extra",
        f"{VALID_CHECKSUM}\r\nEnvironment=EVIL=1",
    ],
)
def test_checksum_injection_attempts_are_rejected(dry_settings, payload):
    with pytest.raises(ValidationError):
        validate_checksum(payload)


def test_checksum_pattern_is_configurable(monkeypatch, tmp_path):
    monkeypatch.setenv("CHECKSUM_PATTERN", r"^[a-z0-9]{8}$")
    config.reload_settings()
    try:
        assert validate_checksum("abcd1234") == "abcd1234"
        with pytest.raises(ValidationError):
            validate_checksum(VALID_CHECKSUM)
    finally:
        config.reload_settings()


# --- filenames and paths ---------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "..\\windows\\system32",
        "tx-test-mgmt-1.6.0.jar; rm -rf /",
        "tx-test-mgmt-1.6.0.txt",
        "sub/dir/app.jar",
        "",
    ],
)
def test_path_traversal_and_bad_filenames_are_rejected(dry_settings, filename):
    with pytest.raises(ValidationError):
        validate_jar_filename(filename)


def test_unit_must_belong_to_a_managed_service(dry_settings):
    assert validate_unit_name("aiDAPApp.service") == "aiDAPApp.service"
    for bad in ["sshd.service", "nginx.service", "../../etc/passwd", "aiDAPApp"]:
        with pytest.raises(ValidationError):
            validate_unit_name(bad)


def test_remote_paths_are_built_from_config(dry_settings):
    assert binaries_path("tx-test-mgmt-1.6.0.jar") == "/home/AidenAI/binaries/tx-test-mgmt-1.6.0.jar"
    assert systemd_path("aiTXTTestMgmt.service") == "/etc/systemd/system/aiTXTTestMgmt.service"


# --- backup path generation ------------------------------------------------


def test_dated_folder_uses_the_manual_convention(dry_settings):
    """Aug15, not 2026-08-15 - matches the folders created by hand today."""
    from app.config import backup_date_folder, copydata_dir

    assert backup_date_folder(date(2026, 8, 15)) == "Aug15"
    assert backup_date_folder(date(2026, 12, 1)) == "Dec01"
    # the same folder name is used for CopyData and for backups
    assert copydata_dir(date(2026, 8, 15)) == "/home/day6sio/CopyData/Aug15"
    assert backup_dir(date(2026, 8, 15)) == "/home/AidenAI/binaries/backups/Aug15"


def test_date_format_is_configurable(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKUP_DATE_FORMAT", "%Y-%m-%d")
    config.reload_settings()
    try:
        assert backup_dir(date(2026, 8, 15)) == "/home/AidenAI/binaries/backups/2026-08-15"
    finally:
        config.reload_settings()


def test_nested_backup_path(dry_settings):
    assert backup_dir(date(2026, 8, 14)) == "/home/AidenAI/binaries/backups/Aug14"


def test_flat_backup_path_matches_the_manual_convention(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKUP_LAYOUT", "flat")
    config.reload_settings()
    try:
        assert backup_dir(date(2026, 8, 14)) == "/home/AidenAI/binaries/Aug14"
    finally:
        config.reload_settings()


# --- ports and PIDs --------------------------------------------------------


def test_port_validation(dry_settings):
    assert validate_port("8096") == 8096
    for bad in [0, -1, 70000, "http", None]:
        with pytest.raises(ValidationError):
            validate_port(bad)


def test_pid_1_is_never_killable(dry_settings):
    assert validate_pid("12345") == 12345
    for bad in [1, 0, -5, "abc", None]:
        with pytest.raises(ValidationError):
            validate_pid(bad)
