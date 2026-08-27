"""Tests for opening a database that predates a schema change.

`_apply_schema_if_needed` only runs `schema.sql` against a database with no
tables at all, so a file created before a column was added never gets it. The
code that reads and writes that column then fails against a real deployment
with `no such column` -- a silent upgrade landmine, since every test and every
fresh install passes.

`_ADDITIVE_COLUMNS` plus `_reconcile_added_columns` is the whole migration
story this project has, deliberately: every schema change so far has been
additive, and handling exactly that -- while saying plainly in the docstring
that it handles nothing else -- is more honest than a migration framework
nobody has needed yet.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from messaging_core.config import schema_path
from messaging_core.db import _ADDITIVE_COLUMNS, Database


def columns_of(path, table: str) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


@pytest.fixture
def stale_db(tmp_path):
    """A database file created from the schema as it stood BEFORE the columns.

    Built with raw `sqlite3` rather than through `Database`, and that detail is
    the whole point: `Database` reconciles on every open, so opening the stale
    file to create it would upgrade it in the same breath and the test would
    prove nothing.

    The old schema is derived from the shipped file rather than hand-written,
    so a column added to `_ADDITIVE_COLUMNS` later is covered here without
    anyone remembering to update a fixture.
    """
    text = schema_path().read_text()
    for _table, ddl in _ADDITIVE_COLUMNS:
        name = ddl.split()[0]
        text, n = re.subn(rf"^[ \t]*{re.escape(name)}[ \t]+.*,[ \t]*$", "", text, flags=re.M)
        assert n == 1, f"expected to strip exactly one {name!r} column line, stripped {n}"

    path = tmp_path / "stale.sqlite3"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(text)
        conn.commit()
    finally:
        conn.close()
    for table, ddl in _ADDITIVE_COLUMNS:
        assert ddl.split()[0] not in columns_of(path, table), (
            f"fixture check: {table}.{ddl.split()[0]} should be absent before the upgrade"
        )
    return path


def test_the_declared_column_list_names_the_columns_it_should():
    """A list derived from itself proves nothing.

    Every other test in this file iterates `_ADDITIVE_COLUMNS` -- the fixture
    strips exactly those columns out, and the assertions loop over exactly
    those columns. Empty the constant and all of them pass while reconciling
    nothing: the fixture strips nothing, the loops run zero times, and a
    database that predates the change is silently left broken.

    Found by the mutation pass, which is the only thing that would have.
    """
    declared = {(table, ddl.split()[0]) for table, ddl in _ADDITIVE_COLUMNS}

    assert ("message_queue", "summary_phase") in declared, (
        "summary_phase is read and written by the summary-phase code; a database "
        "created before it existed must gain it on open"
    )
    assert ("message_queue", "origin_behavior") in declared, (
        "origin_behavior is read by the [RESEARCH] cap on both the queue and the "
        "slot side; a database without it fails every admission"
    )


def test_a_database_that_predates_a_column_gains_it_on_open(stale_db):
    Database(path=stale_db).close()

    for table, ddl in _ADDITIVE_COLUMNS:
        assert ddl.split()[0] in columns_of(stale_db, table), (
            f"{table}.{ddl.split()[0]} was never added; code that reads it will fail "
            "against any database created before it existed"
        )


def test_reopening_an_already_reconciled_database_is_a_no_op(stale_db):
    """Otherwise the second open would try to add a column that already exists."""
    Database(path=stale_db).close()

    again = Database(path=stale_db)
    try:
        assert again.read_one("SELECT COUNT(*) AS n FROM message_queue", ())["n"] == 0
    finally:
        again.close()


def test_the_added_columns_are_usable_after_the_upgrade(stale_db):
    """Present in `PRAGMA table_info` is not the same as writable.

    `ALTER TABLE ... ADD COLUMN` accepts a narrower grammar than `CREATE
    TABLE`, so a constraint that survives the fresh path can be silently
    dropped or rejected on the upgrade path.
    """
    db = Database(path=stale_db)
    try:
        db.write(lambda conn: conn.execute(
            "INSERT INTO projects(source_prefix, project_system_id, title) "
            "VALUES ('science_', 'psid', 'proj')"
        ))
        for title in ("a", "b"):
            db.write(lambda conn, t=title: conn.execute(
                "INSERT INTO partners(uuid, project_id, title, partner_id_in_remote, descr) "
                "VALUES (?, 1, ?, ?, 'd')", (f"u-{t}", t, f"r-{t}")
            ))
        db.write(lambda conn: conn.execute(
            "INSERT INTO message_queue"
            "(partner_id, caller_id, behavior, body, summary_phase, origin_behavior) "
            "VALUES (1, 2, '[TRUTHFUL-REPORT]', 'x', 1, '[RESEARCH]')"
        ))
        row = db.read_one(
            "SELECT summary_phase, origin_behavior FROM message_queue WHERE partner_id = 1", ()
        )
        assert row["summary_phase"] == 1
        assert row["origin_behavior"] == "[RESEARCH]"
    finally:
        db.close()


def test_the_check_constraint_survives_the_upgrade_path(stale_db):
    """A constraint that only holds on a fresh database is not a constraint."""
    db = Database(path=stale_db)
    try:
        db.write(lambda conn: conn.execute(
            "INSERT INTO projects(source_prefix, project_system_id, title) "
            "VALUES ('science_', 'psid', 'proj')"
        ))
        for title in ("a", "b"):
            db.write(lambda conn, t=title: conn.execute(
                "INSERT INTO partners(uuid, project_id, title, partner_id_in_remote, descr) "
                "VALUES (?, 1, ?, ?, 'd')", (f"u-{t}", t, f"r-{t}")
            ))
        with pytest.raises(sqlite3.IntegrityError):
            db.write(lambda conn: conn.execute(
                "INSERT INTO message_queue"
                "(partner_id, caller_id, behavior, body, summary_phase) "
                "VALUES (1, 2, '[QUERY]', 'x', 7)"
            ))
    finally:
        db.close()
