"""Tests for interruption, and for the restart that ends it.

**Interruption belongs to the SENDER.** An agent that sends a `[RESEARCH]`,
`[ERROR]` or `[QUERY]` has handed work away and is waiting on the outcome, so it
stops. The recipient is not disturbed at all -- it finds a job in its queue and
drains it in priority order like anything else.

An interrupted agent is exactly three things: an **empty working slot**, the
**`partners.interrupted` flag**, and **no drain thread**. Nothing occupies the
slot in the meantime; there is no placeholder task, because a placeholder is a
row a reader could mistake for work. The flag exists because the empty slot
alone is ambiguous -- a slot is also empty between two ordinary tasks, and in
that state the thread is still running and should promote the next row.

What ends it is a **response** -- a label whose `reply_behavior IS NULL`, which
is `[MESSAGE-RESPONSE]` and `[TRUTHFUL-REPORT]`. A response takes the empty slot,
clears the flag, and the agent runs again. The one route that sends no response
is an approval `[ERROR]`, which replies with nothing: there the Polling Server
clears the flag itself once the Caller has corrected the permissions.
"""

from __future__ import annotations

import pytest

from extension.base import StubExtension
from messaging_core.core import MessagingCore
from messaging_core.db import Database
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


def working(core: MessagingCore, partner_id: int):
    return core.working_task(partner_id=partner_id)


# ---------------------------------------------------------------------------
# 1. Sending a request stops the SENDER, and only the sender.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("behavior", ["[RESEARCH]", "[QUERY]", "[ERROR]"])
def test_sending_a_request_interrupts_the_sender(db, core, stub, behavior):
    """All three requests stop their sender. None of them stops the recipient."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="first", behavior="[RESEARCH]")
    # The caller is interrupted by that first send; clear it so the second send
    # is observed from a clean state rather than a sticky one.
    core.restart(caller["id"])

    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="x", behavior=behavior)

    assert core._is_interrupted(caller["id"]), (
        f"sending a {behavior} must interrupt the SENDER"
    )
    assert working(core, caller["id"]) is None, (
        "an interrupted agent has an EMPTY slot -- no placeholder task"
    )
    assert not core._is_interrupted(worker["id"]), (
        f"a {behavior} must NOT interrupt its recipient; it just queues a job"
    )


def test_the_recipient_just_finds_a_job_in_its_queue(db, core, stub):
    """The whole of what a request does to its target."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate x", behavior="[RESEARCH]")

    held = working(core, worker["id"])
    assert held is not None and held["behavior"] == "[RESEARCH]", (
        f"the recipient should be working the job, got {held!r}"
    )
    assert held["body"] == "investigate x"


def test_interrupting_pushes_the_senders_own_work_back_paused(db, core, stub):
    """What the sender was doing is not lost -- it is requeued `in_process`."""
    caller, worker = make_pair(core)
    # Give the caller something to be working on.
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="here is a report", behavior="[TRUTHFUL-REPORT]")
    assert working(core, caller["id"])["behavior"] == "[TRUTHFUL-REPORT]"

    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="go", behavior="[RESEARCH]")

    assert working(core, caller["id"]) is None
    row = db.read_one(
        "SELECT behavior, in_process, body FROM message_queue "
        "WHERE partner_id = ? AND behavior = '[TRUTHFUL-REPORT]'", (caller["id"],)
    )
    assert row is not None, "the sender's working task must be back in its own queue"
    assert row["in_process"] == 1, f"and marked paused, got {dict(row)}"
    assert row["body"] == "here is a report", "with its body intact"


def test_interrupting_an_idle_agent_is_legal_and_still_marks_it(db, core, stub):
    """There is nothing to push back, but it has still said it is waiting."""
    caller, worker = make_pair(core)
    assert working(core, caller["id"]) is None

    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="go", behavior="[RESEARCH]")

    assert core._is_interrupted(caller["id"])


def test_nothing_is_delivered_to_an_interrupted_agent(db, core, stub):
    """Its remote was just stopped. A paragraph would give it something to do."""
    caller, worker = make_pair(core)
    before = len(deliver_calls(stub))

    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="go", behavior="[RESEARCH]")

    to_caller = [c for c in deliver_calls(stub)[before:]
                 if c["partner_id_in_remote"] == caller["remote_id"]]
    assert to_caller == [], f"the interrupted sender was sent: {to_caller}"


def test_sending_a_response_does_not_interrupt_the_sender(db, core, stub):
    """A response is an answer, not an ask. Answering does not stop you."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="done", behavior="[TRUTHFUL-REPORT]")

    assert not core._is_interrupted(worker["id"]), (
        "sending a response must not interrupt its sender"
    )


# ---------------------------------------------------------------------------
# 2. While interrupted, requests queue and nothing runs.
# ---------------------------------------------------------------------------


def test_a_request_arriving_at_an_interrupted_agent_only_queues(db, core, stub):
    """It is admitted, and it waits. Nothing promotes it until the restart."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="go", behavior="[RESEARCH]")
    assert core._is_interrupted(caller["id"])

    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="which dataset?", behavior="[QUERY]")

    assert working(core, caller["id"]) is None, (
        "an interrupted agent must not be given work by a request"
    )
    assert "[QUERY]" in queued_behaviors(db, caller["id"]), (
        "but the request is admitted and waiting"
    )
    assert core._is_interrupted(caller["id"]), "and it is still interrupted"


# ---------------------------------------------------------------------------
# 3. A response restarts it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("behavior", ["[MESSAGE-RESPONSE]", "[TRUTHFUL-REPORT]"])
def test_a_response_restarts_an_interrupted_agent(db, core, stub, behavior):
    """Either response label takes the empty slot and clears the flag."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="go", behavior="[RESEARCH]")
    assert core._is_interrupted(caller["id"])

    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="the answer", behavior=behavior)

    held = working(core, caller["id"])
    assert held is not None and held["behavior"] == behavior, (
        f"a {behavior} must restart an interrupted agent, got {held!r}"
    )
    assert not core._is_interrupted(caller["id"]), "and clear the flag"


def test_the_response_is_delivered_as_an_ordinary_message(db, core, stub):
    """No special prompt. It is relayed like anything else."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="go", behavior="[RESEARCH]")
    before = len(deliver_calls(stub))

    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="use the 2024 set", behavior="[MESSAGE-RESPONSE]")

    to_caller = [c for c in deliver_calls(stub)[before:]
                 if c["partner_id_in_remote"] == caller["remote_id"]]
    assert len(to_caller) == 1, f"expected one delivery to the restarted agent: {to_caller}"
    assert "use the 2024 set" in to_caller[0]["body"]


def test_after_the_restart_the_paused_work_resumes(db, core, stub):
    """The queue drains normally again -- that is what a restart buys."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="a report", behavior="[TRUTHFUL-REPORT]")
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="go", behavior="[RESEARCH]")
    assert core._is_interrupted(caller["id"])

    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="answer", behavior="[MESSAGE-RESPONSE]")
    assert working(core, caller["id"])["behavior"] == "[MESSAGE-RESPONSE]"

    core.release(partner_id=caller["id"])
    core.advance(partner_id=caller["id"])
    resumed = working(core, caller["id"])
    assert resumed is not None and resumed["behavior"] == "[TRUTHFUL-REPORT]", (
        f"the paused work must resume after the restart, got {resumed!r}"
    )
    assert bool(resumed["in_process"]) is True


def test_an_agents_own_paused_response_does_not_restart_it(db, core, stub):
    """Interrupting must not supply the thing that undoes it.

    Interrupting pushes the agent's working task back into its own queue. If
    that task carried a response label, the queue now holds a response row --
    and a restart rule that accepted any response would fire on the agent's own
    pushed-back work, un-interrupting it immediately.

    Only a response that ARRIVED counts, which is why the lookup is scoped to
    `in_process = 0`.
    """
    caller, worker = make_pair(core)
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="a report", behavior="[TRUTHFUL-REPORT]")
    assert working(core, caller["id"])["behavior"] == "[TRUTHFUL-REPORT]"

    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="go", behavior="[RESEARCH]")

    paused = db.read_one(
        "SELECT behavior, in_process FROM message_queue WHERE partner_id = ?",
        (caller["id"],),
    )
    assert paused["behavior"] == "[TRUTHFUL-REPORT]" and paused["in_process"] == 1, (
        f"setup failed: expected the response paused in its own queue, got {dict(paused)}"
    )

    assert core.advance(partner_id=caller["id"]) is None, (
        "a paused response of the agent's own must not restart it"
    )
    assert core._is_interrupted(caller["id"])
    assert working(core, caller["id"]) is None


def test_restart_is_a_no_op_on_an_agent_that_is_not_interrupted(db, core, stub):
    caller, worker = make_pair(core)
    assert core.restart(caller["id"]) is False


def test_restart_clears_the_flag_without_delivering_anything(db, core, stub):
    """The approval-[ERROR] route: no response is sent, so something else clears it."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="go", behavior="[RESEARCH]")
    before = len(deliver_calls(stub))

    assert core.restart(caller["id"]) is True

    assert not core._is_interrupted(caller["id"])
    assert len(deliver_calls(stub)) == before, "restart delivers nothing by itself"


# ---------------------------------------------------------------------------
# 4. [ERROR] replies with nothing.
# ---------------------------------------------------------------------------


def test_an_error_expects_no_reply(db, core, stub):
    """Changed deliberately: a reply to an [ERROR] carries nothing usable.

    What resumes the work is the drain thread finding the paused row still
    marked `in_process`, not a message coming back.
    """
    assert core.reply_behavior("[ERROR]") is None


def test_a_query_is_still_answered(db, core, stub):
    """[ERROR] losing its reply must not take [QUERY]'s with it."""
    assert core.reply_behavior("[QUERY]") == "[MESSAGE-RESPONSE]"


def test_the_exchange_ends_at_the_answer(db, core, stub):
    """One hop, not an endless correction loop."""
    assert core.reply_behavior("[MESSAGE-RESPONSE]") is None
    assert core.reply_behavior("[TRUTHFUL-REPORT]") is None


# ---------------------------------------------------------------------------
# 5. The drain thread, and the no-work gate.
# ---------------------------------------------------------------------------


def test_interrupting_deletes_the_drain_threads_row(db, core, stub, server):
    """An interrupted agent has no thread, and no row claiming it has one.

    A row is a claim that a thread is running. After an interruption none is,
    and a row left behind would make the restart believe a thread already exists
    and spawn none -- the queue would sit with nobody draining it.
    """
    caller, worker = make_pair(core)
    server.ensure_partner_thread(partner_id=caller["id"])
    assert db.read_one(
        "SELECT 1 AS ok FROM drain_threads WHERE partner_id = ?", (caller["id"],)
    ) is not None, "setup failed: expected a registered thread"

    server.stop_partner_thread(partner_id=caller["id"])

    assert db.read_one(
        "SELECT 1 AS ok FROM drain_threads WHERE partner_id = ?", (caller["id"],)
    ) is None, "the row must be deleted, not left claiming a thread that is gone"


def test_restarting_puts_a_thread_back(db, core, stub, server):
    """The other half: a restart is only real if something drains again."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="go", behavior="[RESEARCH]")
    server.stop_partner_thread(partner_id=caller["id"])
    assert core._is_interrupted(caller["id"])

    server.restart_partner(partner_id=caller["id"])

    assert not core._is_interrupted(caller["id"])
    assert db.read_one(
        "SELECT 1 AS ok FROM drain_threads WHERE partner_id = ?", (caller["id"],)
    ) is not None, "a restarted partner needs a thread to drain what it accumulated"


def test_restart_partner_is_a_no_op_on_a_running_agent(db, core, stub, server):
    caller, worker = make_pair(core)
    out = server.restart_partner(partner_id=caller["id"])
    assert "nothing new" in out.lower() or "not interrupted" in out.lower(), out


def test_no_work_is_false_while_the_agent_still_has_queued_work(db, core, stub):
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="a", behavior="[RESEARCH]")
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="b", behavior="[RESEARCH]")
    assert queued_behaviors(db, worker["id"]), "setup failed: expected a queued job"

    assert core.no_work(worker["id"]) is False, (
        "an agent with something left to drain is not no-work"
    )


def test_no_work_is_false_while_something_it_sent_is_still_queued(db, core, stub):
    """Work dispatched but not picked up is work whose result it has not seen."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="a", behavior="[RESEARCH]")
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="b", behavior="[RESEARCH]")

    assert db.read_one(
        "SELECT 1 AS ok FROM message_queue WHERE caller_id = ?", (caller["id"],)
    ) is not None, "setup failed: expected one of the caller's sends still queued"
    assert core.no_work(caller["id"]) is False


def test_an_interrupted_dependent_blocks_its_orchestrators_summary(db, core, stub):
    """Interrupted means waiting on a response, which means not done."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="which dataset?", behavior="[QUERY]")
    assert core._is_interrupted(worker["id"]), "setup failed"

    assert core.no_work(worker["id"]) is False, (
        "an interrupted agent is waiting on a response and is not finished"
    )


def test_no_work_survives_a_handshake_cycle(db, core, stub):
    """A->B and B->A are both legal rows. The recursion must still terminate."""
    caller, worker = make_pair(core)
    # `make_pair` already opens caller -> worker. Force the reverse row directly:
    # the handshake capability would refuse it, but nothing stops a cycle
    # existing in the table, and `no_work` recurses over exactly this edge.
    db.write(lambda conn: conn.execute(
        "INSERT INTO handshakes (from_partner, to_partner) VALUES (?, ?)",
        (worker["id"], caller["id"]),
    ))
    db.write(lambda conn: conn.execute(
        "UPDATE partners SET orchestrator_type = 'bridge-scientist' WHERE id = ?",
        (worker["id"],),
    ))

    # Both are orchestrators pointing at each other, so a naive recursion would
    # not terminate. It must.
    assert core.no_work(caller["id"]) is True
    assert core.no_work(worker["id"]) is True


# ---------------------------------------------------------------------------
# 6. Batch send.
# ---------------------------------------------------------------------------


def test_a_batch_accepts_items_until_a_cap_and_keeps_going(db, core, stub):
    """A refusal is per item, not per batch.

    The `[RESEARCH]` cap is 2 per (caller, label) against one partner. A third
    to the SAME partner must be refused -- and the item after it, aimed
    somewhere else, must still land. A batch that aborted on first failure would
    make the caller reconstruct which of its messages survived.
    """
    caller, worker = make_pair(core)
    other = core.create_partner(
        project_id=db.read_one("SELECT project_id FROM partners WHERE id = ?",
                               (worker["id"],))["project_id"],
        title="second-worker", partner_id_in_remote="r-second", descr="d",
    )
    core.handshake(requester_uuid=caller["uuid"], partner_title="second-worker")
    core.restart(caller["id"])

    result = core.send_batch(requester_uuid=caller["uuid"], items=[
        {"queried_partner_title": worker["title"], "message": "a", "behavior": "[RESEARCH]"},
        {"queried_partner_title": worker["title"], "message": "b", "behavior": "[RESEARCH]"},
        {"queried_partner_title": worker["title"], "message": "c", "behavior": "[RESEARCH]"},
        {"queried_partner_title": "second-worker", "message": "d", "behavior": "[RESEARCH]"},
    ])

    assert [a["index"] for a in result["accepted"]] == [0, 1, 3], (
        f"the capped item should be the only casualty; accepted {result['accepted']}"
    )
    assert [r["index"] for r in result["refused"]] == [2]
    assert result["refused"][0]["code"] == "over_queue"


def test_a_batch_reports_an_unknown_target_per_item(db, core, stub):
    caller, worker = make_pair(core)
    core.restart(caller["id"])

    result = core.send_batch(requester_uuid=caller["uuid"], items=[
        {"queried_partner_title": worker["title"], "message": "a", "behavior": "[RESEARCH]"},
        {"queried_partner_title": "nobody", "message": "b", "behavior": "[RESEARCH]"},
    ])

    assert [a["index"] for a in result["accepted"]] == [0]
    assert result["refused"][0]["code"] == "no_such_partner"


def test_a_batch_interrupts_the_sender_exactly_once(db, core, stub):
    """Interruption is a property of the sender, not of a message."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="a report", behavior="[TRUTHFUL-REPORT]")
    assert working(core, caller["id"]) is not None, "setup failed"

    result = core.send_batch(requester_uuid=caller["uuid"], items=[
        {"queried_partner_title": worker["title"], "message": "a", "behavior": "[RESEARCH]"},
        {"queried_partner_title": worker["title"], "message": "b", "behavior": "[RESEARCH]"},
    ])

    assert result["interrupted_sender"] == caller["id"]
    assert core._is_interrupted(caller["id"])
    paused = db.read(
        "SELECT behavior FROM message_queue WHERE partner_id = ? AND in_process = 1",
        (caller["id"],),
    )
    assert len(paused) == 1, (
        f"the sender's own task should be pushed back ONCE, not once per item: {paused}"
    )


def test_a_batch_of_only_responses_interrupts_nobody(db, core, stub):
    caller, worker = make_pair(core)

    result = core.send_batch(requester_uuid=worker["uuid"], items=[
        {"queried_partner_title": caller["title"], "message": "x",
         "behavior": "[MESSAGE-RESPONSE]"},
    ])

    assert result["interrupted_sender"] is None
    assert not core._is_interrupted(worker["id"])


def test_a_malformed_item_is_refused_without_stopping_the_batch(db, core, stub):
    caller, worker = make_pair(core)
    core.restart(caller["id"])

    result = core.send_batch(requester_uuid=caller["uuid"], items=[
        {"queried_partner_title": worker["title"]},  # no message, no behavior
        {"queried_partner_title": worker["title"], "message": "b", "behavior": "[RESEARCH]"},
    ])

    assert result["refused"][0]["code"] == "malformed_item"
    assert [a["index"] for a in result["accepted"]] == [1]
