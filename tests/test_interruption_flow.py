"""Tests for what happens when a Partner cannot finish without its Caller.

A Partner working a `[RESEARCH]` sometimes hits something only the agent that
sent it can resolve -- a path it was not granted, a question about what was
actually meant. It raises a `[QUERY]` or an `[ERROR]` upward and then has to
stop, because the next queued message would otherwise reach an agent that is
blocked on an unanswered question and interleave the two.

`[IDLE]` is the hold that stops it. It is not a message and carries no prompt:
the remote is stopped by `stop_remote_execution` before the swap, and typing a
paragraph at a stopped agent gives it something to act on when the whole point
is that it should be doing nothing.

The hold ends by itself. Anything displaces an `[IDLE]`, so the Caller's answer
takes the slot and the paused work resumes behind it.
"""

from __future__ import annotations

import pytest

from extension.base import StubExtension
from messaging_core.core import MessagingCore
from messaging_core.db import Database
from messaging_core.labels import INTERRUPT_BEHAVIOR
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


def working_behavior(core: MessagingCore, partner_id: int) -> str | None:
    task = core.working_task(partner_id=partner_id)
    return None if task is None else task["behavior"]


# ---------------------------------------------------------------------------
# 1. An [IDLE] is a hold, not a message.
# ---------------------------------------------------------------------------


def test_an_idle_hold_delivers_nothing_to_the_remote(db, core, stub):
    """The remote is already stopped. There is nothing to say to it."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate x", behavior="[RESEARCH]")
    before = len(deliver_calls(stub))

    core.interrupt_partner(requester_uuid=caller["uuid"], partner_title=worker["title"],
                           reason="stop for now")

    assert working_behavior(core, worker["id"]) == INTERRUPT_BEHAVIOR, (
        "the hold must still take the working slot"
    )
    assert len(deliver_calls(stub)) == before, (
        "an [IDLE] must not be handed to the remote; deliveries: "
        f"{[c['behavior'] for c in deliver_calls(stub)]}"
    )


def test_the_held_task_is_paused_and_resumes_afterwards(db, core, stub):
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate x", behavior="[RESEARCH]")
    core.interrupt_partner(requester_uuid=caller["uuid"], partner_title=worker["title"],
                           reason="stop")
    assert "[RESEARCH]" in queued_behaviors(db, worker["id"])

    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="carry on", behavior="[MESSAGE-RESPONSE]")

    assert working_behavior(core, worker["id"]) == "[MESSAGE-RESPONSE]", (
        "anything displaces a hold, so the answer takes the slot"
    )


# ---------------------------------------------------------------------------
# 2. A Partner that raises a question upward parks itself.
# ---------------------------------------------------------------------------


def test_a_partner_raising_a_query_upward_parks_its_own_slot(db, core, stub):
    """Otherwise the next queued message reaches an agent that is blocked."""
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate x", behavior="[RESEARCH]")
    assert working_behavior(core, worker["id"]) == "[RESEARCH]"

    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="which dataset did you mean?", behavior="[QUERY]")

    assert working_behavior(core, worker["id"]) == INTERRUPT_BEHAVIOR, (
        "a Partner that has just asked its Caller a question must stop and wait"
    )
    assert "[RESEARCH]" in queued_behaviors(db, worker["id"]), (
        "and the work it was doing must be paused, not lost"
    )


def test_a_partner_raising_an_error_upward_also_parks(db, core, stub):
    caller, worker = make_pair(core)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate x", behavior="[RESEARCH]")

    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="that path does not exist", behavior="[ERROR]")

    assert working_behavior(core, worker["id"]) == INTERRUPT_BEHAVIOR


def test_a_caller_dispatching_a_query_downward_does_not_park_itself(db, core, stub):
    """The forward direction is routine work, not a Partner waiting on an answer.

    This is what the direction test is for: without it, an orchestrator that
    holds a working slot would stop itself every time it asked a worker
    anything.
    """
    caller, worker = make_pair(core)
    # Give the caller a working slot of its own.
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="here is the report", behavior="[TRUTHFUL-REPORT]")
    assert working_behavior(core, caller["id"]) == "[TRUTHFUL-REPORT]"

    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="what is x?", behavior="[QUERY]")

    assert working_behavior(core, caller["id"]) == "[TRUTHFUL-REPORT]", (
        "a Caller asking a worker a question is dispatching work, and must keep "
        f"working; its slot holds {working_behavior(core, caller['id'])!r}"
    )


def test_a_partner_with_nothing_in_flight_does_not_park(db, core, stub):
    """There is no work to protect, so there is nothing to hold."""
    caller, worker = make_pair(core)

    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="a question", behavior="[QUERY]")

    assert working_behavior(core, worker["id"]) is None


# ---------------------------------------------------------------------------
# 3. The whole round trip.
# ---------------------------------------------------------------------------


def test_the_answer_releases_the_hold_and_the_work_resumes(db, core, stub, server, monkeypatch):
    """The flow end to end, which is the reason the hold exists at all."""
    caller, worker = make_pair(core)
    suppress_no_op(monkeypatch, server)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="investigate x", behavior="[RESEARCH]")

    # The Partner hits something only its Caller can resolve, and stops.
    core.send(requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
              message="which dataset?", behavior="[QUERY]")
    assert working_behavior(core, worker["id"]) == INTERRUPT_BEHAVIOR
    assert "[QUERY]" in queued_behaviors(db, caller["id"]) or \
        working_behavior(core, caller["id"]) == "[QUERY]", (
        "the question must reach the Caller"
    )

    # The Caller answers.
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="the 2024 one", behavior="[MESSAGE-RESPONSE]")

    assert working_behavior(core, worker["id"]) == "[MESSAGE-RESPONSE]", (
        "the answer displaces the hold"
    )
    assert "[RESEARCH]" in queued_behaviors(db, worker["id"]), (
        "and the paused work is still there to resume"
    )


def test_an_error_is_answered_so_the_sender_knows_it_landed(db, core, stub, server, monkeypatch):
    """A Caller that corrects a blocked Partner otherwise never learns it worked."""
    caller, worker = make_pair(core)
    suppress_no_op(monkeypatch, server)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="that path does not exist", behavior="[ERROR]")

    server.drain_once(partner_id=worker["id"])

    assert "[MESSAGE-RESPONSE]" in queued_behaviors(db, caller["id"]), (
        "an [ERROR] must be answered; the caller's queue holds "
        f"{queued_behaviors(db, caller['id'])}"
    )


def test_the_answer_to_an_error_ends_the_exchange(db, core, stub, server, monkeypatch):
    """One hop, not an endless correction loop."""
    caller, worker = make_pair(core)
    suppress_no_op(monkeypatch, server)
    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="that path does not exist", behavior="[ERROR]")
    server.drain_once(partner_id=worker["id"])

    assert core.reply_behavior("[MESSAGE-RESPONSE]") is None, (
        "a [MESSAGE-RESPONSE] replies with nothing, which is what stops the exchange"
    )
