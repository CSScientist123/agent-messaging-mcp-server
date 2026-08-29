"""Tests for `polling.server.PollingServer` against the current model: no state
machine, no `polling_tasks` table -- a task is queued, or it holds the
in-memory working slot, or it is neither.

Uses `Database(path=":memory:")` against the real schema (schema/schema.sql),
`messaging_core.core.MessagingCore` to build real projects/partners/handshakes
and to admit real messages, and `extension.base.StubExtension` (or a small
subclass of it) in place of any real remote.

A `[RESEARCH]`/`[QUERY]`/etc. round trip that completes normally makes
`PollingServer._complete` spawn a follow-up drain thread for the Caller it
just replied to (`_ensure_thread`), so that the Caller's own queue gets
drained without waiting for an unrelated push. Several tests below need to
inspect that Caller's queue/working-slot state deterministically right after
a single `drain_once` call, so they monkeypatch `PollingServer._ensure_thread`
to stop the cascade at exactly that point rather than racing a second, real
background thread. This is spelled out per-test; it is never used to fake the
behaviour actually under test.
"""

from __future__ import annotations

import threading

import pytest

from extension.base import StubExtension
from messaging_core.core import MessagingCore
from messaging_core.db import Database
from messaging_core.errors import NeedsRemote, Rejected
from polling.server import PollingServer

# ---------------------------------------------------------------------------
# fixtures and small helpers
# ---------------------------------------------------------------------------


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
    # Belt and braces: join every thread this test spawned so one test's
    # daemon threads never bleed CPU/log noise into the next one.
    srv.stop(timeout=5.0)


_counter = 0


def _unique(prefix: str) -> str:
    global _counter
    _counter += 1
    return f"{prefix}-{_counter}"


def make_pair(core: MessagingCore) -> tuple[dict, dict]:
    """A science_ project with two handshaken partners.

    `caller` holds project-orchestrator (so it may handshake a plain
    science_ partner and may send `[RESEARCH]` downward); `worker` holds no
    role. Each returned dict also carries `remote_id`, the
    `partner_id_in_remote` it was created with, since `create_partner`'s own
    return value does not include it.
    """
    project_id = core.create_project(
        title=_unique("proj"), source_prefix="science_", project_system_id=_unique("psid")
    )
    caller_remote = _unique("remote")
    worker_remote = _unique("remote")
    caller = core.create_partner(
        project_id=project_id, title=_unique("caller"), partner_id_in_remote=caller_remote, descr="d"
    )
    worker = core.create_partner(
        project_id=project_id, title=_unique("worker"), partner_id_in_remote=worker_remote, descr="d"
    )
    caller["remote_id"] = caller_remote
    worker["remote_id"] = worker_remote
    core.claim_orchestrator(
        requester_uuid=caller["uuid"], project_id=project_id, orchestrator_type="project-orchestrator"
    )
    core.handshake(requester_uuid=caller["uuid"], partner_title=worker["title"])
    return caller, worker


def queued_behaviors(db: Database, partner_id: int) -> list[str]:
    rows = db.read(
        "SELECT behavior FROM message_queue WHERE partner_id = ? ORDER BY id", (partner_id,)
    )
    return [r["behavior"] for r in rows]


def deliver_calls(stub: StubExtension) -> list[dict]:
    return [kwargs for name, kwargs in stub.calls if name == "deliver_message"]


def suppress_no_op(monkeypatch, server: PollingServer) -> None:
    """Stop `_complete`'s follow-up `_ensure_thread` call from spawning a real
    background thread for the Caller it just replied to.

    Used only in tests that drive `drain_once` directly (never through
    `notify_partner_push`/`start`), so the only thing this can possibly
    suppress is that one follow-up spawn -- never the drain pass under test.
    """
    monkeypatch.setattr(server, "_ensure_thread", lambda partner_id, label: None)


def suppress_for_partner(monkeypatch, server: PollingServer, blocked_partner_id: int) -> None:
    """Like `suppress_no_op`, but passes every other partner id through to the
    real implementation. Used when a real drain thread (spawned through
    `notify_partner_push`) must still work normally for some other partner.
    """
    original = server._ensure_thread

    def _patched(partner_id, label):
        if partner_id == blocked_partner_id:
            return None
        return original(partner_id, label)

    monkeypatch.setattr(server, "_ensure_thread", _patched)


# ---------------------------------------------------------------------------
# 1. Retirement deletes the drain_threads row.
# ---------------------------------------------------------------------------


def test_retirement_deletes_the_drain_threads_row(db, stub, core, server, monkeypatch):
    """A liveness rule, not bookkeeping: once a drain thread has nothing left
    to do, it must delete its own `drain_threads` row -- a surviving row with
    no live thread behind it is exactly what silently strands new work.

    The follow-up `_ensure_thread` call for the caller is suppressed (worker's
    own spawn/retirement is untouched -- see `suppress_for_partner`), purely
    to keep this test's only assertion about worker's own row from sharing a
    process with a second, unrelated drain thread. This test still exercises
    the real `notify_partner_push` -> real thread -> real retirement -> real
    DELETE path end to end.
    """
    caller, worker = make_pair(core)
    suppress_for_partner(monkeypatch, server, caller["id"])
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="what is x?", behavior="[QUERY]",
    )

    outcome = server.notify_partner_push(partner_uuid=worker["uuid"])
    assert outcome.startswith("[ok]"), f"expected an [ok] response spawning a thread, got: {outcome!r}"

    thread = server._drain_threads[worker["id"]]
    thread.join(timeout=5.0)
    assert not thread.is_alive(), "the drain thread for worker never retired within the timeout"

    row = db.read_one("SELECT 1 AS x FROM drain_threads WHERE partner_id = ?", (worker["id"],))
    assert row is None, (
        f"expected the drain_threads row for worker (id={worker['id']}) to be deleted on "
        f"retirement; found: {dict(row) if row else row}"
    )


# ---------------------------------------------------------------------------
# 2. stop() does NOT delete rows; start() afterwards respawns a thread.
# ---------------------------------------------------------------------------


def test_stop_preserves_row_and_start_respawns_a_thread(db, stub, core, server):
    caller, worker = make_pair(core)
    stub.completed = False  # the remote never finishes this turn on its own
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="a long task", behavior="[QUERY]",
    )
    server.notify_partner_push(partner_uuid=worker["uuid"])

    row_while_running = db.read_one(
        "SELECT thread_id FROM drain_threads WHERE partner_id = ?", (worker["id"],)
    )
    assert row_while_running is not None, "expected a drain_threads row while the thread is running"

    server.stop(timeout=5.0)

    row_after_stop = db.read_one(
        "SELECT thread_id FROM drain_threads WHERE partner_id = ?", (worker["id"],)
    )
    assert row_after_stop is not None, (
        "stop() must NOT delete the drain_threads row -- start() needs it to resume after a "
        f"restart; found row: {row_after_stop}"
    )

    server.start()
    respawned = server._drain_threads.get(worker["id"])
    assert respawned is not None and respawned.is_alive(), (
        "start() must respawn a live drain thread for every partner still registered in "
        f"drain_threads; got: {respawned}"
    )


# ---------------------------------------------------------------------------
# 3. notify_partner_push is idempotent.
# ---------------------------------------------------------------------------


def test_notify_partner_push_is_idempotent(db, stub, core, server):
    caller, worker = make_pair(core)
    stub.completed = False
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="work", behavior="[QUERY]",
    )

    first = server.notify_partner_push(partner_uuid=worker["uuid"])
    assert first.startswith("[ok]"), f"expected the first push to spawn a thread, got: {first!r}"
    spawned_thread = server._drain_threads[worker["id"]]

    second = server.notify_partner_push(partner_uuid=worker["uuid"])
    assert second.startswith("[nothing new]"), (
        f"a second push while a thread is alive must say nothing new, got: {second!r}"
    )
    assert server._drain_threads[worker["id"]] is spawned_thread, (
        "a second push while a thread is alive must not spawn a new one"
    )


# ---------------------------------------------------------------------------
# 4. notify_partner_push on an unknown uuid.
# ---------------------------------------------------------------------------


def test_notify_partner_push_unknown_uuid(server):
    with pytest.raises(Rejected) as exc_info:
        server.notify_partner_push(partner_uuid="does-not-exist")
    assert exc_info.value.code == "unknown_partner", (
        f"expected code 'unknown_partner', got {exc_info.value.code!r}"
    )
    assert server._drain_threads == {}, (
        f"nothing should be spawned for an unknown uuid, got: {server._drain_threads}"
    )


# ---------------------------------------------------------------------------
# 5. A [QUERY] round trip.
# ---------------------------------------------------------------------------


def test_query_round_trip(db, stub, core, server, monkeypatch):
    caller, worker = make_pair(core)
    suppress_no_op(monkeypatch, server)

    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="what is the answer", behavior="[QUERY]",
    )
    delivered = deliver_calls(stub)
    assert len(delivered) == 1 and delivered[0]["behavior"] == "[QUERY]", (
        f"expected exactly one [QUERY] delivery after send(), got: {delivered}"
    )

    idle = server.drain_once(partner_id=worker["id"])
    assert idle is False, "a pass that just completed a task must not report idle"

    assert core.working_task(partner_id=worker["id"]) is None, (
        "the working slot must be released once the QUERY completes"
    )
    behaviors = queued_behaviors(db, caller["id"])
    assert behaviors == ["[MESSAGE-RESPONSE]"], (
        f"expected exactly one [MESSAGE-RESPONSE] queued for the caller, got: {behaviors}"
    )


# ---------------------------------------------------------------------------
# 6. A [RESEARCH] round trip, with its summary phase.
# ---------------------------------------------------------------------------


def test_research_round_trip_runs_the_summary_phase_exactly_once(db, stub, core, server, monkeypatch):
    caller, worker = make_pair(core)
    suppress_no_op(monkeypatch, server)

    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="investigate x", behavior="[RESEARCH]",
    )
    delivered = deliver_calls(stub)
    assert len(delivered) == 1 and delivered[0]["behavior"] == "[RESEARCH]", (
        f"expected the RESEARCH dispatch to be delivered once, got: {delivered}"
    )

    # Pass 1: the remote finishes the WORK. This must trigger the summary
    # phase -- a second delivery to the SAME remote -- rather than a reply.
    idle_1 = server.drain_once(partner_id=worker["id"])
    assert idle_1 is False

    delivered = deliver_calls(stub)
    assert len(delivered) == 2, (
        f"expected a second delivery (the summary request) after the work finished, got: {delivered}"
    )
    assert delivered[1]["behavior"] == "[TRUTHFUL-REPORT]", (
        f"the summary request must carry [TRUTHFUL-REPORT], got: {delivered[1]}"
    )
    assert delivered[1]["partner_id_in_remote"] == worker["remote_id"], (
        "the summary request must be delivered to the SAME remote that did the work: "
        f"expected {worker['remote_id']!r}, got {delivered[1]['partner_id_in_remote']!r}"
    )

    working = core.working_task(partner_id=worker["id"])
    assert working is not None and working["behavior"] == "[TRUTHFUL-REPORT]", (
        f"the working slot must still hold the (now-promoted) task, got: {working}"
    )
    assert queued_behaviors(db, caller["id"]) == [], (
        "nothing may reach the caller until the summary phase ALSO finishes; found: "
        f"{queued_behaviors(db, caller['id'])}"
    )

    # Pass 2: the remote finishes the SUMMARY. Only now does the caller
    # receive anything, and the summary phase must not be entered again.
    idle_2 = server.drain_once(partner_id=worker["id"])
    assert idle_2 is False

    delivered = deliver_calls(stub)
    assert len(delivered) == 2, (
        "entering the summary phase must happen exactly once, not on every pass; deliver_message "
        f"calls: {delivered}"
    )
    assert core.working_task(partner_id=worker["id"]) is None, (
        "the working slot must be released once the summary itself completes"
    )
    behaviors = queued_behaviors(db, caller["id"])
    assert behaviors == ["[TRUTHFUL-REPORT]"], (
        f"expected exactly one [TRUTHFUL-REPORT] queued for the caller, got: {behaviors}"
    )

    # Pass 3: nothing left to do for worker at all.
    idle_3 = server.drain_once(partner_id=worker["id"])
    assert idle_3 is True, "worker must be fully idle once the round trip has finished"
    assert len(deliver_calls(stub)) == 2, "an idle pass must not deliver anything new"


# ---------------------------------------------------------------------------
# 7. Termination: [MESSAGE-RESPONSE] and a delivered [TRUTHFUL-REPORT] must
#    each send nothing back.
#
#    [ERROR] is deliberately NOT in this set. It is a question -- "this is what
#    stopped me" -- and is answered with a [MESSAGE-RESPONSE], which is itself
#    the label that replies with nothing. The exchange still terminates, one
#    hop later.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("behavior", ["[MESSAGE-RESPONSE]"])
def test_termination_produces_nothing_back(db, stub, core, server, behavior):
    """The single most important property in this file: a completed task whose
    label's `reply_behavior` is NULL must push nothing back to the caller, or
    two agents would reply to each other forever."""
    caller, worker = make_pair(core)
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="status", behavior=behavior,
    )

    idle = server.drain_once(partner_id=worker["id"])
    assert idle is False

    assert core.working_task(partner_id=worker["id"]) is None, (
        "the working slot must still be released even though nothing is owed back"
    )
    behaviors = queued_behaviors(db, caller["id"])
    assert behaviors == [], (
        f"a completed {behavior} must send NOTHING back to the caller (reply_behavior is NULL "
        f"in label_caps); found queued for caller: {behaviors}"
    )


def test_termination_delivered_truthful_report_produces_nothing_back(db, stub, core, server):
    """A [TRUTHFUL-REPORT] sent directly (not as the second phase of a
    [RESEARCH] round trip -- see the previous test) must terminate exactly
    like [ERROR] and [MESSAGE-RESPONSE]. `_complete` distinguishes a promoted
    research summary from a directly-sent [TRUTHFUL-REPORT] by an explicit
    `summary_phase` marker `begin_summary_phase` sets on the working-slot
    dict, not by the label alone -- two directly-sendable behaviors carrying
    the same label must not be treated as if one were secretly the other."""
    caller, worker = make_pair(core)

    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="status", behavior="[TRUTHFUL-REPORT]",
    )

    idle = server.drain_once(partner_id=worker["id"])
    assert idle is False

    assert core.working_task(partner_id=worker["id"]) is None
    behaviors = queued_behaviors(db, caller["id"])
    assert behaviors == [], (
        "a completed [TRUTHFUL-REPORT] must send NOTHING back to the caller (reply_behavior is "
        f"NULL in label_caps); found queued for caller: {behaviors}"
    )


# ---------------------------------------------------------------------------
# 8. An agent waiting on its own question.
# ---------------------------------------------------------------------------



def poll_completion(self, *, partner_id_in_remote: str) -> bool:
        self._record("poll_completion", partner_id_in_remote=partner_id_in_remote)
        raise NeedsRemote("poll_completion", "this stub never supports polling for completion.")


class _NoCompletionCheckExtension(StubExtension):
    """A remote with no notion of "is my current turn done?" at all."""

    def poll_completion(self, *, partner_id_in_remote: str) -> bool:
        self._record("poll_completion", partner_id_in_remote=partner_id_in_remote)
        raise NeedsRemote("poll_completion", "this stub never supports polling for completion.")


def test_needs_remote_from_poll_completion_is_treated_as_finished(db, monkeypatch):
    ext = _NoCompletionCheckExtension(source_prefix="science_")
    core = MessagingCore(db, extension=ext)
    srv = PollingServer(db, extensions={"science_": ext}, poll_interval=0.01, core=core)
    try:
        caller, worker = make_pair(core)
        suppress_no_op(monkeypatch, srv)

        core.send(
            requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
            message="q", behavior="[QUERY]",
        )

        idle = srv.drain_once(partner_id=worker["id"])
        assert idle is False

        assert core.working_task(partner_id=worker["id"]) is None, (
            "a remote that cannot be polled for completion must be treated as finished, not "
            "left occupying the working slot forever"
        )
        behaviors = queued_behaviors(db, caller["id"])
        assert behaviors == ["[MESSAGE-RESPONSE]"], (
            f"expected the QUERY to be answered anyway, got: {behaviors}"
        )
    finally:
        srv.stop(timeout=5.0)


# ---------------------------------------------------------------------------
# 10. A drain loop survives an exception from the extension.
# ---------------------------------------------------------------------------


class _FlakyOnceExtension(StubExtension):
    """Raises a plain RuntimeError (not NeedsRemote) from poll_completion on
    its first call only, then behaves normally -- simulates a bug or a
    transient failure in a real extension that the drain loop must survive
    rather than die from."""

    def __init__(self, *, source_prefix: str = "science_") -> None:
        super().__init__(source_prefix=source_prefix)
        self._poll_calls = 0

    def poll_completion(self, *, partner_id_in_remote: str) -> bool:
        self._poll_calls += 1
        if self._poll_calls == 1:
            self._record("poll_completion", partner_id_in_remote=partner_id_in_remote)
            raise RuntimeError("boom: simulated extension failure")
        return super().poll_completion(partner_id_in_remote=partner_id_in_remote)


def test_drain_loop_survives_an_extension_exception(db, monkeypatch):
    ext = _FlakyOnceExtension()
    core = MessagingCore(db, extension=ext)
    srv = PollingServer(db, extensions={"science_": ext}, poll_interval=0.01, core=core)
    try:
        caller, worker = make_pair(core)
        suppress_for_partner(monkeypatch, srv, caller["id"])

        core.send(
            requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
            message="q", behavior="[QUERY]",
        )

        outcome = srv.notify_partner_push(partner_uuid=worker["uuid"])
        assert outcome.startswith("[ok]"), f"expected the push to spawn a thread, got: {outcome!r}"
        thread = srv._drain_threads[worker["id"]]

        # The thread survives the exception on its first pass and, once the
        # flaky extension stops failing, finishes normally and retires --
        # bounded by a real timeout so a genuine hang fails fast instead of
        # wedging the test suite.
        thread.join(timeout=5.0)
        assert not thread.is_alive(), (
            "the drain thread for worker never retired -- an exception from the extension must "
            "not kill the daemon thread, but the loop must keep making progress afterwards"
        )
        assert len(srv.last_errors) == 1 and isinstance(srv.last_errors[0], RuntimeError), (
            f"expected exactly one recorded RuntimeError on last_errors, got: {srv.last_errors}"
        )
    finally:
        srv.stop(timeout=5.0)


# ---------------------------------------------------------------------------
# 11. stop() and start() are each safe to call twice.
# ---------------------------------------------------------------------------


def test_stop_twice_is_safe(db, stub, core, server):
    caller, worker = make_pair(core)
    stub.completed = False
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="q", behavior="[QUERY]",
    )
    server.notify_partner_push(partner_uuid=worker["uuid"])

    server.stop(timeout=5.0)
    server.stop(timeout=5.0)  # must not raise the second time


def test_start_twice_is_safe(db, stub, core, server):
    caller, worker = make_pair(core)
    stub.completed = False
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="q", behavior="[QUERY]",
    )
    # Simulate a restart: a drain_threads row survives from "a previous
    # process" with no in-memory thread behind it yet.
    db.write(
        lambda conn: conn.execute(
            "INSERT INTO drain_threads(partner_id, thread_id) VALUES (?, 'stale-from-a-previous-process')",
            (worker["id"],),
        )
    )

    server.start()
    first_thread = server._drain_threads.get(worker["id"])
    assert first_thread is not None and first_thread.is_alive(), (
        "start() must respawn a live thread for a partner left in drain_threads"
    )

    server.start()  # must not raise the second time
    second_thread = server._drain_threads.get(worker["id"])
    assert second_thread is first_thread, (
        "a second start() must not replace or duplicate an already-running thread"
    )


# ---------------------------------------------------------------------------
# 12. No source prefix registered for a partner.
# ---------------------------------------------------------------------------


def test_drain_once_with_no_registered_extension_loses_nothing(db, stub, core):
    caller, worker = make_pair(core)
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="first", behavior="[MESSAGE-RESPONSE]",
    )
    # A second message of the same label ties with the one already occupying
    # the working slot, so it stays queued rather than being delivered --
    # exactly the "still waiting" state a no_extension failure must not lose.
    #
    # [MESSAGE-RESPONSE] rather than [QUERY], because sending a [QUERY] would
    # stop the CALLER: an agent that asks a blocking question is interrupted
    # and cannot ask a second one until it is answered.
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="second", behavior="[MESSAGE-RESPONSE]",
    )

    before_queue = db.read(
        "SELECT id, behavior, body FROM message_queue WHERE partner_id = ?", (worker["id"],)
    )
    assert len(before_queue) == 1, (
        f"setup failed: expected exactly one message still queued behind the working task, "
        f"got: {[dict(r) for r in before_queue]}"
    )
    before_working = core.working_task(partner_id=worker["id"])
    assert before_working is not None and before_working["body"] == "first"

    srv = PollingServer(db, extensions={}, poll_interval=0.01, core=core)
    with pytest.raises(Rejected) as exc_info:
        srv.drain_once(partner_id=worker["id"])
    assert exc_info.value.code == "no_extension", (
        f"expected code 'no_extension', got {exc_info.value.code!r}"
    )

    after_queue = db.read(
        "SELECT id, behavior, body FROM message_queue WHERE partner_id = ?", (worker["id"],)
    )
    assert [dict(r) for r in after_queue] == [dict(r) for r in before_queue], (
        "the queued message must survive a no_extension failure completely untouched: "
        f"before={[dict(r) for r in before_queue]!r} after={[dict(r) for r in after_queue]!r}"
    )
    after_working = core.working_task(partner_id=worker["id"])
    assert after_working == before_working, (
        f"the working slot must be untouched by a no_extension failure: "
        f"before={before_working!r} after={after_working!r}"
    )


# ---------------------------------------------------------------------------
# Bonus: cross-label ordering through the public surface. Not one of the 12
# requirements, but `drain_once` relies entirely on `MessagingCore.advance`
# to pick the right queue head, and nothing else in this file exercises two
# DIFFERENT labels competing for the same working slot at once -- every
# other test here only ever has one label queued at a time.
# ---------------------------------------------------------------------------


def test_lower_priority_number_wins_across_different_labels(db, stub, core):
    caller, worker = make_pair(core)

    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="the summary", behavior="[TRUTHFUL-REPORT]",
    )
    working = core.working_task(partner_id=worker["id"])
    assert working is not None and working["behavior"] == "[TRUTHFUL-REPORT]", (
        f"setup failed: expected [TRUTHFUL-REPORT] to occupy the empty working slot, "
        f"got: {working}"
    )

    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="something went wrong", behavior="[ERROR]",
    )
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="an answer", behavior="[MESSAGE-RESPONSE]",
    )
    queued = queued_behaviors(db, worker["id"])
    assert set(queued) == {"[ERROR]", "[MESSAGE-RESPONSE]"}, (
        f"setup failed: expected both to be genuinely queued (neither beats priority 1 "
        f"held by [TRUTHFUL-REPORT]), got: {queued}"
    )

    core.release(partner_id=worker["id"])  # the remote finished its turn
    # Otherwise drain_once would promote [ERROR] and then immediately see it
    # as complete (stub.completed defaults to True) and release it again in
    # the very same pass, before this test ever gets to look at what was
    # promoted.
    stub.completed = False

    srv = PollingServer(db, extensions={"science_": stub}, poll_interval=0.01, core=core)
    srv.drain_once(partner_id=worker["id"])

    promoted = core.working_task(partner_id=worker["id"])
    assert promoted is not None and promoted["behavior"] == "[MESSAGE-RESPONSE]", (
        "[MESSAGE-RESPONSE] (priority 2) must be promoted ahead of [ERROR] (priority 3) -- a "
        f"lower priority number is supposed to win; got: {promoted}"
    )


# ---------------------------------------------------------------------------
# 17. a push landing in the retirement window is not lost
# ---------------------------------------------------------------------------


def test_a_push_during_the_retirement_window_is_not_stranded(db, stub, core, server):
    """A message admitted while a drain thread is retiring must still be picked up.

    This is a liveness race, not a tidiness one, and it was reproduced before it
    was fixed. The window is between `drain_once` reporting "nothing left" and
    the thread actually exiting:

        thread: drain_once() -> True .......................... exits, deletes row
        client:                       send() ; notify_partner_push()
                                      -> sees the thread still ALIVE
                                      -> "[nothing new]", spawns nothing

    The message ends up queued with no thread and no `drain_threads` row, and
    nothing ever picks it up — the exact failure the row exists to prevent,
    arriving by the other door.

    The window is real but tiny, so it is forced here rather than waited for: a
    wrapper around `drain_once` blocks the thread at the moment it decides to
    retire. A test that hoped to hit this by timing would pass on a fast machine
    and stay silent about the bug.

    The fix is that retirement re-checks for work while holding the same lock
    `notify_partner_push` decides under, which makes the two mutually exclusive.
    """
    caller, worker = make_pair(core)
    worker_id = worker["id"]

    decided_to_retire = threading.Event()
    may_finish = threading.Event()
    real_drain_once = server.drain_once

    def drain_once_then_block(*, partner_id):
        idle = real_drain_once(partner_id=partner_id)
        if idle and partner_id == worker_id and not decided_to_retire.is_set():
            decided_to_retire.set()
            may_finish.wait(5)
        return idle

    server.drain_once = drain_once_then_block

    # A thread starts on an empty queue, so its very first pass decides to retire.
    server.notify_partner_push(partner_uuid=worker["uuid"])
    assert decided_to_retire.wait(5), "the drain thread never reached its retirement decision"

    # The message lands while that thread is committed to retiring but still alive.
    core.send(
        requester_uuid=caller["uuid"],
        queried_partner_title=worker["title"],
        message="landed in the window",
        behavior="[QUERY]",
    )
    core.release(partner_id=worker_id)
    db.write(
        lambda conn: conn.execute(
            "INSERT INTO message_queue(partner_id, caller_id, behavior, body) "
            "VALUES (?, ?, '[ERROR]', 'queued during retirement')",
            (worker_id, caller["id"]),
        )
    )
    server.notify_partner_push(partner_uuid=worker["uuid"])

    may_finish.set()

    drained = threading.Event()
    for _ in range(200):
        remaining = db.read_one(
            "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id = ?", (worker_id,)
        )["n"]
        if remaining == 0:
            drained.set()
            break
        threading.Event().wait(0.02)

    remaining = db.read_one(
        "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id = ?", (worker_id,)
    )["n"]
    row = db.read_one("SELECT * FROM drain_threads WHERE partner_id = ?", (worker_id,))
    assert drained.is_set(), (
        f"a message admitted during the retirement window was never picked up: "
        f"{remaining} row(s) still queued for partner {worker_id}, "
        f"drain_threads row {'present' if row else 'gone'}. "
        "Retirement must re-check for work under the same lock notify_partner_push uses."
    )
    assert deliver_calls(stub), "nothing was ever delivered to the remote"


# ---------------------------------------------------------------------------
# 18. a Partner stopped on a permission prompt reaches its Caller
# ---------------------------------------------------------------------------


def test_an_approval_prompt_is_reported_to_the_caller_not_polled_forever(db, core, server):
    """A Partner blocked on an approval is neither busy nor finished.

    It is waiting for something no amount of polling will produce. Nobody else
    can report it either: the Partner cannot, because an agent stopped on a
    prompt is not running, and the Caller has no reason to look. So the Polling
    Server reports it on the Partner's behalf.

    Left unreported the failure is total and silent — the slot is held, the
    drain thread never retires because `drain_once` never returns True, and the
    only trace is an entry appended to `last_errors` once per poll interval.
    """
    caller, worker = make_pair(core)

    class Blocked(StubExtension):
        def poll_completion(self, *, partner_id_in_remote):
            raise Rejected(
                "approval_is_an_error",
                "conversation is blocked on an approval/permission prompt: "
                "wants write access to /mnt/c/Data/out.md",
            )

    blocked = Blocked(source_prefix="science_")
    server.extensions["science_"] = blocked
    core.extension = blocked

    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="do the work", behavior="[RESEARCH]")
    assert core.working_task(partner_id=worker["id"]) is not None, "the task never started"

    idle = server.drain_once(partner_id=worker["id"])

    assert idle is False, "an approval must not be reported as an idle partner"
    assert core.working_task(partner_id=worker["id"]) is None, (
        "the working slot is still held; the drain thread would spin on it forever"
    )

    landed = db.read(
        "SELECT behavior, body FROM message_queue WHERE partner_id = ?", (caller["id"],)
    )
    assert [r["behavior"] for r in landed] == ["[ERROR]"], (
        f"the Caller should have been sent exactly one [ERROR]; got "
        f"{[r['behavior'] for r in landed]}"
    )
    body = landed[0]["body"]
    for expected in ("get_permissions", "add_permissions", "/mnt/c/Data/out.md",
                     worker["remote_id"]):
        assert expected in body, (
            f"the [ERROR] must name {expected!r} so the Caller can act on it; body was:\n{body}"
        )
    assert "answers one" in body, "the message must say an approval is not a question to answer"


def test_a_stopped_partner_is_not_left_running_on_the_prompt(db, core, server):
    """The remote is stopped as well as reported — and a remote that cannot be
    stopped does not turn the report into a failure.

    The report is the point; stopping is hygiene. A remote like Claude Science
    refuses cancellation outright, and that refusal must not cost the Caller its
    error message.
    """
    caller, worker = make_pair(core)

    class BlockedUncancellable(StubExtension):
        def poll_completion(self, *, partner_id_in_remote):
            raise Rejected("approval_is_an_error", "blocked: needs read on /etc")

        def stop_remote_execution(self, *, partner_id_in_remote, reason):
            raise Rejected("no_remote_cancel", "this remote cannot be cancelled")

    ext = BlockedUncancellable(source_prefix="science_")
    server.extensions["science_"] = ext
    core.extension = ext

    core.send(requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
              message="work", behavior="[RESEARCH]")
    server.drain_once(partner_id=worker["id"])

    landed = db.read("SELECT behavior FROM message_queue WHERE partner_id = ?", (caller["id"],))
    assert [r["behavior"] for r in landed] == ["[ERROR]"], (
        "a remote that cannot be cancelled must still get its error reported"
    )
