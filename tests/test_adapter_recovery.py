"""Tests for an adapter opened against a database it did not create.

Two adapters keep an in-memory map from a Partner's remote id to the container
it lives in -- NotebookLM's notebook id, Claude Science's project id. Both are
filled in exactly one place, `verify_partner_id_in_remote`, which runs only
during `create_partner`.

So the map is populated once, in the process that registered the Partner, and
is empty after any restart. Delivering to a Partner registered before that
restart raised a cache-miss error; the drain loop recorded it and retried
forever, the Caller was never told, and the working slot stayed held -- while
the mapping sat in the database the whole time, as
`partners.partner_id_in_remote` joined to `projects.project_system_id`.

The cache is now a cache: on a miss it asks the database, and only fails when
the answer genuinely is not there.
"""

from __future__ import annotations

import subprocess

import pytest

from adapters.claude_science.adapter import (
    ClaudeScienceExtension,
    ClaudeScienceProjectIdUnknown,
)
from adapters.notebooklm.adapter import NlmNotebookIdUnknown, NotebookLMExtension
from mcp_server.config import build_extension
from messaging_core.core import MessagingCore
from messaging_core.db import Database


@pytest.fixture
def db():
    database = Database(path=":memory:")
    yield database
    database.close()


def register(db: Database, *, source_prefix: str, system_id: str, remote_id: str) -> None:
    """Create a project and a partner directly, as an earlier process would have."""
    db.write(lambda conn: conn.execute(
        "INSERT INTO projects(source_prefix, project_system_id, title) VALUES (?, ?, ?)",
        (source_prefix, system_id, f"proj-{system_id}"),
    ))
    db.write(lambda conn: conn.execute(
        "INSERT INTO partners(uuid, project_id, title, partner_id_in_remote, descr) "
        "SELECT ?, id, ?, ?, 'd' FROM projects WHERE project_system_id = ?",
        (f"u-{remote_id}", f"partner-{remote_id}", remote_id, system_id),
    ))


# ---------------------------------------------------------------------------
# 1. NotebookLM
# ---------------------------------------------------------------------------


def test_a_fresh_notebooklm_adapter_recovers_the_notebook_id(db, monkeypatch):
    """This is the restart. The adapter has never seen this partner."""
    register(db, source_prefix="nlm_", system_id="notebook-42", remote_id="src-1")
    calls: list[list[str]] = []

    def fake_run(cmd, capture_output=True, text=True):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ext = build_extension("nlm_", db=db)
    ext.harvest_wait_seconds = 0
    assert ext._notebook_id_by_source == {}, "the cache must genuinely start empty"

    ext.deliver_message(partner_id_in_remote="src-1", behavior="[QUERY]", body="what is x?")

    assert any("notebook-42" in part for cmd in calls for part in cmd), (
        f"the query must be addressed to the notebook named in the database; ran: {calls}"
    )


def test_a_recovered_notebook_id_is_cached_rather_than_re_queried(db, monkeypatch):
    """It is still a cache -- the database is the fallback, not the lookup path."""
    register(db, source_prefix="nlm_", system_id="notebook-42", remote_id="src-1")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, returncode=0, stdout="", stderr=""))
    ext = build_extension("nlm_", db=db)
    ext.harvest_wait_seconds = 0

    ext.deliver_message(partner_id_in_remote="src-1", behavior="[QUERY]", body="q")

    assert ext._notebook_id_by_source.get("src-1") == "notebook-42"


def test_an_adapter_with_no_resolver_still_raises_on_a_miss(db, monkeypatch):
    """The failure has to stay reachable where it is genuinely unrecoverable."""
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, returncode=0, stdout="", stderr=""))
    ext = NotebookLMExtension()

    with pytest.raises(NlmNotebookIdUnknown):
        ext.deliver_message(partner_id_in_remote="never-registered", behavior="[QUERY]", body="q")


def test_a_partner_that_is_not_in_the_database_still_raises(db, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, returncode=0, stdout="", stderr=""))
    ext = build_extension("nlm_", db=db)

    with pytest.raises(NlmNotebookIdUnknown):
        ext.deliver_message(partner_id_in_remote="never-registered", behavior="[QUERY]", body="q")


def test_the_resolver_does_not_cross_sources(db, monkeypatch):
    """`partner_id_in_remote` is unique only WITHIN a project.

    An unfiltered lookup would happily hand a NotebookLM adapter a Claude
    Science project id that happened to share a remote id -- and the adapter
    would then address a query to a container that is not a notebook at all.
    """
    register(db, source_prefix="science_", system_id="science-proj", remote_id="collide")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, returncode=0, stdout="", stderr=""))
    ext = build_extension("nlm_", db=db)

    with pytest.raises(NlmNotebookIdUnknown):
        ext.deliver_message(partner_id_in_remote="collide", behavior="[QUERY]", body="q")


def test_a_resolver_that_raises_is_not_worse_than_no_resolver(db, monkeypatch):
    """A throwing resolver would turn a recoverable miss into the same
    unrecoverable failure this exists to fix, plus an unfamiliar exception."""
    def boom(_remote_id):
        raise RuntimeError("the database connection is closed")

    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, returncode=0, stdout="", stderr=""))
    ext = NotebookLMExtension(resolve_project_system_id=boom)

    with pytest.raises(NlmNotebookIdUnknown):
        ext.deliver_message(partner_id_in_remote="src-1", behavior="[QUERY]", body="q")


# ---------------------------------------------------------------------------
# 2. Claude Science
# ---------------------------------------------------------------------------


def test_a_fresh_claude_science_adapter_recovers_the_project_id(db, monkeypatch):
    register(db, source_prefix="science_", system_id="proj-7", remote_id="frame-1")
    seen: dict = {}

    def fake_submit(self, frame_id, text):
        seen["project_id"] = self._require_project_id(frame_id)
        return "req-1", {}

    monkeypatch.setattr(ClaudeScienceExtension, "_submit_into_frame", fake_submit)
    ext = build_extension("science_", db=db)
    assert ext._project_id_by_frame == {}

    ext.deliver_message(partner_id_in_remote="frame-1", behavior="[QUERY]", body="q")

    assert seen["project_id"] == "proj-7", (
        f"the frame's project must be recovered from the database; got {seen}"
    )


def test_claude_science_still_raises_when_the_frame_is_unknown(db):
    ext = build_extension("science_", db=db)

    with pytest.raises(ClaudeScienceProjectIdUnknown):
        ext._require_project_id("never-registered")
