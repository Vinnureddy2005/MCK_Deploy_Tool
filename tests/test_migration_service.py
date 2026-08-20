"""Reading the migrations a release will apply, before it applies them.

AidenOps runs auto_migrate: true, so starting the service migrates the schema.
This makes that visible - it judges nothing and blocks nothing, but turns
"nobody knew" into "you were told".
"""

from __future__ import annotations

import zipfile

import pytest

from app.services.migration_service import MigrationError, pending, scan, summarise

BASE = '''"""add partner sla columns

Revision ID: a41f2c
"""
revision = "a41f2c"
down_revision = None

def upgrade():
    op.add_column("partners", sa.Column("sla_hours", sa.Integer()))
'''

DESTRUCTIVE = '''"""drop legacy ticket notes

Revision ID: b8e9d1
"""
revision = "b8e9d1"
down_revision = "a41f2c"

def upgrade():
    op.drop_column("tickets", "legacy_notes")
'''

BACKFILL = '''"""backfill ticket priority

Revision ID: c02a77
"""
revision: str = "c02a77"
down_revision: str = "b8e9d1"

def upgrade():
    op.execute("UPDATE tickets SET priority = 3 WHERE priority IS NULL")
'''

RAW_DROP = '''"""raw sql cleanup

Revision ID: d13b88
"""
revision = "d13b88"
down_revision = "c02a77"

def upgrade():
    op.execute("DROP TABLE stale_audit_rows")
'''


def _wheel(tmp_path, revisions=(BASE, DESTRUCTIVE, BACKFILL), name="w.whl"):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("app/cli.py", "def main(): pass\n")
        zf.writestr("migrations/versions/__init__.py", "")
        for index, source in enumerate(revisions):
            zf.writestr(f"migrations/versions/{index:04d}_rev.py", source)
    return path


# --- reading them out of the wheel ----------------------------------------


def test_every_migration_is_found(tmp_path):
    found = scan(_wheel(tmp_path))
    assert {r["revision"] for r in found} == {"a41f2c", "b8e9d1", "c02a77"}


def test_the_init_file_is_not_a_migration(tmp_path):
    assert all(r["revision"] for r in scan(_wheel(tmp_path)))


def test_annotated_declarations_are_parsed(tmp_path):
    """Newer Alembic templates write `revision: str = "..."`."""
    found = {r["revision"]: r for r in scan(_wheel(tmp_path))}
    assert found["c02a77"]["down_revision"] == "b8e9d1"


def test_the_base_revision_has_no_parent(tmp_path):
    found = {r["revision"]: r for r in scan(_wheel(tmp_path))}
    assert found["a41f2c"]["down_revision"] is None


def test_an_unreadable_wheel_is_an_error(tmp_path):
    broken = tmp_path / "broken.whl"
    broken.write_bytes(b"not a zip")
    with pytest.raises(MigrationError, match="Could not read migrations"):
        scan(broken)


# --- what destroys data ---------------------------------------------------


def test_a_drop_column_is_flagged(tmp_path):
    found = {r["revision"]: r for r in scan(_wheel(tmp_path))}
    assert found["b8e9d1"]["destructive"] == ["drop_column"]


def test_an_additive_migration_is_not_flagged(tmp_path):
    found = {r["revision"]: r for r in scan(_wheel(tmp_path))}
    assert found["a41f2c"]["destructive"] == []


def test_a_data_only_migration_is_not_flagged(tmp_path):
    """An UPDATE changes data but destroys no structure; the dump covers it."""
    found = {r["revision"]: r for r in scan(_wheel(tmp_path))}
    assert found["c02a77"]["destructive"] == []


def test_raw_sql_drops_are_flagged(tmp_path):
    """op.execute bypasses the op.* helpers, so it is scanned separately."""
    found = {r["revision"]: r for r in scan(_wheel(tmp_path, revisions=(RAW_DROP,)))}
    assert found["d13b88"]["destructive"] == ["raw SQL DROP/TRUNCATE"]


# --- what will actually run -----------------------------------------------


def test_an_empty_database_applies_everything(tmp_path):
    order = [r["revision"] for r in pending(scan(_wheel(tmp_path)), None)]
    assert order == ["a41f2c", "b8e9d1", "c02a77"]


def test_only_what_comes_after_the_current_revision_applies(tmp_path):
    order = [r["revision"] for r in pending(scan(_wheel(tmp_path)), "a41f2c")]
    assert order == ["b8e9d1", "c02a77"]


def test_a_database_already_at_head_applies_nothing(tmp_path):
    assert pending(scan(_wheel(tmp_path)), "c02a77") == []


def test_a_database_ahead_of_the_release_is_refused(tmp_path):
    """A revision the wheel does not contain means this deploy is a downgrade -
    worth saying rather than silently applying nothing."""
    with pytest.raises(MigrationError, match="downgrade"):
        pending(scan(_wheel(tmp_path)), "ffffff")


# --- what the operator is told -------------------------------------------


def test_a_release_with_no_migrations_needs_no_dump(tmp_path):
    """Ten config-only deploys in a day should not write 1.4 GB of dumps."""
    result = summarise(pending(scan(_wheel(tmp_path)), "c02a77"))
    assert result["count"] == 0
    assert result["needs_dump"] is False
    assert result["needs_confirmation"] is False


def test_a_release_with_migrations_needs_a_dump(tmp_path):
    result = summarise(pending(scan(_wheel(tmp_path)), "a41f2c"))
    assert result["needs_dump"] is True


def test_a_destructive_release_needs_confirmation(tmp_path):
    result = summarise(pending(scan(_wheel(tmp_path)), "a41f2c"))
    assert result["needs_confirmation"] is True
    assert [r["revision"] for r in result["destructive"]] == ["b8e9d1"]


def test_an_additive_release_needs_no_confirmation(tmp_path):
    """Blocking on every schema change would train people to click through."""
    result = summarise(pending(scan(_wheel(tmp_path)), "b8e9d1"))
    assert result["count"] == 1
    assert result["needs_confirmation"] is False


# --- regressions ----------------------------------------------------------

WITH_DOWNGRADE = '''"""add audit table

Revision ID: e5f6a7
"""
revision = "e5f6a7"
down_revision = None

def upgrade() -> None:
    op.create_table("audit", sa.Column("id", sa.Integer()))
    op.create_index("ix_audit_id", "audit", ["id"])

def downgrade() -> None:
    op.drop_index("ix_audit_id", table_name="audit")
    op.drop_table("audit")
'''


def test_drops_in_downgrade_are_not_flagged(tmp_path):
    """Every downgrade reverses its upgrade, so it contains drops by definition.

    Scanning whole files flagged 67 of the 115 real migrations - an alarm at that
    rate is one nobody reads. Only the upgrade body is scanned.
    """
    found = {r["revision"]: r for r in scan(_wheel(tmp_path, revisions=(WITH_DOWNGRADE,)))}
    assert found["e5f6a7"]["destructive"] == []


def test_a_drop_in_upgrade_is_still_flagged_when_a_downgrade_follows(tmp_path):
    """The complement of the test above: narrowing the scan must not blind it."""
    source = WITH_DOWNGRADE.replace(
        'op.create_table("audit", sa.Column("id", sa.Integer()))',
        'op.drop_column("tickets", "old_col")',
    )
    found = {r["revision"]: r for r in scan(_wheel(tmp_path, revisions=(source,)))}
    assert found["e5f6a7"]["destructive"] == ["drop_column"]


def test_crlf_files_are_parsed(tmp_path):
    """Every migration in the real wheel uses CRLF line endings."""
    crlf = WITH_DOWNGRADE.replace("\n", "\r\n").replace(
        'op.create_table("audit", sa.Column("id", sa.Integer()))',
        'op.drop_table("legacy")',
    )
    found = {r["revision"]: r for r in scan(_wheel(tmp_path, revisions=(crlf,)))}
    assert found["e5f6a7"]["revision"] == "e5f6a7"
    assert found["e5f6a7"]["destructive"] == ["drop_table"]


def test_the_upgrade_pattern_contains_no_control_characters():
    """A guard against how this broke: `\b` written through a non-raw string
    became a literal backspace, so the pattern matched nothing at all - and
    printing it showed the backspace as nothing, hiding the fault.
    """
    from app.services.migration_service import _UPGRADE_BODY

    assert not any(ord(ch) < 32 for ch in _UPGRADE_BODY.pattern), repr(_UPGRADE_BODY.pattern)


def test_a_migration_with_no_upgrade_function_is_not_flagged(tmp_path):
    """An empty body must read as "nothing destructive", not crash."""
    stub = 'revision = "aaaa11"\ndown_revision = None\n'
    found = {r["revision"]: r for r in scan(_wheel(tmp_path, revisions=(stub,)))}
    assert found["aaaa11"]["destructive"] == []
