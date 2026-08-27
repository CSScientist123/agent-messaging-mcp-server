"""Tests for which Partners a `PollingServer` will and will not drain.

Every MCP process registers exactly ONE extension -- its own source -- and the
three processes coordinate through one shared SQLite file. That single fact
decides everything here:

- A Partner whose source this process holds no extension for must never get a
  drain thread. Not on completion, not on a push, not on a restart. The thread
  would raise `no_extension` on every pass, never retire, and leave a
  `drain_threads` row that re-arms it at the next `start()`.
- A `code_` Partner must never get one in ANY process, because there is no
  `code_` adapter anywhere: a Claude Code session receives work through its own
  built-in channel mechanism, and its stored messages simply wait to be `read`.
- Something must still notice a message that one process admitted for a Partner
  another process owns. That is the supervisor: it scans for queued work in its
  OWN sources and arms the threads for it.

Uses `Database(path=":memory:")` against the real schema and `StubExtension` in
place of any real remote, like the rest of this suite.
"""

from __future__ import annotations

import asyncio

import pytest

from extension.base import StubExtension
from mcp_server.server import build_server
from messaging_core.core import MessagingCore
from messaging_core.db import Database
from messaging_core.errors import NeedsRemote
from polling.server import PollingServer


# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    database = Database(path=":memory:")
    yield database
    database.close()


@pytest.fixture
def science():
    return StubExtension(source_prefix="science_")


@pytest.fixture
def core(db, science):
    return MessagingCore(db, extension=science)


@pytest.fixture
def server(db, science, core):
    """A server that speaks for science_ and nothing else -- i.e. one real process."""
    srv = PollingServer(
        db, extensions={"science_": science}, poll_interval=0.01,
        supervisor_interval=0.01, core=core,
    )
    yield srv
    srv.stop(timeout=5.0)


_counter = 0


def _unique(prefix: str) -> str:
    global _counter
    _counter += 1
    return f"tl-{prefix}-{_counter}"


def make_orchestrator(core: MessagingCore, role: str = "project-orchestrator") -> dict:
    """A science_ partner holding `role`, in a project of its own.

    Which role matters: `handshake` requires a gemini-orchestrator to reach a
    gemini_ partner and a bridge-scientist to reach a code_ one, and a partner
    holds exactly one role -- so each cross-source pair needs its own caller.
    """
    core.extension = StubExtension(source_prefix="science_")
    project_id = core.create_project(
        title=_unique("proj"), source_prefix="science_", project_system_id=_unique("psid")
    )
    caller = core.create_partner(
        project_id=project_id, title=_unique("caller"),
        partner_id_in_remote=_unique("remote"), descr="d",
    )
    core.claim_orchestrator(
        requester_uuid=caller["uuid"], project_id=project_id, orchestrator_type=role
    )
    caller["project_id"] = project_id
    return caller


def make_gemini_orchestrator(core: MessagingCore, project_caller: dict) -> dict:
    """A gemini-orchestrator in the same project, holding a budget grant.

    Reaching a gemini_ partner takes both: only a gemini-orchestrator may
    handshake one, and only after its own project-orchestrator has granted it
    budget. Both live in the same project, which is why this takes one.
    """
    core.extension = StubExtension(source_prefix="science_")
    gemini_caller = core.create_partner(
        project_id=project_caller["project_id"], title=_unique("gemini-caller"),
        partner_id_in_remote=_unique("remote"), descr="d",
    )
    core.claim_orchestrator(
        requester_uuid=gemini_caller["uuid"], project_id=project_caller["project_id"],
        orchestrator_type="gemini-orchestrator",
    )
    core.grant_gemini_budget(
        requester_uuid=project_caller["uuid"], grantee_uuid=gemini_caller["uuid"],
        budget_count=2,
    )
    gemini_caller["project_id"] = project_caller["project_id"]
    return gemini_caller


def make_worker(core: MessagingCore, caller: dict, source_prefix: str) -> dict:
    """A worker of `source_prefix`, handshaken from `caller`.

    A same-source worker joins the caller's own project; a cross-source one
    needs a project of its own, which is the shape the system is built around.
    """
    core.extension = StubExtension(source_prefix=source_prefix)
    if source_prefix == "science_":
        project_id = caller["project_id"]
    else:
        project_id = core.create_project(
            title=_unique("proj"), source_prefix=source_prefix,
            project_system_id=_unique("psid"),
        )
    worker = core.create_partner(
        project_id=project_id, title=_unique("worker"),
        partner_id_in_remote=_unique("remote"), descr="d",
    )
    core.extension = StubExtension(source_prefix="science_")
    core.handshake(requester_uuid=caller["uuid"], partner_title=worker["title"])
    return worker


def queue_one(core: MessagingCore, caller: dict, worker: dict, source_prefix: str) -> None:
    """Admit a [QUERY] for `worker`, tolerating a delivery this process cannot make.

    Admission is local and always succeeds; delivery needs the target's own
    extension, and that is exactly the case these tests are about.
    """
    core.extension = StubExtension(source_prefix=source_prefix)
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="what is x?", behavior="[QUERY]",
    )
    core.extension = StubExtension(source_prefix="science_")


def queued_behaviors(db: Database, partner_id: int) -> list[str]:
    rows = db.read(
        "SELECT behavior FROM message_queue WHERE partner_id = ? ORDER BY id", (partner_id,)
    )
    return [r["behavior"] for r in rows]


def has_row(db: Database, partner_id: int) -> bool:
    return db.read_one(
        "SELECT 1 AS x FROM drain_threads WHERE partner_id = ?", (partner_id,)
    ) is not None


# ---------------------------------------------------------------------------
# 1. A source this process cannot serve is never drained by it.
# ---------------------------------------------------------------------------


def test_a_partner_this_process_cannot_serve_never_gets_a_thread(db, core, server):
    """The failure this prevents is a thread that can never succeed and never stops.

    `_complete` arms a thread for the CALLER it just replied to, and a Caller
    is routinely of a different source than the Partner that answered it. With
    no guard, a science_ process spawns a gemini_ drain thread that raises
    `no_extension` on every single pass, never retires, and writes a
    `drain_threads` row that survives the process and re-arms the same doomed
    thread at the next start().
    """
    caller = make_gemini_orchestrator(core, make_orchestrator(core))
    worker = make_worker(core, caller, "gemini_")

    outcome = server.ensure_partner_thread(partner_id=worker["id"])

    assert "[nothing new]" in outcome, (
        "a process holding no gemini_ extension must decline to drain a gemini_ partner, "
        f"got: {outcome!r}"
    )
    assert worker["id"] not in server._drain_threads, (
        "no thread should have been spawned for a partner this process cannot serve"
    )
    assert not has_row(db, worker["id"]), (
        "no drain_threads row should have been written: the row is what re-arms the "
        "thread after a restart, so writing one here makes the bug permanent"
    )


def test_a_code_partner_never_gets_a_drain_thread(db, core, server):
    """There is no code_ adapter in any process, by design.

    A Claude Code session receives work through its own built-in channel
    mechanism, not through this system; its stored messages simply wait to be
    `read`. A drain thread for one would have nothing to poll and nothing to
    stop -- so the guard has to hold even in a process that WAS configured
    with a code_ extension, which is why this asserts on a server that has one.
    """
    # A code_ partner may only be handshaken by a bridge-scientist -- that
    # pairing is the whole reason the role exists.
    bridge = make_orchestrator(core, "bridge-scientist")
    code_partner = make_worker(core, bridge, "code_")

    outcome = server.ensure_partner_thread(partner_id=code_partner["id"])

    assert "[nothing new]" in outcome, f"a code_ partner must never be drained, got: {outcome!r}"
    assert code_partner["id"] not in server._drain_threads
    assert not has_row(db, code_partner["id"])


def test_a_code_partner_is_not_drained_even_where_the_queue_has_work(db, core, server):
    """The supervisor must not reintroduce what the guard rules out.

    A code_ partner's queue fills up exactly like anyone else's -- the messages
    are stored for it to `read` -- so a supervisor scanning for "queued work"
    would arm a thread for one unless code_ is absent from its extension map.
    """
    bridge = make_orchestrator(core, "bridge-scientist")
    code_partner = make_worker(core, bridge, "code_")
    queue_one(core, bridge, code_partner, "code_")

    assert server.scan_once() == 0
    assert code_partner["id"] not in server._drain_threads


# ---------------------------------------------------------------------------
# 2. start() resumes only what this process owns -- and leaves the rest alone.
# ---------------------------------------------------------------------------


def test_start_resumes_its_own_partners_and_does_not_delete_another_process_s_rows(
    db, core, server
):
    """A restart must not become the thing that strands another process's work.

    Both halves matter. Spawning for a foreign source recreates the doomed
    thread; DELETING the foreign row would remove the one record the process
    that CAN serve that partner uses to bring its own thread back.
    """
    caller = make_orchestrator(core)
    mine = make_worker(core, caller, "science_")
    gemini_caller = make_gemini_orchestrator(core, caller)
    theirs = make_worker(core, gemini_caller, "gemini_")
    queue_one(core, caller, mine, "science_")
    queue_one(core, gemini_caller, theirs, "gemini_")

    for partner_id in (mine["id"], theirs["id"]):
        db.write(
            lambda conn, pid=partner_id: conn.execute(
                "INSERT INTO drain_threads(partner_id, thread_id) VALUES (?, ?)",
                (pid, f"drain-{pid}"),
            )
        )

    server.start()

    assert mine["id"] in server._drain_threads, "this process's own partner should be resumed"
    assert theirs["id"] not in server._drain_threads, (
        "a partner of a source this process holds no extension for must not be resumed"
    )
    assert has_row(db, theirs["id"]), (
        "the foreign drain_threads row must survive: it is what the owning process's own "
        "start() reads to bring that partner's thread back"
    )


# ---------------------------------------------------------------------------
# 3. The supervisor is what makes a cross-process message arrive at all.
# ---------------------------------------------------------------------------


def test_the_supervisor_arms_a_thread_for_a_row_left_queued(db, core, server):
    """Nothing else closes this gap.

    A message admitted while nothing could deliver it -- another process's
    send, a delivery that failed, a restart mid-flight -- is committed to a
    table this process can see, against a remote only this process can poll.
    The supervisor is that process noticing.
    """
    caller = make_orchestrator(core)
    worker = make_worker(core, caller, "science_")
    # Admitted with no extension able to deliver: `advance` refuses and the row
    # stays queued, which is exactly the state left behind by a send that
    # happened somewhere this remote could not be reached from.
    core.extension = None
    with pytest.raises(NeedsRemote):
        core.send(
            requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
            message="what is x?", behavior="[QUERY]",
        )
    core.extension = StubExtension(source_prefix="science_")
    assert worker["id"] not in server._drain_threads

    armed = server.scan_once()

    assert armed >= 1, f"the supervisor should have armed a thread, it armed {armed}"
    assert worker["id"] in server._drain_threads


def test_the_supervisor_arms_a_thread_for_a_task_stranded_in_the_working_slot(db, core, server):
    """A queued row is not the only way a Partner is left unattended.

    A same-process `send` deletes the queue row as it promotes the task into
    the working slot, so a Partner whose remote is mid-turn has an EMPTY queue.
    If the arm that should have followed that send did not happen -- it is
    deliberately best-effort, and a thread can also die -- a queue-only scan
    would look right past a remote that is working with nobody watching it.
    """
    caller = make_orchestrator(core)
    worker = make_worker(core, caller, "science_")
    core.send(
        requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
        message="what is x?", behavior="[QUERY]",
    )
    assert queued_behaviors(db, worker["id"]) == [], (
        "setup check: a delivered task leaves nothing in the queue"
    )
    assert core.working_task(partner_id=worker["id"]) is not None
    assert worker["id"] not in server._drain_threads

    armed = server.scan_once()

    assert armed >= 1, f"the supervisor should have armed a thread, it armed {armed}"
    assert worker["id"] in server._drain_threads


def test_the_supervisor_leaves_a_partner_of_another_source_alone(db, core, server):
    caller = make_gemini_orchestrator(core, make_orchestrator(core))
    theirs = make_worker(core, caller, "gemini_")
    queue_one(core, caller, theirs, "gemini_")

    armed = server.scan_once()

    assert armed == 0, (
        f"a science_ process must not arm anything for a gemini_ partner; armed {armed}"
    )
    assert theirs["id"] not in server._drain_threads
    assert not has_row(db, theirs["id"])


def test_the_supervisor_scans_nothing_when_this_process_holds_no_extension(db, core):
    """An empty extension map is a real configuration, and must not query at all."""
    empty = PollingServer(db, extensions={}, poll_interval=0.01, core=core)
    try:
        assert empty.scan_once() == 0
    finally:
        empty.stop(timeout=5.0)


# ---------------------------------------------------------------------------
# 4. send arms the target's thread, and never fails because arming did.
# ---------------------------------------------------------------------------


def _call(server, name: str, arguments: dict) -> str:
    result = asyncio.run(server.call_tool(name, arguments))
    content = result[0] if isinstance(result, tuple) else result
    return "\n".join(block.text for block in content if hasattr(block, "text"))


def test_send_arms_the_drain_thread_for_its_target(db, core, server):
    """Without this the first message to a Partner is never polled.

    `advance()` delivers it, so the remote starts working -- and then nothing
    watches. `poll_completion` is never called, the slot is never released,
    and the answer never reaches the Caller. Before this fix the documented
    remedy was for an operator to call `notify_partner_push` by hand.
    """
    caller = make_orchestrator(core)
    worker = make_worker(core, caller, "science_")
    mcp = build_server(name="messaging-test", core=core, polling=server)

    body = _call(mcp, "send", {
        "requester_uuid": caller["uuid"], "queried_partner_title": worker["title"],
        "message": "what is x?", "behavior": "[QUERY]",
    })

    assert body.startswith("[ok]"), f"send should have succeeded, got: {body!r}"
    assert worker["id"] in server._drain_threads, (
        "send must arm the target's drain thread: nothing else polls the remote it just "
        "handed work to"
    )


def test_a_failure_to_arm_never_turns_a_successful_send_into_an_error(db, core, server, monkeypatch):
    """The receipt has already been earned by the time arming is attempted.

    Arming only makes the answer arrive sooner -- the supervisor picks the row
    up within one interval regardless -- so it must never be able to report
    that a committed message was rejected.
    """
    caller = make_orchestrator(core)
    worker = make_worker(core, caller, "science_")
    mcp = build_server(name="messaging-test", core=core, polling=server)

    def _boom(*args, **kwargs):
        raise RuntimeError("the thread pool is on fire")

    monkeypatch.setattr(server, "ensure_partner_thread", _boom)

    body = _call(mcp, "send", {
        "requester_uuid": caller["uuid"], "queried_partner_title": worker["title"],
        "message": "what is x?", "behavior": "[QUERY]",
    })

    assert body.startswith("[ok]"), (
        f"a failure to arm must not be reported as a failed send; got: {body!r}"
    )
    assert db.read_one(
        "SELECT 1 AS x FROM message_queue WHERE partner_id = ?", (worker["id"],)
    ) is not None or core.working_task(partner_id=worker["id"]) is not None, (
        "the message really was admitted, which is why the receipt must stand"
    )
