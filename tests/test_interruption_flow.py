"""Tests for what happens when an agent cannot continue on its own.

An agent -- any agent, Caller or Partner -- sometimes hits something only
another one can resolve: a path it was not granted, a question about what was
actually meant. It sends a `[QUERY]` or an `[ERROR]`, and that act stops it.

There is no separate hold label. The question the agent asked takes its own
working slot, and it is defended there at its own natural priority -- `[QUERY]`
and `[ERROR]` are never raised above the 2 they already hold. That is enough to
make an unanswered question a blocker: only `[TRUTHFUL-REPORT]`, at 1, outranks
it, and a caller owed a summary outranks the asker's own question by design.
**The question is the hold.**

A displaced question is not lost. It goes back to the queue still marked
`awaiting_resolution`, outranks everything else in that agent's queue when the
summary finishes, and re-enters the wait rather than being handed back as work
the agent is somehow supposed to do.

What clears it is the answer, not a displacement. When the `[MESSAGE-RESPONSE]`
arrives, the question is discarded -- never requeued -- and the answer is
folded into whatever the queue holds next, because a bare response is close to
useless as a prompt: the agent would be holding a fact and no instruction.
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


def working(core: MessagingCore, partner_id: int):
    return core.working_task(partner_id=partner_id)


def last_body(stub) -> str:
    return deliver_calls(stub)[-1]["body"]


# ---------------------------------------------------------------------------
# 1. Asking a blocking question stops the asker.
# ---------------------------------------------------------------------------


def test_sending_a_query_stops_the_sender(db, core, stub):
    """The agent said it cannot continue without this. It cannot continue."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate x", behavior="[RESEARCH]")
    assert working(core, worker["id"])["behavior"] == "[RESEARCH]"

    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="which dataset did you mean?", behavior="[QUERY]")

    held = working(core, worker["id"])
    assert held["behavior"] == "[QUERY]" and held.get("awaiting_resolution"), (
        f"the question itself must take the slot; got {held}"
    )
    assert "[RESEARCH]" in queued_behaviors(db, worker["id"]), (
        "and the work it was doing must be paused, not lost"
    )


def test_sending_an_error_stops_the_sender(db, core, stub):
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate x", behavior="[RESEARCH]")

    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="that path does not exist", behavior="[ERROR]")

    assert working(core, worker["id"])["behavior"] == "[ERROR]"


def test_a_caller_asking_downward_is_stopped_too(db, core, stub):
    """Direction does not matter. Whoever asked cannot continue.

    An earlier shape of this rule only stopped an agent answering upward, on
    the grounds that a Caller dispatching work should keep working. But a
    Caller that sends a `[QUERY]` has said the same thing a Partner does when
    it sends one: I need this before I go on.
    """
    caller, worker = make_pair(core)
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="here is the report", behavior="[TRUTHFUL-REPORT]")
    assert working(core, caller["id"])["behavior"] == "[TRUTHFUL-REPORT]"

    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="what is x?", behavior="[QUERY]")

    assert working(core, caller["id"])["behavior"] == "[QUERY]", (
        "a Caller that asks a blocking question is stopped like anyone else"
    )
    assert "[TRUTHFUL-REPORT]" in queued_behaviors(db, caller["id"])


def test_the_asker_stops_even_with_nothing_in_flight(db, core, stub):
    """There is no work to protect, but there is still a wait to represent."""
    caller, worker = make_pair(core)

    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="a question", behavior="[QUERY]")

    assert working(core, worker["id"])["behavior"] == "[QUERY]"


def test_nothing_is_delivered_to_a_stopped_asker(db, core, stub):
    """Its remote was just stopped. A paragraph would give it something to do."""
    caller, worker = make_pair(core)
    before = len(deliver_calls(stub))

    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="a question", behavior="[QUERY]")

    delivered_to_worker = [
        c for c in deliver_calls(stub)[before:]
        if c["partner_id_in_remote"] == worker["remote_id"]
    ]
    assert delivered_to_worker == [], f"the stopped agent was sent: {delivered_to_worker}"


def test_only_a_truthful_report_displaces_a_waiting_agent(db, core, stub):
    """The question is defended at its own natural priority, not a raised one.

    `[QUERY]` and `[ERROR]` sit at priority 2 and are never promoted above it.
    That is what makes an unanswered question a blocker without inventing a
    special rule for it: only `[TRUTHFUL-REPORT]`, at 1, outranks the wait, and
    everything else queues behind it.
    """
    caller, worker = make_pair(core)
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="a question", behavior="[QUERY]")

    # Ordinary work does not get through.
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate y", behavior="[RESEARCH]")
    assert working(core, worker["id"])["behavior"] == "[QUERY]", (
        "an agent waiting on an answer must not be handed other work"
    )
    assert "[RESEARCH]" in queued_behaviors(db, worker["id"])

    # A summary does, because its caller is owed the report before the agent's
    # own question is worth anything.
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="summarise what you have", behavior="[TRUTHFUL-REPORT]")
    assert working(core, worker["id"])["behavior"] == "[TRUTHFUL-REPORT]", (
        "a [TRUTHFUL-REPORT] outranks the wait and must take the slot"
    )


def test_a_stopped_agent_cannot_ask_a_second_question(db, core, stub):
    """It is stopped. A second question is one it could not act on the answer to.

    And the queue would not survive it: `_await_answer` pushes the working task
    back paused, so asking again would requeue the FIRST question as a second
    paused row of its own label -- two rows one resume line cannot name.
    """
    caller, worker = make_pair(core)
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="which dataset?", behavior="[QUERY]")

    for behavior in ("[QUERY]", "[ERROR]"):
        with pytest.raises(Rejected) as exc_info:
            core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
                      message="and another thing", behavior=behavior)
        assert exc_info.value.code == "already_awaiting_an_answer", (
            f"a stopped agent sending a {behavior} must be refused "
            f"already_awaiting_an_answer, got {exc_info.value.code!r}"
        )

    # Refused, and nothing half-applied: still exactly one question, still the
    # first one, and the caller's queue is untouched by the two attempts.
    held = working(core, worker["id"])
    assert held is not None and held["body"].count("which dataset?") <= 1
    assert held["behavior"] == "[QUERY]", f"the original wait must be intact, got {held!r}"
    parked = db.read(
        "SELECT * FROM message_queue WHERE partner_id = ?", (worker["id"],)
    )
    assert parked == [], (
        f"a refused second question must not requeue the first: {[dict(r) for r in parked]}"
    )
    assert queued_behaviors(db, caller["id"]).count("[QUERY]") <= 1, (
        "the refused questions must never reach the agent they were aimed at"
    )


def test_a_stopped_agent_may_ask_again_once_it_is_answered(db, core, stub):
    """The refusal is a wait, not a ban."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="which dataset?", behavior="[QUERY]")
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="the 2024 one", behavior="[MESSAGE-RESPONSE]")

    result = core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
                       message="and which split?", behavior="[QUERY]")
    assert result["behavior"] == "[QUERY]", (
        f"once answered, an agent may ask again; got {result!r}"
    )


def test_a_displaced_wait_outranks_ordinary_paused_work_of_its_own_label(db, core, stub):
    """Which row resumes when the summary finishes, and why it is not a coin toss.

    The agent is holding a paused `[QUERY]` it was given as WORK, and a
    displaced `[QUERY]` of its own that is still unanswered. Both are paused,
    both carry the same label, so `in_process` and arrival order decide nothing
    useful -- and the work row arrived first, so plain chronology picks exactly
    the wrong one.

    `awaiting_resolution` is read ahead of both. The wait resumes, because
    handing the agent work it still cannot do would also leave the answer, when
    it arrives, with nothing in the slot to resolve.
    """
    caller, worker = make_pair(core)

    # 1. The caller gives the worker a [QUERY] as work, and it takes the slot.
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="what is X?", behavior="[QUERY]")
    assert working(core, worker["id"])["body"] == "what is X?"

    # 2. The worker asks its own question. Its work is pushed back paused and
    #    the question takes the slot.
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="which dataset?", behavior="[QUERY]")
    assert working(core, worker["id"])["awaiting_resolution"]
    assert "[QUERY]" in queued_behaviors(db, worker["id"])

    # 3. A summary displaces the wait. Now BOTH [QUERY] rows are in the queue,
    #    both paused, and the work row is the older of the two.
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="summarise what you have", behavior="[TRUTHFUL-REPORT]")
    rows = db.read(
        "SELECT body, in_process, awaiting_resolution FROM message_queue "
        "WHERE partner_id = ? AND behavior = '[QUERY]' ORDER BY id", (worker["id"],)
    )
    assert len(rows) == 2 and all(r["in_process"] == 1 for r in rows), (
        f"setup failed: expected two paused [QUERY] rows, got {[dict(r) for r in rows]}"
    )
    assert rows[0]["awaiting_resolution"] == 0 and rows[1]["awaiting_resolution"] == 1, (
        "setup failed: the work row must be the OLDER of the two, or chronology alone "
        f"would pick the wait and this stops isolating the tie-break: {[dict(r) for r in rows]}"
    )

    # 4. The summary finishes. The wait is what comes back.
    core.release(partner_id=worker["id"])
    core.advance(partner_id=worker["id"])
    resumed = working(core, worker["id"])
    assert resumed is not None and resumed["awaiting_resolution"], (
        f"an unanswered question must outrank work of its own label; got {resumed!r}"
    )

    # 5. And the answer still resolves it, folding in the work that was waiting.
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="the 2024 one", behavior="[MESSAGE-RESPONSE]")
    body = last_body(stub)
    assert "Resolution attempt on [QUERY] is returned." in body, body
    assert "the 2024 one" in body
    assert "Resume your work on" in body, (
        f"the paused work is what it goes back to, named by label: {body}"
    )


def test_a_displaced_wait_returns_to_waiting_rather_than_becoming_work(db, core, stub):
    """The question survives being displaced, and is not handed back as a job.

    Without `awaiting_resolution` on the requeued row, the question would come
    back looking like an ordinary `[QUERY]` its caller had sent -- and the
    agent would be told to answer a question it asked.
    """
    caller, worker = make_pair(core)
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="a question", behavior="[QUERY]")
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="summarise what you have", behavior="[TRUTHFUL-REPORT]")

    parked = db.read(
        "SELECT * FROM message_queue WHERE partner_id = ? AND behavior = '[QUERY]'",
        (worker["id"],),
    )
    assert len(parked) == 1 and parked[0]["awaiting_resolution"] == 1, (
        f"a displaced wait must stay marked as a wait: {[dict(r) for r in parked]}"
    )

    calls_before = len(stub.calls)
    core.release(partner_id=worker["id"])
    core.advance(partner_id=worker["id"])
    resumed = working(core, worker["id"])
    assert resumed is not None and resumed["awaiting_resolution"], (
        f"the promoted wait must re-enter the wait, not become work: {resumed!r}"
    )
    assert not [c for c in stub.calls[calls_before:] if c[0] == "deliver_message"], (
        "nothing is said to an agent that is waiting on its own question"
    )

    # And the answer still resolves it afterwards.
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="use the 2024 set", behavior="[MESSAGE-RESPONSE]")
    body = last_body(stub)
    assert "Resolution attempt on [QUERY] is returned." in body, body
    assert "use the 2024 set" in body


# ---------------------------------------------------------------------------
# 2. The answer arrives, and is folded into what comes next.
# ---------------------------------------------------------------------------


def test_the_answer_alone_is_delivered_when_nothing_is_waiting(db, core, stub):
    """The one case where a bare response IS the right prompt."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="which dataset?", behavior="[QUERY]")

    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="the 2024 one", behavior="[MESSAGE-RESPONSE]")

    body = last_body(stub)
    assert "Resolution attempt on [QUERY] is returned." in body, body
    assert "the 2024 one" in body
    assert "Resume your work" not in body, f"nothing was waiting: {body}"


def test_the_answer_is_concatenated_with_paused_work(db, core, stub):
    """Resuming names the label only -- the agent never stopped holding the body."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate x", behavior="[RESEARCH]")
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="which dataset?", behavior="[QUERY]")

    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="the 2024 one", behavior="[MESSAGE-RESPONSE]")

    body = last_body(stub)
    assert "Resolution attempt on [QUERY] is returned." in body, body
    assert "the 2024 one" in body
    assert "Resume your work on: [RESEARCH]" in body, body


def test_the_answer_is_concatenated_with_a_new_job(db, core, stub):
    """A fresh job is restated in full -- the agent has never seen it."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="which dataset?", behavior="[QUERY]")
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="a brand new assignment", behavior="[RESEARCH]")

    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="the 2024 one", behavior="[MESSAGE-RESPONSE]")

    body = last_body(stub)
    assert "Resolution attempt on [QUERY] is returned." in body, body
    assert "the 2024 one" in body
    assert "Resume your work with this new job:" in body, body
    assert "a brand new assignment" in body, body


def test_the_question_is_never_requeued(db, core, stub):
    """It was asked and answered. Requeuing it would ask again."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="which dataset?", behavior="[QUERY]")
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="the 2024 one", behavior="[MESSAGE-RESPONSE]")

    assert "[QUERY]" not in queued_behaviors(db, worker["id"]), (
        f"the resolved question came back: {queued_behaviors(db, worker['id'])}"
    )


def test_the_answers_own_row_is_consumed(db, core, stub):
    """It is folded into the next prompt, not promoted as a task of its own."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate x", behavior="[RESEARCH]")
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="which dataset?", behavior="[QUERY]")

    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="the 2024 one", behavior="[MESSAGE-RESPONSE]")

    assert "[MESSAGE-RESPONSE]" not in queued_behaviors(db, worker["id"])
    assert working(core, worker["id"])["behavior"] == "[RESEARCH]", (
        "the work behind the question is what the agent resumes"
    )


# ---------------------------------------------------------------------------
# 3. The resolver's side is ordinary.
# ---------------------------------------------------------------------------


def test_the_resolver_just_receives_the_question(db, core, stub, server, monkeypatch):
    """No special mechanism on the answering side: it is a message in a queue."""
    caller, worker = make_pair(core)
    suppress_no_op(monkeypatch, server)

    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="which dataset?", behavior="[QUERY]")

    assert working(core, caller["id"])["behavior"] == "[QUERY]" or \
        "[QUERY]" in queued_behaviors(db, caller["id"]), (
        "the question must reach the resolver like any other message"
    )


def test_an_error_is_answered_so_the_sender_knows_it_landed(db, core, stub, server, monkeypatch):
    caller, worker = make_pair(core)
    suppress_no_op(monkeypatch, server)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="that path does not exist", behavior="[ERROR]")

    server.drain_once(partner_id=worker["id"])

    assert "[MESSAGE-RESPONSE]" in queued_behaviors(db, caller["id"]) or \
        working(core, caller["id"]) is not None, (
        "an [ERROR] must be answered"
    )


def test_the_exchange_ends_at_the_answer(db, core, stub):
    """One hop, not an endless correction loop."""
    assert core.reply_behavior("[MESSAGE-RESPONSE]") is None
