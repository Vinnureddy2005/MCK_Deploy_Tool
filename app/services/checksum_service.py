"""Safe, non-interactive editing of APP_CHECKSUM in a systemd unit file.

Replaces the manual `vim /etc/systemd/system/<unit>` step. Only the checksum
value is touched; every other byte of the unit file is preserved.
"""

from __future__ import annotations

import re

from app.config import ValidationError, validate_checksum

# Matches, on an Environment= line:
#   Environment="APP_CHECKSUM=abc123"
#   Environment=APP_CHECKSUM=abc123
#   Environment='APP_CHECKSUM=abc123'
_CHECKSUM_LINE = re.compile(
    r"""(?P<prefix>^[ \t]*Environment[ \t]*=[ \t]*(?P<quote>["']?)APP_CHECKSUM[ \t]*=[ \t]*)"""
    r"""(?P<value>[^"'\s]*)"""
    r"""(?P<suffix>(?P=quote)[ \t]*)$""",
    re.MULTILINE,
)


class ChecksumError(RuntimeError):
    """The unit file could not be updated safely."""


def extract_checksum(unit_content: str) -> str | None:
    """Return the current APP_CHECKSUM value, or None when absent."""
    match = _CHECKSUM_LINE.search(unit_content or "")
    return match.group("value") if match else None


def count_checksum_lines(unit_content: str) -> int:
    return len(_CHECKSUM_LINE.findall(unit_content or ""))


def replace_checksum(unit_content: str, new_checksum: str) -> tuple[str, str]:
    """Return (updated_content, previous_checksum).

    Raises ChecksumError unless the file contains exactly one APP_CHECKSUM
    line, so an unexpected unit file is never rewritten.
    """
    if not unit_content or not unit_content.strip():
        raise ChecksumError("Unit file is empty - refusing to modify it")

    new_checksum = validate_checksum(new_checksum)

    occurrences = count_checksum_lines(unit_content)
    if occurrences == 0:
        raise ChecksumError(
            'No Environment="APP_CHECKSUM=..." line found in the unit file. '
            "Refusing to modify a file that does not match the expected format."
        )
    if occurrences > 1:
        raise ChecksumError(
            f"Found {occurrences} APP_CHECKSUM lines in the unit file. "
            "Refusing to guess which one to update - fix the unit file manually."
        )

    previous = extract_checksum(unit_content) or ""

    def _sub(match: re.Match) -> str:
        return f"{match.group('prefix')}{new_checksum}{match.group('suffix')}"

    updated = _CHECKSUM_LINE.sub(_sub, unit_content, count=1)

    # Post-conditions: the new value is present and nothing else moved.
    if extract_checksum(updated) != new_checksum:
        raise ChecksumError("Verification failed: new checksum is not present after replacement")
    if len(updated.splitlines()) != len(unit_content.splitlines()):
        raise ChecksumError("Verification failed: line count changed during replacement")
    if previous and previous != new_checksum:
        expected_delta = len(new_checksum) - len(previous)
        if len(updated) - len(unit_content) != expected_delta:
            raise ChecksumError("Verification failed: unexpected change outside the checksum value")

    return updated, previous


def verify_unit_is_expected(unit_content: str, unit_name: str, jar_filename: str | None = None) -> None:
    """Confirm we are editing the unit we think we are before writing it back.

    Guards against a mistyped/misresolved path silently rewriting the wrong
    service. Raises ChecksumError on a mismatch.
    """
    if not unit_content.strip():
        raise ChecksumError(f"{unit_name}: unit file is empty or unreadable")
    if "[Service]" not in unit_content:
        raise ChecksumError(f"{unit_name}: file does not look like a systemd unit ([Service] missing)")
    if jar_filename and jar_filename not in unit_content:
        raise ChecksumError(
            f"{unit_name}: unit file does not reference {jar_filename}. "
            "The selected service and JAR may not match this server."
        )


def validate_for_deployment(checksum: str, service_key: str) -> str:
    """Full pre-deployment validation of the pasted checksum."""
    from app.config import get_service

    get_service(service_key)  # allowlist check
    value = validate_checksum(checksum)
    if len(set(value)) <= 2:
        raise ValidationError("Checksum looks like placeholder text, not a real digest")
    return value
