"""APP_CHECKSUM replacement inside a systemd unit file."""

from __future__ import annotations

import pytest

from app.config import ValidationError
from app.services.checksum_service import (
    ChecksumError,
    extract_checksum,
    replace_checksum,
    verify_unit_is_expected,
)
from tests.conftest import UNIT_FILE, VALID_CHECKSUM

OLD = "aaaabbbbccccddddeeeeffff00001111222233334444555566667777888899990"


def test_extracts_the_current_checksum():
    assert extract_checksum(UNIT_FILE) == OLD


@pytest.mark.parametrize(
    "line",
    [
        'Environment="APP_CHECKSUM=%s"',
        "Environment=APP_CHECKSUM=%s",
        "Environment='APP_CHECKSUM=%s'",
        '   Environment = "APP_CHECKSUM=%s"',
    ],
)
def test_all_quoting_styles_are_handled(line):
    content = f"[Service]\n{line % OLD}\nExecStart=/usr/bin/java\n"
    updated, previous = replace_checksum(content, VALID_CHECKSUM)
    assert previous == OLD
    assert extract_checksum(updated) == VALID_CHECKSUM


def test_only_the_checksum_value_changes():
    updated, previous = replace_checksum(UNIT_FILE, VALID_CHECKSUM)
    assert previous == OLD
    assert updated == UNIT_FILE.replace(OLD, VALID_CHECKSUM)
    # every other line is byte-identical
    for before, after in zip(UNIT_FILE.splitlines(), updated.splitlines()):
        if "APP_CHECKSUM" not in before:
            assert before == after


def test_other_environment_lines_are_untouched():
    updated, _ = replace_checksum(UNIT_FILE, VALID_CHECKSUM)
    assert 'Environment="SPRING_PROFILES_ACTIVE=prod"' in updated
    assert "ExecStart=/usr/bin/java -jar /home/AidenAI/binaries/tx-test-mgmt-1.6.0.jar" in updated


def test_missing_checksum_line_stops_the_deployment():
    content = "[Service]\nExecStart=/usr/bin/java -jar app.jar\n"
    with pytest.raises(ChecksumError, match="No Environment"):
        replace_checksum(content, VALID_CHECKSUM)


def test_multiple_checksum_lines_are_refused():
    content = UNIT_FILE + f'Environment="APP_CHECKSUM={OLD}"\n'
    with pytest.raises(ChecksumError, match="Found 2"):
        replace_checksum(content, VALID_CHECKSUM)


def test_empty_unit_file_is_refused():
    with pytest.raises(ChecksumError):
        replace_checksum("   ", VALID_CHECKSUM)


def test_invalid_checksum_never_reaches_the_file(dry_settings):
    with pytest.raises(ValidationError):
        replace_checksum(UNIT_FILE, 'abc" \nExecStart=/bin/sh')


def test_unit_file_identity_is_verified():
    verify_unit_is_expected(UNIT_FILE, "aiTXTTestMgmt.service", "tx-test-mgmt-1.6.0.jar")

    with pytest.raises(ChecksumError, match="does not reference"):
        verify_unit_is_expected(UNIT_FILE, "aiTXTTestMgmt.service", "ai-dap-app-1.6.0.jar")

    with pytest.raises(ChecksumError, match="systemd unit"):
        verify_unit_is_expected("just some text\n", "aiTXTTestMgmt.service")


def test_replacement_is_idempotent():
    once, _ = replace_checksum(UNIT_FILE, VALID_CHECKSUM)
    twice, previous = replace_checksum(once, VALID_CHECKSUM)
    assert previous == VALID_CHECKSUM
    assert twice == once
