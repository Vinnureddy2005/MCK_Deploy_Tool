"""Verifying a release archive before anything reaches the server.

Two independent checks: the archive against the hand-carried hash, and its
members against the SHA256SUMS.txt inside it. The first has no override, because
nothing downstream catches a wrong artifact - a wrong wheel installs cleanly,
starts cleanly, and /health returns 200 while running the wrong code.
"""

from __future__ import annotations

import hashlib
import json
import zipfile

import pytest

from app.services.archive_service import (
    ArchiveError,
    classify,
    inspect,
    normalise_checksum,
    sha256_file,
)

WHEEL = "aidenops_service-1.1.0+gd00222c-py3-none-any.whl"
REQS = "requirements-1.1.0+gd00222c.txt"
UI = "aidenops-ui-1.0.0+g635405c.tar.gz"

BODIES = {
    WHEEL: b"wheel bytes",
    REQS: b"fastapi==0.115.0\nasyncpg==0.30.0\n",
    UI: b"tarball bytes",
}


def _archive(tmp_path, bodies=None, sums=None, manifest=True, extra=None):
    """Build a release archive the way the Aiden tool builds one."""
    bodies = BODIES if bodies is None else bodies
    path = tmp_path / "aidenops-d00222c-635405c.zip"

    if sums is None:
        sums = "".join(
            f"{hashlib.sha256(body).hexdigest()}  {name}\n" for name, body in bodies.items()
        )

    with zipfile.ZipFile(path, "w") as zf:
        for name, body in bodies.items():
            zf.writestr(name, body)
        for name, body in (extra or {}).items():
            zf.writestr(name, body)
        if sums is not False:
            zf.writestr("SHA256SUMS.txt", sums)
        if manifest:
            zf.writestr(
                "MANIFEST.json",
                json.dumps({"archive": path.name, "built_by": "vineesh",
                            "builds": [{"commit_short": "d00222c"},
                                       {"commit_short": "635405c"}]}),
            )
    return path


# --- the pasted value ------------------------------------------------------


def test_a_hash_is_accepted_as_pasted():
    """The Aiden tool shows it in blocks of eight, so it arrives with spaces."""
    digest = "a" * 64
    assert normalise_checksum("aaaaaaaa aaaaaaaa " + "a" * 48) == digest
    assert normalise_checksum(digest.upper()) == digest
    assert normalise_checksum(f"  {digest}\n") == digest


@pytest.mark.parametrize(
    "value", ["", None, "not a hash", "a" * 63, "a" * 65, "z" * 64, "1234"]
)
def test_a_value_that_is_not_a_sha256_is_refused(value):
    with pytest.raises(ArchiveError):
        normalise_checksum(value)


# --- the gate --------------------------------------------------------------


def test_a_matching_archive_passes(tmp_path):
    path = _archive(tmp_path)
    result = inspect(path, sha256_file(path))

    assert result["sha256"] == sha256_file(path)
    assert {m["name"] for m in result["verified_members"]} == set(BODIES)
    assert result["contents"]["has_backend"] is True
    assert result["contents"]["has_ui"] is True


def test_a_mismatched_archive_is_refused(tmp_path):
    """The 17 Aug failure class: nothing downstream would catch this."""
    path = _archive(tmp_path)

    with pytest.raises(ArchiveError, match="does not match the checksum"):
        inspect(path, "b" * 64)


def test_the_refusal_shows_both_values(tmp_path):
    """So the operator can see whether they mis-copied or mis-downloaded."""
    path = _archive(tmp_path)
    with pytest.raises(ArchiveError) as caught:
        inspect(path, "b" * 64)

    assert "b" * 64 in str(caught.value)
    assert sha256_file(path) in str(caught.value)
    assert "Nothing has been sent to the server" in str(caught.value)


def test_a_missing_file_is_refused(tmp_path):
    with pytest.raises(ArchiveError, match="is not a file"):
        inspect(tmp_path / "nope.zip", "a" * 64)


# --- the archive's own sums ------------------------------------------------


def test_a_tampered_member_is_caught(tmp_path):
    """The outer hash can only prove the archive; this proves its contents."""
    sums = "".join(
        f"{hashlib.sha256(b'something else').hexdigest()}  {name}\n" for name in BODIES
    )
    path = _archive(tmp_path, sums=sums)

    with pytest.raises(ArchiveError, match="does not match its contents"):
        inspect(path, sha256_file(path))


def test_an_archive_without_sums_is_refused(tmp_path):
    """Unverifiable is not the same as verified - and must not read as it."""
    path = _archive(tmp_path, sums=False)

    with pytest.raises(ArchiveError, match="cannot be verified"):
        inspect(path, sha256_file(path))


def test_a_member_declared_but_absent_fails(tmp_path):
    sums = "".join(
        f"{hashlib.sha256(body).hexdigest()}  {name}\n" for name, body in BODIES.items()
    ) + f"{'c' * 64}  aidenops-ui-9.9.9.tar.gz\n"
    path = _archive(tmp_path, sums=sums)

    with pytest.raises(ArchiveError, match="aidenops-ui-9.9.9.tar.gz"):
        inspect(path, sha256_file(path))


def test_sums_and_manifest_are_not_themselves_failures(tmp_path):
    """SHA256SUMS.txt cannot list itself, and the manifest is metadata."""
    path = _archive(tmp_path)
    result = inspect(path, sha256_file(path))

    unverified = {m["name"] for m in result["unverified_members"]}
    assert unverified == {"SHA256SUMS.txt", "MANIFEST.json"}


# --- tampering -------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["../../etc/cron.d/evil", "/etc/passwd", "sub/dir/file.whl", "-rf.whl", "a b.whl"],
)
def test_an_unsafe_member_name_is_refused(tmp_path, name):
    """These reach `unzip` on the server. unzip would refuse to escape its
    target, but an archive containing one is evidence of tampering."""
    path = _archive(tmp_path, extra={name: b"x"})

    with pytest.raises(ArchiveError, match="unsafe member name"):
        inspect(path, sha256_file(path))


# --- what the release contains --------------------------------------------


def test_a_ui_only_release_is_recognised(tmp_path):
    path = _archive(tmp_path, bodies={UI: BODIES[UI]})
    contents = inspect(path, sha256_file(path))["contents"]

    assert contents["has_ui"] is True
    assert contents["has_backend"] is False
    assert contents["wheel"] is None


def test_a_backend_only_release_is_recognised(tmp_path):
    path = _archive(tmp_path, bodies={WHEEL: BODIES[WHEEL], REQS: BODIES[REQS]})
    contents = inspect(path, sha256_file(path))["contents"]

    assert contents["has_backend"] is True
    assert contents["has_ui"] is False
    assert contents["requirements"] == REQS


def test_classification_is_by_suffix_not_by_name():
    """The wheel's distribution name has already changed once - aidenops became
    aidenops-service - so matching a name prefix would break on the next rename."""
    older = classify([{"name": "aidenops-1.0.0-py3-none-any.whl", "size": 1}])
    assert older["wheel"] == "aidenops-1.0.0-py3-none-any.whl"
    assert older["has_backend"] is True


# --- provenance ------------------------------------------------------------


def test_the_manifest_is_read_when_present(tmp_path):
    path = _archive(tmp_path)
    manifest = inspect(path, sha256_file(path))["manifest"]

    assert manifest["built_by"] == "vineesh"
    assert {b["commit_short"] for b in manifest["builds"]} == {"d00222c", "635405c"}


def test_an_archive_without_a_manifest_still_deploys(tmp_path):
    """An older archive built before manifests existed is not broken, it just
    cannot say which commit it came from."""
    path = _archive(tmp_path, manifest=False)
    assert inspect(path, sha256_file(path))["manifest"] is None
