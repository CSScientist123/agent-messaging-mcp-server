"""Tests for the two ways an agent stops, and the one way it starts again.

There are exactly two kinds of interruption, and they are ordered rather than
racing.

**By priority.** A higher-priority arrival displaces the working task. Ordinary
queue mechanics, covered in `test_queue_order.py` and `test_boundaries.py`.

**By force.** Any agent that sends a REQUEST -- `[ERROR]`, `[QUERY]` or
`[RESEARCH]`, the three labels `label_caps.is_request` marks -- is interrupted by
the act of sending it. Its working task is pushed back paused and **its slot is
left EMPTY**. Nothing may fill that slot, at any priority, until the request is
answered.

That is what makes the two ordered: while a force is open there is no working
task to displace and none may start, so priority interruption does not occur.

A response is **not a message**. It is not queued, it carries no label, it never
competes for the slot, and it cannot be outranked by the very work it unblocks.
It arrives through `resolve_wait`, which lifts the force, records the answer
against its request, and lets the next `advance` fill the slot -- folding the
answer into whatever comes next.

And because a forced slot stays empty, one message would otherwise be stranded:
a Partner hitting a mid-task problem needs to reach a Caller that is itself
waiting. That is what the shortcut channel is for.
"""

from __future__ import annotations

import pytest

from extension.base import StubExtension
from messaging_core.core import MessagingCore
from messaging_core.db import Database
from messaging_core.errors import Rejected
from polling.server import PollingServer

from tests.test_polling_working_slot import (
    _unique,
    deliver_calls,
    make_pair,
    queued_behaviors,
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


def last_body(stub) -> str:
    return deliver_calls(stub)[-1]["body"]


def shortcut_rows(db, partner_id: int):
    """The shortcut channel for one partner, in the order it drains."""
    return db.read(
        "SELECT s.behavior AS behavior, s.body AS body "
        "  FROM shortcut_channel s JOIN label_caps c ON c.behavior = s.behavior "
        " WHERE s.waiting_partner = ? ORDER BY c.priority, s.enqueued_at",
        (partner_id,),
    )


def second_worker(core: MessagingCore, caller: dict) -> dict:
    """Another worker under the same caller, so two agents can reach one waiter."""
    project_id = core.db.read_one(
        "SELECT project_id FROM partners WHERE id = ?", (caller["id"],)
    )["project_id"]
    remote = _unique("remote")
    worker = core.create_partner(
        project_id=project_id, title=_unique("worker2"),
        partner_id_in_remote=remote, descr="d",
    )
    worker["remote_id"] = remote
    core.handshake(requester_uuid=caller["uuid"], partner_title=worker["title"])
    return worker


# ---------------------------------------------------------------------------
# 1. Sending a request forces the sender, and empties its slot.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["[QUERY]", "[ERROR]", "[RESEARCH]"])
def test_every_request_label_forces_its_sender(db, core, stub, label):
    """`label_caps.is_request` decides, and all three behave identically.

    Parameterised rather than written three times because the rule is about the
    COLUMN, not about any particular label -- a fourth request label added later
    should be added here and pass with no new logic.
    """
    # Driven from the CALLER, because [RESEARCH] only travels down or sideways
    # and the caller is the only one of the pair that may send it. The rule under
    # test is about the sender, so which end sends is incidental -- but it has to
    # be an end allowed to send that label at all.
    caller, worker = make_pair(core)
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="here is a report", behavior="[TRUTHFUL-REPORT]")
    core.advance(partner_id=caller["id"])

    before = working(core, caller["id"])
    assert before is not None, "precondition: the caller should hold a task"

    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="blocked", behavior=label)

    assert working(core, caller["id"]) is None, (
        f"sending a {label} must EMPTY the sender's slot, not fill it with a placeholder"
    )
    assert core.slots.is_forced(caller["id"]), (
        f"sending a {label} must open a forced interruption"
    )
    assert before["behavior"] in queued_behaviors(db, caller["id"]), (
        "the work it was doing must be pushed back, not lost"
    )


def test_the_sender_is_forced_even_with_nothing_in_flight(db, core, stub):
    """There is no work to protect, but there is still a wait to represent."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="a question", behavior="[QUERY]")

    assert working(core, worker["id"]) is None
    assert core.slots.is_forced(worker["id"])


def test_direction_does_not_matter(db, core, stub):
    """A Caller that asks is stopped exactly like a Partner that asks.

    Whoever sent the request said the same thing -- *I need this before I go on*
    -- and an orchestrator that keeps driving other work while blocked is an
    orchestrator producing work it will have to redo.
    """
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="what is x?", behavior="[QUERY]")
    assert core.slots.is_forced(caller["id"])


def test_nothing_is_delivered_to_a_forced_agent(db, core, stub):
    """Its remote was just stopped. A paragraph would give it something to do."""
    caller, worker = make_pair(core)
    before = len(deliver_calls(stub))
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="a question", behavior="[QUERY]")
    delivered = [c for c in deliver_calls(stub)[before:]
                 if c["partner_id_in_remote"] == worker["remote_id"]]
    assert delivered == [], f"the forced agent was sent: {delivered}"


# ---------------------------------------------------------------------------
# 2. A forced slot cannot be filled. By anything.
# ---------------------------------------------------------------------------


def test_not_even_the_top_priority_label_fills_a_forced_slot(db, core, stub):
    """This is what makes force outrank priority rather than race it.

    `[TRUTHFUL-REPORT]` is priority 1, the top of the ladder. Under a forced
    interruption it waits like everything else -- because there is no task to
    displace, and a forced slot is empty ON PURPOSE.
    """
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate", behavior="[RESEARCH]")
    assert core.slots.is_forced(caller["id"])

    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="here is the report", behavior="[TRUTHFUL-REPORT]")
    core.advance(partner_id=caller["id"])

    assert working(core, caller["id"]) is None, (
        "a priority-1 arrival must not fill a slot held empty by a forced interruption"
    )
    assert core.slots.is_forced(caller["id"])
    assert "[TRUTHFUL-REPORT]" in queued_behaviors(db, caller["id"]), (
        "it must be queued, not dropped -- enqueuing continues during an interruption"
    )


def test_enqueuing_continues_during_an_interruption(db, core, stub):
    """The queue keeps filling; only the SLOT is held shut."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate", behavior="[RESEARCH]")

    for i in range(3):
        core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
                  message=f"report {i}", behavior="[TRUTHFUL-REPORT]")

    assert len(queued_behaviors(db, caller["id"])) == 3, (
        "messages must still be admitted while the recipient is forced"
    )
    assert working(core, caller["id"]) is None


def test_a_forced_agent_cannot_send_a_second_request(db, core, stub):
    """It is stopped. A second request is one it could not act on the answer to."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="which dataset?", behavior="[QUERY]")

    # [RESEARCH] is omitted deliberately: worker -> caller is upward, so it is
    # refused by the hierarchy rule BEFORE the wait check is reached. That it is
    # refused for a different reason is correct, and asserting this code here
    # would be asserting the wrong rule.
    for label in ("[QUERY]", "[ERROR]"):
        with pytest.raises(Rejected) as exc:
            core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
                      message="and another", behavior=label)
        assert exc.value.code == "already_awaiting_an_answer", (
            f"a forced agent sending a {label} must be refused "
            f"already_awaiting_an_answer, got {exc.value.code!r}"
        )

    assert core.slots.is_forced(worker["id"]), "the original wait must be intact"


def test_it_may_ask_again_once_answered(db, core, stub):
    """The refusal is a wait, not a ban."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="which dataset?", behavior="[QUERY]")
    core.resolve_wait(partner_id=worker["id"], body="the 2024 one")

    result = core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
                       message="and which split?", behavior="[QUERY]")
    assert result["behavior"] == "[QUERY]"


# ---------------------------------------------------------------------------
# 3. Resolution: the answer is not a message.
# ---------------------------------------------------------------------------


def test_resolve_wait_lifts_the_force_and_records_the_answer(db, core, stub):
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate x", behavior="[RESEARCH]")

    assert core.resolve_wait(partner_id=caller["id"], body="use the 2024 set") is True
    assert core.slots.is_forced(caller["id"]) is False

    rows = db.read(
        "SELECT m.behavior AS behavior, r.body AS body, "
        "       m.response_datetime IS NOT NULL AS stamped "
        "  FROM messages m JOIN message_response r ON r.message_id = m.id"
    )
    assert len(rows) == 1, f"exactly one answer should be recorded, got {rows}"
    assert rows[0]["behavior"] == "[RESEARCH]", (
        "the answer is recorded against the REQUEST it answers"
    )
    assert rows[0]["body"] == "use the 2024 set"
    assert rows[0]["stamped"], "response_datetime must be stamped in the same transaction"


def test_resolving_a_wait_that_is_not_open_is_a_no_op(db, core, stub):
    """A response can be observed twice; the second observer must not raise."""
    caller, worker = make_pair(core)
    assert core.resolve_wait(partner_id=caller["id"], body="nobody asked") is False

    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="q", behavior="[QUERY]")
    assert core.resolve_wait(partner_id=caller["id"], body="first") is True
    assert core.resolve_wait(partner_id=caller["id"], body="second") is False, (
        "resolving twice must be harmless, not a second recorded answer"
    )
    n = db.read("SELECT COUNT(*) AS n FROM message_response")[0]["n"]
    assert n == 1, f"the second resolve must not record another answer, got {n}"


def test_the_answer_is_folded_into_paused_work(db, core, stub):
    """Resuming names the label only -- the agent never stopped holding the body."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate x", behavior="[RESEARCH]")
    core.resolve_wait(partner_id=caller["id"], body="carry on")

    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="which dataset?", behavior="[QUERY]")
    core.resolve_wait(partner_id=worker["id"], body="the 2024 one")
    core.advance(partner_id=worker["id"])

    body = last_body(stub)
    assert "the 2024 one" in body, f"the answer must reach the agent: {body}"
    assert "[RESEARCH]" in body, "and it must name the work being resumed"


def test_the_answer_is_folded_into_a_new_job(db, core, stub):
    """A fresh job is quoted in full -- the agent has not seen it before."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="which dataset?", behavior="[QUERY]")
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="brand new instruction", behavior="[TRUTHFUL-REPORT]")
    core.resolve_wait(partner_id=worker["id"], body="the 2024 one")
    core.advance(partner_id=worker["id"])

    body = last_body(stub)
    assert "the 2024 one" in body
    assert "brand new instruction" in body, (
        f"a fresh job must be quoted in full, not named by label: {body}"
    )


def test_the_answer_stands_alone_when_nothing_is_waiting(db, core, stub):
    """The one case where a bare response IS the right prompt."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="which dataset?", behavior="[QUERY]")
    before = len(deliver_calls(stub))
    core.resolve_wait(partner_id=worker["id"], body="the 2024 one")
    core.advance(partner_id=worker["id"])

    mine = [c["body"] for c in deliver_calls(stub)[before:]
            if c["partner_id_in_remote"] == worker["remote_id"]]
    assert mine, "the resolved agent should have been delivered its answer"
    body = mine[-1]
    assert "the 2024 one" in body
    assert "Resume your work with this new job" not in body, (
        f"nothing was waiting, so nothing should be attached: {body}"
    )


# ---------------------------------------------------------------------------
# 4. The shortcut channel.
# ---------------------------------------------------------------------------


def test_a_request_to_a_waiting_agent_takes_the_shortcut(db, core, stub):
    """The message most likely to be sent right now must not be the one stranded.

    A Partner that hits a mid-task problem needs to reach a Caller that is
    itself waiting -- and it needs to reach it NOW, because what it is sending
    may be exactly what the Caller is waiting to hear about.
    """
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate x", behavior="[RESEARCH]")
    assert core.slots.is_forced(caller["id"])

    result = core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
                       message="need approval for /tmp/x", behavior="[ERROR]")

    assert result["shortcut"] is True, f"expected the shortcut route, got {result}"
    assert [r["body"] for r in shortcut_rows(db, caller["id"])] == ["need approval for /tmp/x"]
    assert queued_behaviors(db, caller["id"]) == [], (
        "the main queue must be untouched -- that is the whole point of the shortcut"
    )


def test_the_shortcut_is_itself_a_priority_queue(db, core, stub):
    """`[ERROR]` outranks `[QUERY]` here, on the one channel guaranteed to be read.

    This is where promoting `[ERROR]` above `[QUERY]` earns itself: an approval
    failure jumps ahead of a clarification question. Two senders are needed
    because a single agent is forced by its own first request and cannot send a
    second.
    """
    caller, w1 = make_pair(core)
    w2 = second_worker(core, caller)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=w1["title"],
              message="investigate", behavior="[RESEARCH]")

    # The question is sent FIRST; priority must reorder it behind the error.
    core.send(requester_uuid=w2["uuid"], queried_partner_title=caller["title"],
              message="a question", behavior="[QUERY]")
    core.send(requester_uuid=w1["uuid"], queried_partner_title=caller["title"],
              message="an error", behavior="[ERROR]")

    assert [r["behavior"] for r in shortcut_rows(db, caller["id"])] == ["[ERROR]", "[QUERY]"], (
        "the shortcut must drain by label_caps.priority, not by arrival order"
    )


def test_research_may_not_take_the_shortcut(db, core, stub):
    """It is a request, but the channel is for clarification, not new work.

    Delegating fresh work to an agent that has just said it cannot proceed would
    leave that work sitting in a channel that is deleted when the wait ends.
    """
    caller, worker = make_pair(core)
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="blocked", behavior="[QUERY]")

    with pytest.raises(Rejected) as exc:
        core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
                  message="new work", behavior="[RESEARCH]")
    assert exc.value.code == "target_is_waiting", (
        f"expected target_is_waiting, got {exc.value.code!r}"
    )
    assert shortcut_rows(db, worker["id"]) == []


def test_the_shortcut_dies_with_the_wait_it_routed_around(db, core, stub):
    """A row in it can never outlive the interruption it existed for."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate x", behavior="[RESEARCH]")
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="need approval", behavior="[ERROR]")
    assert len(shortcut_rows(db, caller["id"])) == 1

    core.resolve_wait(partner_id=caller["id"], body="approved")

    assert shortcut_rows(db, caller["id"]) == [], (
        "the channel must be destroyed when the wait ends"
    )
    assert db.read("SELECT COUNT(*) AS n FROM shortcut_channel")[0]["n"] == 0, (
        "and the table should be empty whenever no interruption is outstanding"
    )


def test_the_shortcut_refuses_an_answer_label(db, core, stub):
    """`[TRUTHFUL-REPORT]` is an answer. Answers go through the main queue."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate x", behavior="[RESEARCH]")

    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="the report", behavior="[TRUTHFUL-REPORT]")

    assert shortcut_rows(db, caller["id"]) == [], (
        "an answer must not enter the shortcut channel"
    )
    assert "[TRUTHFUL-REPORT]" in queued_behaviors(db, caller["id"]), (
        "it belongs in the main queue, waiting for the force to lift"
    )
