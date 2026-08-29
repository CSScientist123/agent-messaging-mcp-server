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




def displace_the_summary(core: MessagingCore, stub, worker: dict) -> None:
    """Push the summary phase back into the queue, paused.

    Nothing outranks a `[TRUTHFUL-REPORT]` -- that is the point of raising a
    research task to it -- so the only way one leaves the slot before finishing
    is a delivery that fails. `advance` puts a task whose delivery raised back
    into the queue marked `in_process`, which is exactly the state a resumed
    summary has to survive.
    """
    original = stub.deliver_message

    def failing(*, partner_id_in_remote, behavior, body):
        raise RuntimeError("the remote refused the delivery")

    stub.deliver_message = failing
    try:
        core.advance(partner_id=worker["id"])
    except Exception:
        pass
    finally:
        stub.deliver_message = original


# ---------------------------------------------------------------------------
# 1. The result survives an interruption.
# ---------------------------------------------------------------------------


def test_a_summary_whose_delivery_fails_still_reports_its_result_to_the_caller(
    db, stub, core, server, monkeypatch
):
    """The reachable route back into the queue, and the silent failure it guards.

    Nothing DISPLACES a summary phase: it runs at `[TRUTHFUL-REPORT]`'s own
    priority of 1, and `advance` displaces only on a strictly lower number. What
    does put one back in the queue is a delivery that fails after the row was
    already removed -- `advance` calls `_requeue`, and `_requeue` has to carry
    the same two markers the swap does.

    Without `summary_phase` on that row, the task comes back as an ordinary
    `[TRUTHFUL-REPORT]`. On resume `_complete` looks up
    `reply_behavior('[TRUTHFUL-REPORT]')`, finds NULL, releases the slot and
    pushes nothing. The research was done and the answer is discarded, with no
    error and no log anywhere.
    """
    caller, worker = make_pair(core)
    suppress_no_op(monkeypatch, server)
    into_summary_phase(core, server, caller, worker)

    # Put the summary phase back in the queue the only way it can get there.
    task = core.working_task(partner_id=worker["id"])
    assert task is not None and task["summary_phase"], (
        f"setup failed: expected a summary phase in the slot, got {task!r}"
    )
    core.slots.clear(worker["id"])
    core._requeue(task)

    row = db.read_one(
        "SELECT summary_phase, origin_behavior FROM message_queue "
        "WHERE partner_id = ? AND behavior = '[TRUTHFUL-REPORT]'", (worker["id"],)
    )
    assert row is not None and row["summary_phase"] == 1, (
        f"a requeued summary phase must still be marked as one, got {dict(row) if row else None}"
    )
    assert row["origin_behavior"] == "[RESEARCH]", (
        f"and must still count against its Caller's [RESEARCH] cap, got {dict(row)}"
    )

    server.drain_once(partner_id=worker["id"])
    server.drain_once(partner_id=worker["id"])

    behaviors = queued_behaviors(db, caller["id"])
    assert "[TRUTHFUL-REPORT]" in behaviors, (
        "a summary that was requeued and resumed still owes its Caller the report; "
        f"the caller's queue holds: {behaviors}"
    )


def test_a_displaced_summary_still_reports_its_result_to_the_caller(
    db, stub, core, server, monkeypatch
):
    """The same guarantee, from a row built the way the swap would build one.

    Nothing can displace a summary today, so this reaches the resume path by
    writing the row directly rather than staging a displacement that cannot
    happen. It is the resume half of the property; the test above covers the
    half that actually writes the row.
    """
    caller, worker = make_pair(core)
    suppress_no_op(monkeypatch, server)
    into_summary_phase(core, server, caller, worker)

    core.slots.clear(worker["id"])
    core.db.write(lambda c: c.execute(
        "INSERT INTO message_queue(partner_id, caller_id, behavior, body, in_process, "
        "summary_phase, origin_behavior) VALUES (?, ?, '[TRUTHFUL-REPORT]', ?, 1, 1, "
        "'[RESEARCH]')", (worker["id"], caller["id"], "investigate x")))
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

    core.slots.clear(worker["id"])
    core.db.write(lambda c: c.execute(
        "INSERT INTO message_queue(partner_id, caller_id, behavior, body, in_process, "
        "summary_phase, origin_behavior) VALUES (?, ?, '[TRUTHFUL-REPORT]', ?, 1, 1, "
        "'[RESEARCH]')", (worker["id"], caller["id"], "investigate x")))
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

    # ONE [RESEARCH], because a summary is only requested once the partner is
    # no-work -- a second job still queued would defer it, which is the gate's
    # whole purpose and a different rule from the one under test here.
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate x", behavior="[RESEARCH]")

    # It finishes its work and enters the summary phase, relabelling the slot.
    # Its Caller still has one [RESEARCH] outstanding.
    server.drain_once(partner_id=worker["id"])
    working = core.working_task(partner_id=worker["id"])
    assert working is not None and working["behavior"] == "[TRUTHFUL-REPORT]"

    # Cap is 2, and the summary phase is one of them. So a second is admitted
    # and a third must not be.
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate y", behavior="[RESEARCH]")
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
    core.slots.clear(worker["id"])
    core.db.write(lambda c: c.execute(
        "INSERT INTO message_queue(partner_id, caller_id, behavior, body, in_process, "
        "summary_phase, origin_behavior) VALUES (?, ?, '[TRUTHFUL-REPORT]', ?, 1, 1, "
        "'[RESEARCH]')", (worker["id"], caller["id"], "investigate x")))
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


def test_a_summary_is_deferred_while_the_agent_still_has_queued_work(
    db, stub, core, server, monkeypatch
):
    """NO-WORK is the condition to summarize, and this is it blocking.

    `[TRUTHFUL-REPORT]` outranks `[MESSAGE-RESPONSE]`. Without this gate an
    agent would be asked to summarize while answers were still arriving from
    underneath it, and the summary would outrank them -- describing a situation
    that had already changed.

    Not finished means not summarized: the task stays in the slot and the next
    pass asks again. It is a wait, not a refusal.
    """
    caller, worker = make_pair(core)
    suppress_no_op(monkeypatch, server)

    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate x", behavior="[RESEARCH]")
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate y", behavior="[RESEARCH]")
    assert queued_behaviors(db, worker["id"]) == ["[RESEARCH]"], (
        "setup failed: the second job should be queued behind the first"
    )

    server.drain_once(partner_id=worker["id"])

    held = core.working_task(partner_id=worker["id"])
    assert held is not None and held["behavior"] == "[RESEARCH]", (
        "the summary must be deferred while a second job is still queued; "
        f"the slot holds {held['behavior'] if held else None}"
    )
    assert not held.get("summary_phase"), "and the task must not be relabelled yet"


def test_the_summary_happens_once_the_queue_is_finally_empty(
    db, stub, core, server, monkeypatch
):
    """The other half: the gate is a wait, and the wait ends."""
    caller, worker = make_pair(core)
    suppress_no_op(monkeypatch, server)

    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate x", behavior="[RESEARCH]")
    assert not queued_behaviors(db, worker["id"]), "setup failed: nothing should be queued"

    server.drain_once(partner_id=worker["id"])

    held = core.working_task(partner_id=worker["id"])
    assert held is not None and held["behavior"] == "[TRUTHFUL-REPORT]", (
        f"with nothing left to drain the summary should be requested, got {held!r}"
    )
    assert held["summary_phase"] is True
