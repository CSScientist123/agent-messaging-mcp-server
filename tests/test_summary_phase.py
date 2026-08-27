"""Tests for a `[RESEARCH]` summary phase that gets interrupted.

A research round trip is two exchanges against one remote inside ONE working
slot: do the work, then report on it. `MessagingCore.begin_summary_phase`
performs the switch by mutating the in-memory slot in place -- it relabels the
task `[TRUTHFUL-REPORT]`, raises its priority, and marks it `summary_phase`.

Both things it changes used to live only in memory, and the queue is where a
displaced task goes. So an interruption during the summary erased the fact that
the task still owed its Caller a report (the result vanished with no error
anywhere), and erased the fact that it was still `[RESEARCH]` work as far as
its Caller's cap was concerned (the cap silently allowed one extra).

Same fixtures and helpers as `test_polling_working_slot.py`.
"""

from __future__ import annotations

import pytest

from extension.base import StubExtension
from messaging_core.core import MessagingCore
from messaging_core.db import Database
from messaging_core.errors import Rejected
from polling.server import PollingServer

from tests.test_polling_working_slot import (
    deliver_calls,
    make_pair,
    queued_behaviors,
    suppress_no_op,
)


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


@pytest.fixture
def server(db, stub, core):
    srv = PollingServer(db, extensions={"science_": stub}, poll_interval=0.01, core=core)
    yield srv
    srv.stop(timeout=5.0)


def into_summary_phase(core: MessagingCore, server: PollingServer, caller: dict, worker: dict) -> None:
    """Drive a [RESEARCH] task to the point where it is writing its summary."""
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="investigate x", behavior="[RESEARCH]",
    )
    server.drain_once(partner_id=worker["id"])
    working = core.working_task(partner_id=worker["id"])
    assert working is not None and working["behavior"] == "[TRUTHFUL-REPORT]", (
        f"setup failed: expected the slot to be in its summary phase, got {working}"
    )


# ---------------------------------------------------------------------------
# 1. The result survives an interruption.
# ---------------------------------------------------------------------------


def test_a_displaced_summary_still_reports_its_result_to_the_caller(
    db, stub, core, server, monkeypatch
):
    """This is the silent one: no error, no log, the Caller simply never hears back.

    Displaced, the task is requeued as an ordinary [TRUTHFUL-REPORT] row. On
    resume the marker is gone, so `_complete` looks up
    `reply_behavior('[TRUTHFUL-REPORT]')`, finds NULL, releases the slot and
    pushes nothing. The research was done and the answer is discarded.
    """
    caller, worker = make_pair(core)
    suppress_no_op(monkeypatch, server)
    into_summary_phase(core, server, caller, worker)

    # Only [IDLE] outranks a summary phase -- that is the point of raising it.
    core.interrupt_partner(
        requester_uuid=caller["uuid"], partner_title=worker["title"], reason="stop for now"
    )
    assert "[TRUTHFUL-REPORT]" in queued_behaviors(db, worker["id"]), (
        "setup failed: the displaced summary should be sitting in the queue"
    )

    # The hold is released and the summary resumes, then finishes.
    server.drain_once(partner_id=worker["id"])
    server.drain_once(partner_id=worker["id"])

    behaviors = queued_behaviors(db, caller["id"])
    assert "[TRUTHFUL-REPORT]" in behaviors, (
        "a summary that was interrupted and resumed still owes its Caller the report; "
        f"the caller's queue holds: {behaviors}"
    )


def test_a_resumed_summary_is_asked_for_again_rather_than_told_to_resume(
    db, stub, core, server, monkeypatch
):
    """"Resume your previous [TRUTHFUL-REPORT]" is the wrong instruction here.

    The one-line resume prompt assumes the agent is still holding the task in
    its own context. A summary phase is a REQUEST -- the questions it asks and
    the shape it wants back are in the prompt itself -- so resuming it means
    asking it again, against the original request the row still carries.
    """
    caller, worker = make_pair(core)
    suppress_no_op(monkeypatch, server)
    into_summary_phase(core, server, caller, worker)

    core.interrupt_partner(
        requester_uuid=caller["uuid"], partner_title=worker["title"], reason="stop for now"
    )
    before = len(deliver_calls(stub))
    server.drain_once(partner_id=worker["id"])

    resumed = deliver_calls(stub)[before:]
    assert resumed, "the summary should have been re-delivered on resume"
    body = resumed[-1]["body"]
    assert "Resume your previous" not in body, (
        f"a resumed summary must be re-asked, not resumed with a one-liner; got: {body!r}"
    )
    assert "investigate x" in body, (
        "the re-asked summary must quote the ORIGINAL request back -- that is what the "
        f"row's body deliberately holds; got: {body!r}"
    )


# ---------------------------------------------------------------------------
# 2. The [RESEARCH] cap keeps counting the work while it is being summarized.
# ---------------------------------------------------------------------------


def test_the_research_cap_counts_a_task_that_is_writing_its_summary(db, stub, core, server, monkeypatch):
    """A summary phase runs at [TRUTHFUL-REPORT]'s priority but is still the same work.

    `[RESEARCH]` is capped at 2 per caller per partner. Once the slot has been
    relabelled, a cap check keyed on the CURRENT label stops counting it -- so
    the caller gets a third [RESEARCH] in flight for as long as the summary
    takes, which is exactly when the partner is least able to take more.
    """
    caller, worker = make_pair(core)
    suppress_no_op(monkeypatch, server)

    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate x", behavior="[RESEARCH]")
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate y", behavior="[RESEARCH]")

    # The first finishes its work and enters the summary phase, relabelling
    # the slot. Its Caller still has two [RESEARCH] outstanding.
    server.drain_once(partner_id=worker["id"])
    working = core.working_task(partner_id=worker["id"])
    assert working is not None and working["behavior"] == "[TRUTHFUL-REPORT]"

    with pytest.raises(Rejected) as exc_info:
        core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
                  message="investigate z", behavior="[RESEARCH]")
    assert exc_info.value.code == "over_queue", (
        f"expected the [RESEARCH] cap to still count the summary phase, got {exc_info.value.code!r}"
    )


def test_the_research_cap_counts_a_displaced_summary_sitting_in_the_queue(
    db, stub, core, server, monkeypatch
):
    """Same rule, read from the queue instead of the slot.

    A displaced summary is a [TRUTHFUL-REPORT] ROW. Counting queued rows by
    their current label would miss it just as counting the slot by its current
    label did, and the cap would leak through the other door.
    """
    caller, worker = make_pair(core)
    suppress_no_op(monkeypatch, server)

    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate x", behavior="[RESEARCH]")
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate y", behavior="[RESEARCH]")
    server.drain_once(partner_id=worker["id"])
    core.interrupt_partner(
        requester_uuid=caller["uuid"], partner_title=worker["title"], reason="stop for now"
    )
    assert "[TRUTHFUL-REPORT]" in queued_behaviors(db, worker["id"]), (
        "setup failed: the displaced summary should be in the queue, not the slot"
    )

    with pytest.raises(Rejected) as exc_info:
        core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
                  message="investigate z", behavior="[RESEARCH]")
    assert exc_info.value.code == "over_queue", (
        "a displaced summary is still its Caller's [RESEARCH]; expected over_queue, got "
        f"{exc_info.value.code!r}"
    )


def test_a_directly_sent_truthful_report_is_not_treated_as_a_summary_phase(
    db, stub, core, server, monkeypatch
):
    """The distinction the marker exists to preserve, checked from the other side.

    A [TRUTHFUL-REPORT] an agent sent directly IS the report; it owes nothing
    back. Inferring the phase from the label instead of the marker made each
    report reply with another report, and the pair never stopped.
    """
    caller, worker = make_pair(core)
    suppress_no_op(monkeypatch, server)

    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="here is my report", behavior="[TRUTHFUL-REPORT]")
    server.drain_once(partner_id=worker["id"])

    assert queued_behaviors(db, caller["id"]) == [], (
        "a directly-sent [TRUTHFUL-REPORT] must not generate a reply; the caller's queue "
        f"holds: {queued_behaviors(db, caller['id'])}"
    )
