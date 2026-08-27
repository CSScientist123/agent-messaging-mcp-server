"""Tests for what happens to work in flight when a Partner disappears.

Two ways a Partner goes away, and both used to lose work without saying so.

`archive_sessions` sets `archived_at` with no regard for what is queued. The
next `advance()` sees an archived Partner and DELETES its whole queue -- which
is correct in itself, since an archived Partner can never be messaged again --
so every Caller waiting on one of those messages simply never hears back.

`delete_partner` removes the row, and `message_queue.caller_id` cascades. So
deleting a CALLER takes its queued rows with it, while the in-memory working
slot still names it -- and the `report_back` that follows hits a foreign-key
error after the slot has already been released.

An `[ERROR]` at priority 2 displaces whatever the Caller is doing, so telling
it IS the interruption; no separate mechanism is needed.
"""

from __future__ import annotations

import pytest

from extension.base import StubExtension
from messaging_core.core import MessagingCore
from messaging_core.db import Database
from messaging_core.errors import Rejected

from tests.test_polling_working_slot import make_pair, queued_behaviors


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


def bodies_for(db: Database, partner_id: int) -> list[str]:
    return [
        r["body"] for r in db.read(
            "SELECT body FROM message_queue WHERE partner_id = ? ORDER BY id", (partner_id,)
        )
    ]



# ---------------------------------------------------------------------------
# 1. Archiving
# ---------------------------------------------------------------------------


def test_archiving_a_partner_tells_the_caller_waiting_on_it(db, core):
    """Otherwise the Caller waits forever for work that has already been dropped.

    The Caller told is normally the same partner that called `archive_sessions`,
    and that is the point rather than a redundancy: within a project only the
    project-orchestrator may handshake a plain worker, so it is the only Caller
    there is -- and an orchestrator archiving a list of titles in bulk is
    exactly who does not realise one of them had work in flight.
    """
    caller, worker = make_pair(core)
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="investigate x", behavior="[RESEARCH]",
    )

    core.archive_sessions(requester_uuid=caller["uuid"], titles=[worker["title"]])

    assert "[ERROR]" in queued_behaviors(db, caller["id"]), (
        "the caller must be told its partner was archived and the work is gone; its "
        f"queue holds: {queued_behaviors(db, caller['id'])}"
    )
    body = " ".join(bodies_for(db, caller["id"]))
    assert worker["title"] in body, (
        f"the error must name which partner vanished; got: {body!r}"
    )


def test_archiving_names_what_happened_rather_than_implying_a_delay(db, core):
    caller, worker = make_pair(core)
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="investigate x", behavior="[RESEARCH]",
    )

    core.archive_sessions(requester_uuid=caller["uuid"], titles=[worker["title"]])

    body = " ".join(bodies_for(db, caller["id"])).lower()
    assert "archiv" in body, (
        f"the caller must be told the partner was archived, not merely that work failed: {body!r}"
    )


def test_archiving_a_partner_with_nothing_in_flight_reports_nothing(db, core):
    """A Caller with no stake must not be handed an [ERROR] about it."""
    caller, worker = make_pair(core)

    core.archive_sessions(requester_uuid=caller["uuid"], titles=[worker["title"]])

    assert queued_behaviors(db, caller["id"]) == [], (
        f"nothing was in flight, so nothing is owed; got {queued_behaviors(db, caller['id'])}"
    )


def test_archiving_clears_the_vanishing_partners_working_slot(db, core):
    caller, worker = make_pair(core)
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="investigate x", behavior="[RESEARCH]",
    )
    assert core.working_task(partner_id=worker["id"]) is not None

    core.archive_sessions(requester_uuid=caller["uuid"], titles=[worker["title"]])

    assert core.working_task(partner_id=worker["id"]) is None, (
        "an archived partner must not be left holding a working slot -- nothing will "
        "ever poll it, and the slot blocks the id from being reasoned about cleanly"
    )


# ---------------------------------------------------------------------------
# 2. Deletion
# ---------------------------------------------------------------------------


def test_deleting_a_partner_with_work_in_flight_is_refused(db, core):
    """Deletion is irreversible, and here it cannot even report itself.

    `message_queue.caller_id` is `ON DELETE CASCADE`, so a notice attributed to
    the vanishing Partner is destroyed by the very DELETE it warns about; and
    attributing it to the requester collides with `CHECK (caller_id <>
    partner_id)` in the normal case where the requester IS the Caller waiting.
    Refusing is the honest answer rather than a workaround -- and it leaves the
    Caller a route that does report, which is what `next_call` names.
    """
    caller, worker = make_pair(core)
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="investigate x", behavior="[RESEARCH]",
    )

    with pytest.raises(Rejected) as exc_info:
        core.delete_partner(requester_uuid=caller["uuid"], partner_title=worker["title"])

    assert exc_info.value.code == "partner_has_work_in_flight", (
        f"expected a refusal naming the work in flight, got {exc_info.value.code!r}"
    )
    assert "archive" in str(exc_info.value).lower(), (
        f"the refusal must name the route that does report: {exc_info.value}"
    )
    assert db.read_one("SELECT 1 AS x FROM partners WHERE id = ?", (worker["id"],)) is not None, (
        "a refused deletion must leave the partner in place"
    )


def test_deleting_a_partner_with_nothing_in_flight_still_works(db, core):
    """The refusal is scoped to work in flight, not a new blanket ban."""
    caller, worker = make_pair(core)

    core.delete_partner(requester_uuid=caller["uuid"], partner_title=worker["title"])

    assert db.read_one("SELECT 1 AS x FROM partners WHERE id = ?", (worker["id"],)) is None


def test_a_report_to_a_deleted_caller_does_not_raise(db, core):
    """The Polling Server calls `report_back` AFTER releasing the slot.

    A foreign-key error there is unrecoverable: the slot is already gone, the
    drain thread records an exception nobody reads, and the work is lost twice
    over. Reporting to a Caller that no longer exists must be a quiet
    non-delivery, not a crash.
    """
    caller, worker = make_pair(core)
    deleted_id = caller["id"]
    core.delete_partner(requester_uuid=caller["uuid"], partner_title=caller["title"])

    result = core.report_back(
        to_partner_id=deleted_id, from_partner_id=worker["id"],
        behavior="[TRUTHFUL-REPORT]", body="the answer",
    )

    assert result is not None
    assert not result.get("delivered", False), (
        f"nothing can be delivered to a partner that is gone; got {result!r}"
    )


def test_a_report_to_an_archived_caller_does_not_raise(db, core):
    caller, worker = make_pair(core)
    core.archive_sessions(requester_uuid=caller["uuid"], titles=[caller["title"]])

    result = core.report_back(
        to_partner_id=caller["id"], from_partner_id=worker["id"],
        behavior="[TRUTHFUL-REPORT]", body="the answer",
    )

    assert result is not None
    assert not result.get("delivered", False)


def test_deleting_a_project_with_work_in_flight_is_refused(db, core):
    """A Project deletion cannot report itself either, and cascades wider.

    Every queue row under the project goes, and `message_queue.caller_id` is
    `ON DELETE CASCADE` too — so a notice written to warn a waiting Caller is
    destroyed by the very DELETE it warns about, exactly as in
    `delete_partner`. There is no shape of message that survives, so it
    refuses and names the route that does report.
    """
    caller, worker = make_pair(core)
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="investigate x", behavior="[RESEARCH]",
    )
    project_title = db.read_one(
        "SELECT pr.title AS title FROM projects pr JOIN partners p ON p.project_id = pr.id "
        "WHERE p.id = ?", (worker["id"],)
    )["title"]

    with pytest.raises(Rejected) as exc_info:
        core.delete_project(requester_uuid=caller["uuid"], project_title=project_title)

    assert exc_info.value.code == "partner_has_work_in_flight", (
        f"expected a refusal naming the work in flight, got {exc_info.value.code!r}"
    )
    assert db.read_one("SELECT 1 AS x FROM partners WHERE id = ?", (worker["id"],)) is not None, (
        "a refused deletion must leave the project and its partners in place"
    )


def test_deleting_a_project_with_nothing_in_flight_still_works(db, core):
    """The refusal is scoped to work in flight, not a new blanket ban."""
    caller, worker = make_pair(core)
    project_title = db.read_one(
        "SELECT pr.title AS title FROM projects pr JOIN partners p ON p.project_id = pr.id "
        "WHERE p.id = ?", (worker["id"],)
    )["title"]

    result = core.delete_project(requester_uuid=caller["uuid"], project_title=project_title)

    assert result["partners_deleted"] >= 2
    assert db.read_one("SELECT 1 AS x FROM partners WHERE id = ?", (worker["id"],)) is None
