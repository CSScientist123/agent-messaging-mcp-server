"""Tests for which LABEL runs next when two share a priority.

`[QUERY]` and `[ERROR]` deliberately share priority 2, and the pop order is
two statements rather than one because `in_process` breaks ties only WITHIN a
label. `test_boundaries.py` already covers the original failure -- a wholly
paused `[QUERY]` beating a fresh `[ERROR]`.

This file covers the case that survived that fix: a label holding a paused row
AND a fresh one. `MIN(in_process)` is then 0, the label ties with `[ERROR]` on
that key, and the next key -- the earliest arrival -- picks the paused row's
label precisely because a paused row is the oldest thing in the queue. The
Partner is handed "resume your previous [QUERY]" instead of the correction, and
the correction is what unblocks it.

Rows are inserted directly here rather than driven through `send`. The order
under test is a property of the SQL, and reaching this arrangement through the
public API requires a sequence of swaps that would each change it again --
which would test the sequence, not the rule.
"""

from __future__ import annotations

import pytest

from extension.base import StubExtension
from messaging_core.core import MessagingCore
from messaging_core.db import Database

from tests.test_polling_working_slot import make_pair


@pytest.fixture
def db():
    database = Database(path=":memory:")
    yield database
    database.close()


@pytest.fixture
def stub():
    return StubExtension(source_prefix="science_")


@pytest.fixture
def core(db, stub):
    return MessagingCore(db, extension=stub)


def put(db: Database, *, partner_id: int, caller_id: int, behavior: str,
        body: str, in_process: int, at: str) -> None:
    """Insert one queue row with an explicit arrival time."""
    db.write(
        lambda conn: conn.execute(
            "INSERT INTO message_queue "
            "(partner_id, caller_id, behavior, body, in_process, enqueued_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (partner_id, caller_id, behavior, body, in_process, at),
        )
    )


def tie_error_to_query(db: Database) -> None:
    """Put `[ERROR]` back on `[QUERY]`'s rank.

    No two seeded labels share a priority any more -- `[ERROR]` was moved above
    `[QUERY]` deliberately, so a permission fix lands before more querying. That
    makes the within-label scoping of `in_process` unreachable through the
    shipped data, and an untested rule is one a later edit silently breaks.

    So these tests force the tie. The schema says exactly why the rule is
    written to survive it: the scoping "makes EVERY pair safe, including two
    labels a later deployment gives the same rank".
    """
    db.write(lambda conn: conn.execute(
        "UPDATE label_caps SET priority = "
        "(SELECT priority FROM label_caps WHERE behavior = '[QUERY]') "
        "WHERE behavior = '[ERROR]'"
    ))


def test_a_fresh_error_still_wins_when_the_query_label_also_holds_a_fresh_row(db, core, stub):
    """The tie-break that reintroduced the shipped bug.

    Arrangement: a paused `[QUERY]` (the oldest row, because it has been
    waiting since before it was interrupted), a fresh `[ERROR]` explaining
    what went wrong, and a second fresh `[QUERY]`. `[QUERY]` now has a fresh
    row too, so it no longer loses on `MIN(in_process)` -- and if the next key
    is the earliest arrival over ALL its rows, its paused row's age wins the
    label and the `[ERROR]` is never seen.
    """
    tie_error_to_query(db)
    caller, worker = make_pair(core)
    put(db, partner_id=worker["id"], caller_id=caller["id"], behavior="[QUERY]",
        body="the original question", in_process=1, at="2026-01-01T00:00:00.000Z")
    put(db, partner_id=worker["id"], caller_id=caller["id"], behavior="[ERROR]",
        body="that path does not exist", in_process=0, at="2026-01-01T00:00:01.000Z")
    put(db, partner_id=worker["id"], caller_id=caller["id"], behavior="[QUERY]",
        body="a second question", in_process=0, at="2026-01-01T00:00:02.000Z")

    core.advance(partner_id=worker["id"])

    working = core.working_task(partner_id=worker["id"])
    assert working is not None and working["behavior"] == "[ERROR]", (
        "a paused [QUERY] won the label for a fresh [ERROR] of equal priority, because "
        f"the label also held a fresh row; working={working!r}"
    )
    assert "does not exist" in working["prompt"], (
        f"the [ERROR]'s own content must reach the agent; got: {working['prompt']!r}"
    )
    assert "resume" not in working["prompt"].lower(), (
        f"the resume template was delivered instead of the correction: {working['prompt']!r}"
    )


def test_a_label_that_is_entirely_paused_still_loses_to_one_with_fresh_work(db, core, stub):
    """The original rule, unchanged: a label only waiting to resume ranks last."""
    tie_error_to_query(db)
    caller, worker = make_pair(core)
    put(db, partner_id=worker["id"], caller_id=caller["id"], behavior="[QUERY]",
        body="the original question", in_process=1, at="2026-01-01T00:00:00.000Z")
    put(db, partner_id=worker["id"], caller_id=caller["id"], behavior="[ERROR]",
        body="that path does not exist", in_process=0, at="2026-01-01T00:00:01.000Z")

    core.advance(partner_id=worker["id"])

    working = core.working_task(partner_id=worker["id"])
    assert working is not None and working["behavior"] == "[ERROR]"


def test_within_one_label_the_paused_row_still_goes_first(db, core, stub):
    """Unchanged, and the reason the second statement exists.

    A Partner finishes what it started before starting anything else of the
    same kind -- and at most one row per label is ever paused, which is what
    lets "resume your previous [QUERY]" have exactly one referent.
    """
    caller, worker = make_pair(core)
    put(db, partner_id=worker["id"], caller_id=caller["id"], behavior="[QUERY]",
        body="the original question", in_process=1, at="2026-01-01T00:00:00.000Z")
    put(db, partner_id=worker["id"], caller_id=caller["id"], behavior="[QUERY]",
        body="a second question", in_process=0, at="2026-01-01T00:00:02.000Z")

    core.advance(partner_id=worker["id"])

    working = core.working_task(partner_id=worker["id"])
    assert working is not None and bool(working["in_process"]) is True, (
        f"the paused row of a label must be promoted before its fresh ones; got {working!r}"
    )


def test_a_higher_priority_label_still_beats_both(db, core, stub):
    """Priority is decided before any of this, and nothing here may reorder it."""
    caller, worker = make_pair(core)
    put(db, partner_id=worker["id"], caller_id=caller["id"], behavior="[QUERY]",
        body="a question", in_process=0, at="2026-01-01T00:00:00.000Z")
    put(db, partner_id=worker["id"], caller_id=caller["id"], behavior="[TRUTHFUL-REPORT]",
        body="the report", in_process=0, at="2026-01-01T00:00:05.000Z")

    core.advance(partner_id=worker["id"])

    working = core.working_task(partner_id=worker["id"])
    assert working is not None and working["behavior"] == "[TRUTHFUL-REPORT]", (
        f"priority 1 must beat priority 2 regardless of arrival; got {working!r}"
    )

