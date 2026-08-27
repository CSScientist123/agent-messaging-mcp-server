"""Tests for being able to tell what this system did.

Three error sinks existed and nothing read any of them: `PollingServer`
swallowed exceptions into `last_errors` to keep a daemon thread alive,
`MessagingCore` recorded `uncancelled_displacements` when a remote refused to
be stopped, and the Antigravity adapter collected `close_errors` from a
`finally`. All three were write-only, so the information they hold reached
nobody -- and a drain thread failing every pass looked exactly like one with
nothing to do.

There was also no logging anywhere in the project, and the one measurement the
schema is deliberately shaped to allow -- `enqueued_at` on the queue row,
`started_at` on the working slot, so how long a task waited can be recovered
after the row is gone -- was never actually subtracted.
"""

from __future__ import annotations

import logging

import pytest

from extension.base import StubExtension
from messaging_core.core import MessagingCore
from messaging_core.db import Database
from messaging_core.errors import Rejected
from polling.server import PollingServer

from tests.test_polling_working_slot import make_pair


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


# ---------------------------------------------------------------------------
# 1. The error sinks are readable.
# ---------------------------------------------------------------------------


def test_diagnostics_surfaces_what_a_drain_loop_swallowed(db, core, server):
    """A thread failing every pass must not look like one with nothing to do."""
    server._record_error(RuntimeError("the remote refused the connection"))

    report = server.diagnostics()

    assert report["last_errors"], f"the swallowed error is not reported: {report}"
    assert any("refused the connection" in str(e) for e in report["last_errors"]), (
        f"the report must carry what actually failed: {report['last_errors']}"
    )
    assert report["last_error_count"] == 1


def test_diagnostics_surfaces_a_remote_that_could_not_be_cancelled(db, core, server):
    """A displacement against an uncancellable remote leaves two turns running.

    That is the honest behaviour for a remote with no interrupt, and it is
    exactly the kind of thing an operator has to be able to find out about.
    """
    core.uncancelled_displacements.append((7, "[RESEARCH]", "[QUERY]"))

    report = server.diagnostics()

    assert report["uncancelled_displacements"], (
        f"an uncancelled displacement is not reported: {report}"
    )


def test_diagnostics_is_empty_and_honest_on_a_healthy_server(db, core, server):
    report = server.diagnostics()

    assert report["last_errors"] == []
    assert report["last_error_count"] == 0
    assert report["uncancelled_displacements"] == []


def test_diagnostics_never_raises_on_an_extension_without_close_errors(db, core, server):
    """`close_errors` is an Antigravity detail, not part of the extension contract."""
    report = server.diagnostics()
    assert "extension_errors" in report


# ---------------------------------------------------------------------------
# 2. How long a task waited is actually computed.
# ---------------------------------------------------------------------------


def test_status_reports_how_long_the_working_task_waited(db, core, stub):
    """`enqueued_at` and `started_at` are recorded in the same UTC shape on purpose.

    A promoted row is DELETEd, so the wait can only be measured by carrying
    `enqueued_at` onto the slot -- which the code already does, and then never
    subtracted.
    """
    caller, worker = make_pair(core)
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="what is x?", behavior="[QUERY]",
    )

    report = core.status(requester_uuid=worker["uuid"])

    working = report["working"]
    assert working is not None
    assert "waited_ms" in working, f"the wait is never computed: {working}"
    assert isinstance(working["waited_ms"], (int, float))
    assert working["waited_ms"] >= 0, (
        f"a negative wait means the two timestamps are not the same clock: {working}"
    )


def test_status_reports_no_wait_when_nothing_is_working(db, core, stub):
    caller, worker = make_pair(core)

    report = core.status(requester_uuid=worker["uuid"])

    assert report["working"] is None


# ---------------------------------------------------------------------------
# 3. Logging exists at all.
# ---------------------------------------------------------------------------


def test_admitting_a_message_is_logged(db, core, stub, caplog):
    """Nothing in this project logged anything, so a production incident had
    only the database to reconstruct from."""
    caller, worker = make_pair(core)

    with caplog.at_level(logging.INFO, logger="messaging_core"):
        core.send(
            requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
            message="what is x?", behavior="[QUERY]",
        )

    assert caplog.records, "send emitted no log record at all"
    assert any("[QUERY]" in r.getMessage() for r in caplog.records), (
        f"the record must name the label: {[r.getMessage() for r in caplog.records]}"
    )


def test_a_swallowed_drain_error_is_logged_as_a_warning(db, core, server, caplog):
    """Swallowing an exception to keep a daemon alive is right; swallowing it
    silently is what made a permanently failing thread invisible."""
    with caplog.at_level(logging.WARNING, logger="polling"):
        server._record_error(RuntimeError("the remote refused the connection"))

    assert any("refused the connection" in r.getMessage() for r in caplog.records), (
        f"the swallowed error was never logged: {[r.getMessage() for r in caplog.records]}"
    )


def test_a_refusal_is_not_logged_as_an_error(db, core, stub, caplog):
    """A `Rejected` is a rule working, not a fault. Logging it at ERROR would
    fill an operator's log with correct behaviour."""
    caller, worker = make_pair(core)

    with caplog.at_level(logging.DEBUG, logger="messaging_core"):
        with pytest.raises(Rejected):
            core.send(
                requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
                message="x", behavior="[NOT-A-LABEL]",
            )

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "a business-rule refusal was logged at ERROR or worse: "
        f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )
