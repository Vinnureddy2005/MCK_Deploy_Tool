"""The dependency diff.

Not a safeguard around pip - the mechanism itself. The wheel declares zero
dependencies by design, so nothing installs a new package and nothing upgrades
an existing one. Both changes are invisible without this comparison.
"""

from __future__ import annotations

import pytest

from app.services.requirements_service import diff, normalise, parse, summarise

FREEZE = """fastapi==0.115.0
asyncpg==0.29.0
pydantic==2.9.2
"""


# --- parsing --------------------------------------------------------------


def test_exact_pins_are_read():
    assert parse(FREEZE) == {
        "fastapi": "0.115.0",
        "asyncpg": "0.29.0",
        "pydantic": "2.9.2",
    }


@pytest.mark.parametrize(
    "line",
    [
        "# a comment",
        "",
        "   ",
        "--index-url https://example.invalid/simple",
        "-e .",
        "-r other.txt",
        "somepackage",              # no version to compare
        "somepackage>=1.0",         # a range, not a pin
        "git+https://x/y.git#egg=z",
    ],
)
def test_anything_that_is_not_a_pin_is_skipped(line):
    """Guessing a version for these would invent a change that is not there."""
    assert parse(line) == {}


def test_inline_comments_are_stripped():
    assert parse("fastapi==0.115.0  # pinned for the 1.1 release") == {"fastapi": "0.115.0"}


def test_environment_markers_do_not_corrupt_the_version():
    assert parse('uvloop==0.20.0 ; sys_platform != "win32"') == {"uvloop": "0.20.0"}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("aidenops_service", "aidenops-service"),
        ("Aidenops.Service", "aidenops-service"),
        ("AIDENOPS--SERVICE", "aidenops-service"),
        ("  fastapi  ", "fastapi"),
    ],
)
def test_names_are_normalised_per_pep503(raw, expected):
    assert normalise(raw) == expected


def test_a_name_difference_alone_is_not_a_change():
    assert diff("aidenops_service==1.0", "aidenops-service==1.0") == []


# --- the diff -------------------------------------------------------------


def test_a_new_package_is_added():
    changes = diff(FREEZE, FREEZE + "httpx==0.27.2\n")
    assert changes == [{"package": "httpx", "change": "added", "to": "0.27.2"}]


def test_a_moved_pin_is_repinned():
    """As invisible as a new package: nothing upgrades an installed one."""
    changes = diff(FREEZE, "asyncpg==0.30.0\n")
    assert {"package": "asyncpg", "change": "repinned",
            "from": "0.29.0", "to": "0.30.0"} in changes


def test_a_package_no_longer_required_is_reported_as_removed():
    changes = diff(FREEZE, "fastapi==0.115.0\n")
    removed = [c for c in changes if c["change"] == "removed"]
    assert {c["package"] for c in removed} == {"asyncpg", "pydantic"}


def test_no_changes_when_the_pins_match():
    assert diff(FREEZE, FREEZE) == []


def test_additions_come_before_repins_and_removals():
    """Ordered by how much attention each deserves."""
    changes = diff(FREEZE, "httpx==0.27.2\nasyncpg==0.30.0\nfastapi==0.115.0\n")
    assert [c["change"] for c in changes] == ["added", "repinned", "removed"]


# --- what it means for the deployment ------------------------------------


def test_a_release_that_moves_nothing_needs_no_index():
    """This is what keeps a server with no PyPI access deployable: with no
    changes there is nothing to install, so the deploy does not need an index."""
    result = summarise(diff(FREEZE, FREEZE))
    assert result["needs_install"] is False
    assert result["needs_index"] is False
    assert result["probe"] == []


def test_a_removal_alone_needs_no_install():
    """Nothing uninstalls it; it sits on disk harmlessly."""
    result = summarise(diff(FREEZE, "fastapi==0.115.0\n"))
    assert result["needs_install"] is False


def test_an_addition_needs_an_index_and_confirmation():
    result = summarise(diff(FREEZE, FREEZE + "httpx==0.27.2\n"))
    assert result["needs_install"] is True
    assert result["needs_confirmation"] is True


def test_the_probe_list_covers_every_changed_pin():
    """Probing one sample package proves PyPI is up, not that this release
    resolves - so the pip<22.2 fallback loops exactly these."""
    result = summarise(diff(FREEZE, "httpx==0.27.2\nasyncpg==0.30.0\n"))
    assert sorted(result["probe"]) == ["asyncpg==0.30.0", "httpx==0.27.2"]


def test_the_probe_list_excludes_removals():
    result = summarise(diff(FREEZE, "fastapi==0.115.0\n"))
    assert result["probe"] == []
