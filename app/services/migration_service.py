"""Reading the Alembic migrations a release will apply, before it applies them.

AidenOps runs `auto_migrate: true`, so starting the service migrates the schema.
That makes a deployment a schema change whether or not anyone intended one, and
nothing about it is visible until afterwards.

This module makes it visible. It reads the revision files out of the wheel - no
server needed - links them into order, and flags the ones that destroy data.

It deliberately makes no judgement and blocks nothing. Whether a migration is
correct is not a deployment tool's call. But "you were told and chose to
proceed" is a very different position from "nobody knew", and that difference is
the entire point of this step.
"""

from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)


class MigrationError(RuntimeError):
    """The migrations in the wheel could not be read."""


# Alembic operations that remove data or structure. A column dropped here cannot
# be recovered by reinstalling the previous wheel - only by restoring the dump.
_DESTRUCTIVE = (
    "drop_table",
    "drop_column",
    "drop_constraint",
    "drop_index",
)

# Raw SQL bypasses the op.* helpers, so it is scanned separately.
_DESTRUCTIVE_SQL = re.compile(r"\b(DROP|TRUNCATE)\s+(TABLE|COLUMN|SCHEMA|CONSTRAINT)\b", re.I)

_REVISION = re.compile(r"^revision(?::\s*str)?\s*=\s*['\"]([^'\"]+)['\"]", re.M)
_DOWN = re.compile(r"^down_revision(?::[^=]+)?\s*=\s*(?:['\"]([^'\"]+)['\"]|None)", re.M)
_VERSIONS = re.compile(r"(^|/)migrations/versions/[^/]+\.py$")

# Only the upgrade() body is scanned. Every downgrade() reverses its upgrade, so
# it contains drops by definition - scanning whole files flagged 67 of the 115
# real migrations as destructive, which is an alarm nobody would read twice.
_UPGRADE_BODY = re.compile(r"^def upgrade[\s\S]*?(?=^def |\Z)", re.M)


def scan(wheel_path: str | Path) -> list[dict]:
    """Every migration the wheel carries, with its destructive operations."""
    path = Path(wheel_path)
    try:
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if _VERSIONS.search(n) and "__init__" not in n]
            revisions = [_parse(zf.read(name).decode("utf-8", errors="replace"), name)
                         for name in names]
    except (zipfile.BadZipFile, OSError) as exc:
        raise MigrationError(f"Could not read migrations from {path.name}: {exc}") from exc

    found = [r for r in revisions if r["revision"]]
    log.info("%s carries %d migration(s)", path.name, len(found))
    return found


def pending(revisions: list[dict], current: str | None) -> list[dict]:
    """The migrations that will run, in the order Alembic will run them.

    `current` is the server's alembic_version, read separately. None means an
    empty database, where every migration applies.

    A current revision the wheel does not contain means the database is ahead of
    this release - a downgrade - and the caller is told rather than guessed at.
    """
    by_down: dict[str | None, dict] = {}
    for revision in revisions:
        by_down.setdefault(revision["down_revision"], revision)

    known = {r["revision"] for r in revisions}
    if current and current not in known:
        raise MigrationError(
            f"The database is at revision {current}, which this release does not "
            "contain. That means it was migrated by a newer build - deploying "
            "this one would be a downgrade."
        )

    ordered: list[dict] = []
    cursor = current
    seen: set[str] = set()
    while cursor in by_down:
        step = by_down[cursor]
        if step["revision"] in seen:
            # A cycle cannot happen in a valid history, but a malformed wheel
            # should stop the deployment rather than spin.
            raise MigrationError("The migration history in this wheel contains a cycle.")
        seen.add(step["revision"])
        ordered.append(step)
        cursor = step["revision"]
    return ordered


def summarise(pending_revisions: list[dict]) -> dict:
    """What the operator needs to decide with."""
    destructive = [r for r in pending_revisions if r["destructive"]]
    return {
        "count": len(pending_revisions),
        "migrations": pending_revisions,
        "destructive": destructive,
        # A dump is only worth taking when the schema can actually change.
        # Ten config-only deploys in a day should not write 1.4 GB for nothing.
        "needs_dump": bool(pending_revisions),
        "needs_confirmation": bool(destructive),
    }


def _parse(source: str, filename: str) -> dict:
    revision = _REVISION.search(source)
    down = _DOWN.search(source)

    upgrade = _UPGRADE_BODY.search(source)
    body = upgrade.group(0) if upgrade else ""

    destructive = sorted({op for op in _DESTRUCTIVE if f"op.{op}(" in body})
    if _DESTRUCTIVE_SQL.search(body):
        destructive.append("raw SQL DROP/TRUNCATE")

    return {
        "revision": revision.group(1) if revision else None,
        # group(1) is None when the literal matched was `None` - the base revision.
        "down_revision": down.group(1) if down and down.group(1) else None,
        # The slug Alembic puts in the filename is the only human-readable
        # description available without importing the module.
        "slug": Path(filename).stem,
        "destructive": destructive,
    }
