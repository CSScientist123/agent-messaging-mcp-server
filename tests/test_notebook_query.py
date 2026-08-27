"""Tests for asking a notebook in its own terms.

A NotebookLM Partner is not an agent. It holds sources and answers questions
about them; it never acts, and `source_caps` says so -- `can_execute = 0`,
`can_send = 0`, `accepts_research = 0`. The generic relay template is written
for an agent: it announces who is speaking, hands over a message, and tells the
recipient how to answer back. Two of those three mean nothing to a notebook.
"""

from __future__ import annotations

import pytest

from extension.base import StubExtension
from messaging_core import templates
from messaging_core.core import MessagingCore
from messaging_core.db import Database

_counter = 0


def _unique(prefix: str) -> str:
    global _counter
    _counter += 1
    return f"nb-{prefix}-{_counter}"


@pytest.fixture
def db():
    database = Database(path=":memory:")
    yield database
    database.close()


@pytest.fixture
def core(db):
    return MessagingCore(db, extension=StubExtension(source_prefix="science_"))


@pytest.fixture
def world(core):
    """A science_ orchestrator and a notebook source it may query directly."""
    core.extension = StubExtension(source_prefix="science_")
    sci = core.create_project(title=_unique("sci"), source_prefix="science_",
                              project_system_id=_unique("psid"))
    asker = core.create_partner(project_id=sci, title=_unique("asker"),
                                partner_id_in_remote=_unique("rem"), descr="d")
    core.claim_orchestrator(requester_uuid=asker["uuid"], project_id=sci,
                            orchestrator_type="project-orchestrator")

    core.extension = StubExtension(source_prefix="nlm_")
    notebook = core.create_project(title=_unique("nb"), source_prefix="nlm_",
                                   project_system_id="notebook-42")
    source = core.create_partner(project_id=notebook, title=_unique("src"),
                                 partner_id_in_remote="https://example.org/paper",
                                 descr="a paper about photosynthesis")
    return {"asker": asker, "source": source, "stub": core.extension}


def delivered_body(stub) -> str:
    calls = [kwargs for name, kwargs in stub.calls if name == "deliver_message"]
    assert calls, "nothing was delivered"
    return calls[-1]["body"]


def test_a_query_to_a_notebook_names_the_source_it_targets(core, world):
    """The CLI has no per-source query, so the prompt is where the aim happens."""
    core.send(requester_uuid=world["asker"]["uuid"],
              queried_partner_title=world["source"]["title"],
              message="What does it say about quantum coherence?", behavior="[QUERY]")

    body = delivered_body(world["stub"])
    assert "https://example.org/paper" in body, (
        f"the targeted source must appear in the prompt; got:\n{body}"
    )
    assert "What does it say about quantum coherence?" in body


def test_a_query_to_a_notebook_does_not_invite_it_to_message_back(core, world):
    """`can_send = 0`: there is no agent behind a notebook to call `send`.

    An identity block here would be an instruction nothing can follow.
    """
    core.send(requester_uuid=world["asker"]["uuid"],
              queried_partner_title=world["source"]["title"],
              message="what is x?", behavior="[QUERY]")

    body = delivered_body(world["stub"])
    assert world["source"]["uuid"] not in body, (
        f"a notebook must not be handed a requester_uuid; got:\n{body}"
    )
    assert "send(" not in body


def test_a_query_to_an_agent_still_uses_the_relay(core, world):
    """The notebook shape is for notebooks only; nothing else changes."""
    core.extension = StubExtension(source_prefix="science_")
    other = core.create_partner(
        project_id=core.db.read_one("SELECT project_id FROM partners WHERE id = ?",
                                    (world["asker"]["id"],))["project_id"],
        title=_unique("worker"), partner_id_in_remote=_unique("rem"), descr="d")
    core.handshake(requester_uuid=world["asker"]["uuid"], partner_title=other["title"])

    core.send(requester_uuid=world["asker"]["uuid"], queried_partner_title=other["title"],
              message="what is x?", behavior="[QUERY]")

    body = delivered_body(core.extension)
    assert other["uuid"] in body, (
        "an agent still gets its identity, because it can act on it"
    )


def test_the_notebook_template_is_a_relay_not_an_instruction(core, world):
    """The Server is showing a notebook something, not telling an agent to do something."""
    core.send(requester_uuid=world["asker"]["uuid"],
              queried_partner_title=world["source"]["title"],
              message="what is x?", behavior="[QUERY]")

    body = delivered_body(world["stub"])
    assert body.startswith(templates.RELAYS), f"got: {body[:80]!r}"
