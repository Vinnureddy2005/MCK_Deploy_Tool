"""Verifying an AidenOps release archive before anything reaches the server.

A release arrives as one zip carrying the wheel, its paired requirements file,
the UI tarball, a SHA256SUMS.txt and a MANIFEST.json. Two independent checks run
here, both entirely local:

  1. the archive's own hash against the value carried across by hand, which
     proves this is the release that was published;
  2. every member against the SHA256SUMS.txt inside it, which proves the
     contents survived the trip.

The first check has no override. Nothing downstream can catch a wrong artifact:
a wrong wheel installs cleanly, starts cleanly, and /health returns 200 while
running the wrong code. That is not a hypothetical - it is the 17 Aug outage.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)


class ArchiveError(RuntimeError):
    """The archive is not the one we were told to deploy, or is not intact."""


SUMS_FILENAME = "SHA256SUMS.txt"
MANIFEST_FILENAME = "MANIFEST.json"

# What each member is, decided by suffix rather than by position: the wheel's
# distribution name has already changed once (aidenops -> aidenops_service), so
# matching on a name prefix would break on the next rename.
_KINDS = (
    ("wheel", ".whl"),
    ("requirements", ".txt"),
    ("ui", ".tar.gz"),
)

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SUMS_LINE = re.compile(r"^([0-9a-f]{64})\s+\*?(.+)$")

# A member name that will later be handed to `unzip` on the server. unzip
# refuses to write outside its target, but an archive containing such a path is
# evidence of tampering rather than something to work around - so it is refused
# here, before anything is uploaded.
_SAFE_MEMBER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")

_CHUNK = 1024 * 1024


def sha256_file(path: str | Path) -> str:
    """Streamed, so a 140 MB archive is not read into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalise_checksum(value: str) -> str:
    """Accept a hash as pasted - spaced in blocks, mixed case, padded."""
    cleaned = re.sub(r"\s+", "", value or "").lower()
    if not _SHA256.match(cleaned):
        raise ArchiveError(
            "That does not look like a SHA-256 value. Expected 64 hexadecimal "
            "characters, as shown by the Aiden tool."
        )
    return cleaned


def inspect(path: str | Path, expected_checksum: str) -> dict:
    """Verify an archive end to end. Raises ArchiveError on any failure.

    Nothing is extracted. Members are read from the zip in memory, so a release
    that fails verification never touches the filesystem beyond the download
    itself.
    """
    archive = Path(path)
    if not archive.is_file():
        raise ArchiveError(f"{archive} is not a file.")

    expected = normalise_checksum(expected_checksum)
    actual = sha256_file(archive)
    if actual != expected:
        # Deliberately not a warning. There is no downstream check that would
        # catch a wrong artifact, so continuing has no upside.
        raise ArchiveError(
            "The archive does not match the checksum you pasted.\n"
            f"  pasted:     {expected}\n"
            f"  downloaded: {actual}\n"
            "Nothing has been sent to the server. Re-copy the value from the "
            "Aiden tool, or re-download the archive."
        )
    log.info("Archive %s matches the pasted checksum (%s)", archive.name, actual[:12])

    with zipfile.ZipFile(archive) as zf:
        members = _members(zf)
        _refuse_unsafe_names(members)
        sums = _sums(zf)
        files = _verify_members(zf, members, sums)
        manifest = _manifest(zf)

    failed = [f["name"] for f in files if f["verified"] is False]
    if failed:
        raise ArchiveError(
            "The archive's own SHA256SUMS.txt does not match its contents: "
            + ", ".join(failed)
            + ". The archive is damaged - download it again."
        )

    return {
        "archive": archive.name,
        "sha256": actual,
        "size": archive.stat().st_size,
        "verified_members": [f for f in files if f["verified"]],
        "unverified_members": [f for f in files if f["verified"] is None],
        "manifest": manifest,
        "contents": classify(members),
    }


def classify(members: list[dict]) -> dict:
    """Which of the deployable parts this release carries.

    A release can be backend-only or UI-only - both are normal - so each part is
    reported independently rather than assuming a full pair.
    """
    found: dict[str, str | None] = {kind: None for kind, _ in _KINDS}
    for member in members:
        for kind, suffix in _KINDS:
            if member["name"].endswith(suffix) and found[kind] is None:
                found[kind] = member["name"]
    return {
        **found,
        "has_backend": found["wheel"] is not None,
        "has_ui": found["ui"] is not None,
    }


def _members(zf: zipfile.ZipFile) -> list[dict]:
    return [
        {"name": info.filename, "size": info.file_size}
        for info in zf.infolist()
        if not info.is_dir()
    ]


def _refuse_unsafe_names(members: list[dict]) -> None:
    """Refuse absolute paths, traversal, and anything a shell could misread."""
    for member in members:
        name = member["name"]
        if name.startswith("/") or ".." in name or not _SAFE_MEMBER.match(name):
            raise ArchiveError(
                f"The archive contains an unsafe member name: {name!r}. "
                "A release archive is flat - this one has been tampered with."
            )


def _sums(zf: zipfile.ZipFile) -> dict[str, str]:
    try:
        text = zf.read(SUMS_FILENAME).decode("utf-8", errors="replace")
    except KeyError as exc:
        raise ArchiveError(
            f"{SUMS_FILENAME} is missing from the archive, so its contents "
            "cannot be verified. Refusing to deploy an unverifiable release."
        ) from exc

    entries: dict[str, str] = {}
    for line in text.splitlines():
        match = _SUMS_LINE.match(line.strip())
        if match:
            entries[match.group(2).strip()] = match.group(1).lower()
    if not entries:
        raise ArchiveError(f"{SUMS_FILENAME} is present but contains no usable entries.")
    return entries


def _verify_members(zf: zipfile.ZipFile, members: list[dict], sums: dict[str, str]) -> list[dict]:
    """Hash every member the sums file declares.

    A member the sums file does not mention is reported as unverified rather than
    failed - SHA256SUMS.txt cannot list itself, and the manifest is metadata.
    """
    results = []
    for member in members:
        name = member["name"]
        expected = sums.get(name)
        if expected is None:
            results.append({**member, "verified": None, "sha256": None})
            continue

        digest = hashlib.sha256()
        with zf.open(name) as stream:
            for chunk in iter(lambda: stream.read(_CHUNK), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        results.append({**member, "verified": actual == expected, "sha256": actual})

    declared_but_absent = set(sums) - {m["name"] for m in members}
    for name in sorted(declared_but_absent):
        results.append(
            {"name": name, "size": None, "verified": False, "sha256": None,
             "message": "listed in SHA256SUMS.txt but not in the archive"}
        )
    return results


def _manifest(zf: zipfile.ZipFile) -> dict | None:
    """Provenance, if the archive carries it.

    Optional on purpose: an older archive built before manifests existed is
    still deployable, it just cannot say which commit it came from.
    """
    try:
        return json.loads(zf.read(MANIFEST_FILENAME).decode("utf-8"))
    except KeyError:
        log.info("%s has no %s; provenance unknown", zf.filename, MANIFEST_FILENAME)
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        log.warning("%s is unreadable: %s", MANIFEST_FILENAME, exc)
        return None
