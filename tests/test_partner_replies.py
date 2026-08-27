"""Tests for a Partner being able to say anything at all to its Caller.

Two independent things blocked it, and either one alone was enough.

A handshake is *claimed by an orchestrator* and points at the worker it
directs -- `handshake` refuses `requester_not_orchestrator` otherwise -- so a
worker had no row of its own in the reply direction and could never make one.
`send` refused `no_handshake`.

And the agent was never told its own `requester_uuid`, which is `send`'s first
argument. The research dispatch instructed it, in so many words, to message
back a [QUERY] when it was missing context -- an instruction to do something
the system refused and it lacked the credentials to attempt.

Both had to be fixed together; fixing either alone leaves the same silence.
"""

from __future__ import annotations

import pytest

from extension.base import StubExtension
from messaging_core.core import MessagingCore
from messaging_core.db import Database
from messaging_core.errors import Rejected

from tests.test_polling_working_slot import deliver_calls, make_pair


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


_counter = 0


def _unique(prefix: str) -> str:
    global _counter
    _counter += 1
    return f"pr-{prefix}-{_counter}"


# ---------------------------------------------------------------------------
# 1. The reply direction is open -- and only the reply direction.
# ---------------------------------------------------------------------------


def test_a_partner_can_answer_the_caller_that_handshook_it(db, core):
    """The handshake row points Caller -> Partner. The answer travels back along it."""
    caller, worker = make_pair(core)

    result = core.send(
        requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
        message="here is what I found", behavior="[MESSAGE-RESPONSE]",
    )

    assert result["partner_id"] == caller["id"], (
        f"the answer should have been queued for the caller, got: {result}"
    )


def test_a_partner_can_raise_a_query_to_its_caller_mid_task(db, core):
    """The dispatch tells the agent to do exactly this when it is missing context.

    An instruction the system refuses is worse than no instruction: the agent
    either guesses, or stops and says nothing.
    """
    caller, worker = make_pair(core)

    result = core.send(
        requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
        message="which dataset did you mean?", behavior="[QUERY]",
    )

    assert result["behavior"] == "[QUERY]"
    assert result["partner_id"] == caller["id"]


def test_a_partner_can_report_that_it_is_blocked(db, core):
    caller, worker = make_pair(core)

    result = core.send(
        requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
        message="I cannot reach the path you gave me", behavior="[ERROR]",
    )

    assert result["partner_id"] == caller["id"]


def test_the_reverse_direction_carries_answers_but_never_delegation(db, core):
    """Opening the answer direction must not silently invert the chain of command.

    A handshake points Caller -> Partner because an orchestrator claimed it.
    The layer rule alone does not protect that: a project-orchestrator and the
    plain science_ worker it directs sit at the SAME layer, so
    `research_cannot_flow_upward` (which refuses only a strictly higher target)
    would let the worker hand [RESEARCH] back to its own director. The forward
    row was what used to prevent it, so accepting the reverse row for every
    label would give that away as a side effect of letting answers home.
    """
    caller, worker = make_pair(core)

    with pytest.raises(Rejected) as exc_info:
        core.send(
            requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
            message="you go investigate x", behavior="[RESEARCH]",
        )

    assert exc_info.value.code == "research_needs_a_forward_handshake", (
        "delegation must still require the handshake to point at the target, got "
        f"{exc_info.value.code!r}"
    )


def test_research_still_cannot_travel_upward_across_layers(db, core):
    """The layer rule is untouched and still fires where it always did."""
    project_id = core.create_project(
        title=_unique("proj"), source_prefix="science_", project_system_id=_unique("psid")
    )
    po = core.create_partner(
        project_id=project_id, title=_unique("po"),
        partner_id_in_remote=_unique("remote"), descr="d",
    )
    core.claim_orchestrator(
        requester_uuid=po["uuid"], project_id=project_id,
        orchestrator_type="project-orchestrator",
    )
    go = core.create_partner(
        project_id=project_id, title=_unique("go"),
        partner_id_in_remote=_unique("remote"), descr="d",
    )
    core.claim_orchestrator(
        requester_uuid=go["uuid"], project_id=project_id,
        orchestrator_type="gemini-orchestrator",
    )
    core.grant_gemini_budget(
        requester_uuid=po["uuid"], grantee_uuid=go["uuid"], budget_count=2
    )
    core.extension = StubExtension(source_prefix="gemini_")
    gemini_project = core.create_project(
        title=_unique("proj"), source_prefix="gemini_", project_system_id=_unique("psid")
    )
    worker = core.create_partner(
        project_id=gemini_project, title=_unique("agy"),
        partner_id_in_remote=_unique("remote"), descr="d",
    )
    core.extension = StubExtension(source_prefix="science_")
    core.handshake(requester_uuid=go["uuid"], partner_title=worker["title"])

    with pytest.raises(Rejected) as exc_info:
        core.send(
            requester_uuid=worker["uuid"], queried_partner_title=go["title"],
            message="you go investigate x", behavior="[RESEARCH]",
        )

    assert exc_info.value.code == "research_cannot_flow_upward", (
        f"expected the layer rule to refuse this, got {exc_info.value.code!r}"
    )


def test_a_partner_can_still_answer_a_caller_that_outranks_it(db, core):
    """The whole point: everything that is not delegation still travels back."""
    caller, worker = make_pair(core)

    for behavior in ("[MESSAGE-RESPONSE]", "[TRUTHFUL-REPORT]", "[QUERY]", "[ERROR]"):
        result = core.send(
            requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
            message="x", behavior=behavior,
        )
        assert result["partner_id"] == caller["id"], (
            f"{behavior} must travel back along the reverse handshake, got: {result}"
        )


def test_a_partner_with_no_relationship_in_either_direction_is_still_refused(db, core):
    """Nothing new becomes reachable -- only the pair an orchestrator already joined."""
    caller, worker = make_pair(core)
    stranger = core.create_partner(
        project_id=db.read_one(
            "SELECT project_id FROM partners WHERE id = ?", (caller["id"],)
        )["project_id"],
        title=_unique("stranger"), partner_id_in_remote=_unique("remote"), descr="d",
    )

    with pytest.raises(Rejected) as exc_info:
        core.send(
            requester_uuid=worker["uuid"], queried_partner_title=stranger["title"],
            message="hello", behavior="[QUERY]",
        )

    assert exc_info.value.code == "no_handshake", (
        f"expected no_handshake for a pair with no row in either direction, got "
        f"{exc_info.value.code!r}"
    )


# ---------------------------------------------------------------------------
# 2. The agent is told who it is -- and told not to answer twice.
# ---------------------------------------------------------------------------


def test_the_research_dispatch_tells_the_agent_its_own_uuid(db, core, stub):
    """`send`'s first argument is the agent's own uuid. Nothing else ever states it."""
    caller, worker = make_pair(core)
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="investigate x", behavior="[RESEARCH]",
    )

    body = deliver_calls(stub)[0]["body"]
    assert worker["uuid"] in body, (
        "the dispatch must state the agent's own requester_uuid; without it the "
        f"instruction to message back cannot be followed. Prompt was:\n{body}"
    )
    assert caller["title"] in body, (
        "and the title to address the reply to"
    )


def test_the_dispatch_tells_the_agent_not_to_send_its_own_answer(db, core, stub):
    """Without this the fix causes every result to arrive twice.

    The Polling Server harvests the turn's output and delivers it. An agent
    that also sends it has reported the same work through two channels, and
    the Caller has no way to tell they are the same.
    """
    caller, worker = make_pair(core)
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="investigate x", behavior="[RESEARCH]",
    )

    body = deliver_calls(stub)[0]["body"].lower()
    assert "automatic" in body or "do not send" in body or "twice" in body, (
        "the dispatch must say that answering is automatic and must not be sent by "
        f"the agent. Prompt was:\n{deliver_calls(stub)[0]['body']}"
    )


def test_a_relayed_message_also_carries_the_recipients_identity(db, core, stub):
    """An agent handed an [ERROR] saying it is blocked may need to answer it."""
    caller, worker = make_pair(core)
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="that path does not exist", behavior="[ERROR]",
    )

    body = deliver_calls(stub)[0]["body"]
    assert worker["uuid"] in body, (
        f"a relayed message must carry the recipient's own uuid. Prompt was:\n{body}"
    )


def test_a_summary_request_does_not_invite_the_agent_to_send_it(db, core, stub):
    """A summary is harvested, never sent.

    Putting the identity block here would tell the agent to send exactly the
    thing the Polling Server is about to read off the session itself.
    """
    caller, worker = make_pair(core)
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="investigate x", behavior="[RESEARCH]",
    )
    prompt = core.begin_summary_phase(partner_id=worker["id"])

    assert prompt is not None
    assert worker["uuid"] not in prompt, (
        f"the summary request must not carry a send() invitation. Prompt was:\n{prompt}"
    )
