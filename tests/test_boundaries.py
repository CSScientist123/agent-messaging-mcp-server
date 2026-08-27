"""Adversarial boundary tests for the redesigned messaging system.

This file does not confirm the system does what it should -- every other test
file in this project is already that. It tries to CROSS the nine boundaries
the system claims to hold and reports, per boundary, whether it could.

Every attack below follows the same three-part oracle:

    1. The attempt is refused, with the correct error class -- not an
       incidental type/validation error that happens to block it today.
    2. No state changed -- checked directly against the database or the
       in-memory working slot, never only against the return value.
    3. The attempt is observable: a caller who tried it gets a real
       `Rejected` back (never a silent no-op) -- or, where that is NOT true,
       that silence is itself reported as the finding.

Where an attack is refused, the test asserts the secure behavior and stands
as a negative result: this was tried, and it did not work. One prior finding
against `report_back` (Claim 3: it admitted `[RESEARCH]` upward with none of
`send`'s checks) has since been fixed upstream and this file's test for it
was converted from a pinned `xfail` into a normal passing assertion of the
fix. One prior test against `archive_sessions` (Claim 6) asserted a role
requirement the spec does not actually make -- it was built off a comment on
`delete_partner` that turned out to be wrong and has since been corrected --
and has been replaced with a test of the rule the spec actually states:
same-project scope, no role required. Neither finding is a live crossing in
the file as it stands now; see the summary at the bottom for the full,
current state of every claim.

Fixtures use `Database(path=":memory:")`, `MessagingCore`, and
`extension.base.StubExtension` exactly as production wires a single
MessagingCore per `source_prefix` (see `mcp_server/config.py`): one
`MessagingCore` (and therefore one `WorkingSlots`) per source, all sharing
one `Database`, mirroring the real one-MCP-server-process-per-source
topology.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import uuid as uuid_lib

import pytest

from adapters.antigravity.adapter import AntigravityExtension
from extension.base import NonExecutingExtension, RemoteExtension, StubExtension
from mcp_server.server import build_server
from messaging_core.core import MessagingCore
from messaging_core.db import Database
from messaging_core.errors import Rejected

try:
    from adapters.claude_science.adapter import ClaudeScienceExtension
except Exception:  # pragma: no cover - optional dependency surface
    ClaudeScienceExtension = None

try:
    from adapters.notebooklm.adapter import NotebookLMExtension
except Exception:  # pragma: no cover - optional dependency surface
    NotebookLMExtension = None


# ===========================================================================
# fixtures and world-building helpers
# ===========================================================================


@pytest.fixture
def db():
    database = Database(path=":memory:")
    yield database
    database.close()


def mk_project(core: MessagingCore, *, title: str, source_prefix: str, system_id: str) -> int:
    return core.create_project(title=title, source_prefix=source_prefix, project_system_id=system_id)


def mk_partner(core: MessagingCore, *, project_id: int, title: str, remote_id: str, descr: str = "d") -> dict:
    return core.create_partner(project_id=project_id, title=title, partner_id_in_remote=remote_id, descr=descr)


class World:
    """One MessagingCore (and one StubExtension) per source_prefix, sharing one
    Database -- the real topology (one MCP server process per source_prefix).

    Project A (science_) holds a full role set: project-orchestrator,
    gemini-orchestrator, bridge-scientist, and a plain worker. Project B
    (science_) holds a second, independent project-orchestrator +
    gemini-orchestrator pair for the cross-project extension attacks and the
    "at most one science_ source" attack. Two gemini_ projects and two
    code_ projects exist for the single-code-partner and single-science-
    source cardinality attacks.
    """

    def __init__(self, db: Database):
        self.db = db
        self.sci_ext = StubExtension(source_prefix="science_")
        self.gem_ext = StubExtension(source_prefix="gemini_")
        self.nlm_ext = StubExtension(source_prefix="nlm_")
        self.code_ext = StubExtension(source_prefix="code_")
        self.sci = MessagingCore(db, self.sci_ext)
        self.gem = MessagingCore(db, self.gem_ext)
        self.nlm = MessagingCore(db, self.nlm_ext)
        self.code = MessagingCore(db, self.code_ext)

        # -- Project A (science_): full role set -----------------------------
        self.pid_a = mk_project(self.sci, title="SciA", source_prefix="science_", system_id="a-1")
        self.orch = mk_partner(self.sci, project_id=self.pid_a, title="orchA", remote_id="ra-orch")
        self.sci.claim_orchestrator(
            requester_uuid=self.orch["uuid"], project_id=self.pid_a, orchestrator_type="project-orchestrator"
        )
        self.gem_orch = mk_partner(self.sci, project_id=self.pid_a, title="gemorchA", remote_id="ra-gemorch")
        self.sci.claim_orchestrator(
            requester_uuid=self.gem_orch["uuid"], project_id=self.pid_a, orchestrator_type="gemini-orchestrator"
        )
        self.bridge = mk_partner(self.sci, project_id=self.pid_a, title="bridgeA", remote_id="ra-bridge")
        self.sci.claim_orchestrator(
            requester_uuid=self.bridge["uuid"], project_id=self.pid_a, orchestrator_type="bridge-scientist"
        )
        self.worker = mk_partner(self.sci, project_id=self.pid_a, title="workerA", remote_id="ra-worker")
        self.sci.grant_gemini_budget(
            requester_uuid=self.orch["uuid"], grantee_uuid=self.gem_orch["uuid"], budget_count=2
        )

        # -- Project B (science_): second, independent role set --------------
        self.pid_b = mk_project(self.sci, title="SciB", source_prefix="science_", system_id="b-1")
        self.orch_b = mk_partner(self.sci, project_id=self.pid_b, title="orchB", remote_id="rb-orch")
        self.sci.claim_orchestrator(
            requester_uuid=self.orch_b["uuid"], project_id=self.pid_b, orchestrator_type="project-orchestrator"
        )
        self.gem_orch_b = mk_partner(self.sci, project_id=self.pid_b, title="gemorchB", remote_id="rb-gemorch")
        self.sci.claim_orchestrator(
            requester_uuid=self.gem_orch_b["uuid"], project_id=self.pid_b, orchestrator_type="gemini-orchestrator"
        )
        self.sci.grant_gemini_budget(
            requester_uuid=self.orch_b["uuid"], grantee_uuid=self.gem_orch_b["uuid"], budget_count=2
        )

        # -- gemini_ world: two projects, two partners ------------------------
        self.gem_pid = mk_project(self.gem, title="GemWing", source_prefix="gemini_", system_id="g-1")
        self.gemini_partner = mk_partner(self.gem, project_id=self.gem_pid, title="gemw", remote_id="rg-1")
        self.gem_pid2 = mk_project(self.gem, title="GemWing2", source_prefix="gemini_", system_id="g-2")
        self.gemini_partner2 = mk_partner(self.gem, project_id=self.gem_pid2, title="gemw2", remote_id="rg-2")

        # -- nlm_ world ---------------------------------------------------------
        self.nlm_pid = mk_project(self.nlm, title="Notebook", source_prefix="nlm_", system_id="n-1")
        self.nlm_partner = mk_partner(self.nlm, project_id=self.nlm_pid, title="notebook", remote_id="rn-1")

        # -- code_ world: two projects, two partners --------------------------
        self.code_pid = mk_project(self.code, title="Repo", source_prefix="code_", system_id="c-1")
        self.code_partner = mk_partner(self.code, project_id=self.code_pid, title="ciagent", remote_id="rc-1")
        self.code_pid2 = mk_project(self.code, title="Repo2", source_prefix="code_", system_id="c-2")
        self.code_partner2 = mk_partner(self.code, project_id=self.code_pid2, title="ciagent2", remote_id="rc-2")

    def all_uuids(self) -> set[str]:
        return {
            p["uuid"]
            for p in (
                self.orch, self.gem_orch, self.bridge, self.worker,
                self.orch_b, self.gem_orch_b,
                self.gemini_partner, self.gemini_partner2,
                self.nlm_partner, self.code_partner, self.code_partner2,
            )
        }


@pytest.fixture
def world(db) -> World:
    return World(db)


def _flatten_strings(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _flatten_strings(v)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            yield from _flatten_strings(v)
    elif isinstance(obj, str):
        yield obj


def assert_no_foreign_uuid(result, forbidden_uuids: set[str]) -> None:
    strings = set(_flatten_strings(result))
    leaked = strings & forbidden_uuids
    assert not leaked, f"leaked a foreign uuid: {leaked!r} in {result!r}"


# ===========================================================================
# Claim 1 -- Cap: at most label_caps.max_outstanding of one label outstanding
# against one partner, per caller, counting the working slot.
# ===========================================================================


def test_cap_counts_the_working_slot_deterministically(world: World):
    """ATTACK (direct, sequential -- no thread-scheduling luck required). The
    claim is specific: "the count is keyed (partner_id, caller_id, behavior)
    and INCLUDES the in-memory working slot." Isolate exactly that term
    without any concurrency, so this fails for certain (not "usually") if
    `_ADMIT_SQL` ever stops adding `:working` to the count: push four
    `[QUERY]`s from ONE caller, one at a time, in order. The first always
    wins the empty slot (occupying 1 of the cap's 3), so admissions 2 and 3
    must be admitted (bringing outstanding to 1 working + 2 queued = 3) and
    the 4th must be refused -- if the slot's own occupant were not counted,
    a strictly sequential send is exactly the case where the mutation
    (`_ADMIT_SQL` counting only queued rows) would let a 4th through, since
    at admission time nothing is racing to obscure the count.
    """
    world.sci.handshake(requester_uuid=world.orch["uuid"], partner_title="workerA")

    r0 = world.sci.send(requester_uuid=world.orch["uuid"], queried_partner_title="workerA", message="m0", behavior="[QUERY]")
    assert r0["delivered"] == "[QUERY]", "the first QUERY into an empty slot must be delivered immediately"

    r1 = world.sci.send(requester_uuid=world.orch["uuid"], queried_partner_title="workerA", message="m1", behavior="[QUERY]")
    assert r1["delivered"] is None
    r2 = world.sci.send(requester_uuid=world.orch["uuid"], queried_partner_title="workerA", message="m2", behavior="[QUERY]")
    assert r2["delivered"] is None

    with pytest.raises(Rejected) as exc:
        world.sci.send(requester_uuid=world.orch["uuid"], queried_partner_title="workerA", message="m3", behavior="[QUERY]")
    assert exc.value.code == "over_queue"

    queued = world.db.read_one(
        "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id=? AND caller_id=? AND behavior='[QUERY]'",
        (world.worker["id"], world.orch["id"]),
    )["n"]
    assert queued == 2, f"exactly m1 and m2 should still be queued (m0 is working, m3 was refused), got {queued}"
    working = world.sci.working_task(partner_id=world.worker["id"])
    assert working["behavior"] == "[QUERY]" and working["body"] == "m0"


def test_cap_holds_under_concurrent_hammering(world: World):
    """ATTACK (concurrent, direct): N threads all push [QUERY] (cap 3) against
    the same partner from the same caller at once, released by a Barrier so
    they race the admission SQL as hard as this process can arrange. Held: at
    most 3 are ever admitted, and every rejection rolls its `messages` row
    back too (the row `_admit` writes for a `stored` label before the cap
    check runs) -- a rejected attempt must leave no trace in `messages`
    either, not just in `message_queue`.
    """
    world.sci.handshake(requester_uuid=world.orch["uuid"], partner_title="workerA")

    n_threads = 12
    barrier = threading.Barrier(n_threads)
    results: list[dict | None] = [None] * n_threads
    errors: list[Rejected | None] = [None] * n_threads

    def attempt(i: int) -> None:
        barrier.wait()
        try:
            results[i] = world.sci.send(
                requester_uuid=world.orch["uuid"],
                queried_partner_title="workerA",
                message=f"q-{i}",
                behavior="[QUERY]",
            )
        except Rejected as exc:
            errors[i] = exc

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert all(not t.is_alive() for t in threads), "a hammering thread never finished"

    admitted = [r for r in results if r is not None]
    rejections = [e for e in errors if e is not None]
    assert len(admitted) == 3, f"expected exactly 3 admissions under a cap of 3, got {len(admitted)}"
    assert len(rejections) == n_threads - 3
    assert all(e.code == "over_queue" for e in rejections), {e.code for e in rejections}

    queued = world.db.read_one(
        "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id=? AND caller_id=? AND behavior='[QUERY]'",
        (world.worker["id"], world.orch["id"]),
    )["n"]
    working = world.sci.working_task(partner_id=world.worker["id"])
    working_count = 1 if working and working["caller_id"] == world.orch["id"] and working["behavior"] == "[QUERY]" else 0
    assert queued + working_count == 3, "outstanding count (queued + working) drifted from the cap"

    # None of the 9 rejected attempts left a `messages` row behind -- the
    # rollback inside _admit must have undone the INSERT it made before the
    # cap check failed.
    stored = world.db.read_one(
        "SELECT COUNT(*) AS n FROM messages WHERE from_partner=? AND to_partner=? AND behavior='[QUERY]'",
        (world.orch["id"], world.worker["id"]),
    )["n"]
    assert stored == 3, f"a rejected [QUERY] left an orphan `messages` row (found {stored}, want 3)"


def test_cap_holds_across_an_in_flight_swap(world: World):
    """ATTACK (concurrent, across the swap): get a [RESEARCH] (cap 2) into the
    working slot, then displace it with a higher-priority [TRUTHFUL-REPORT]
    whose delivery is deliberately blocked mid-flight (simulating a slow
    remote), and fire a wave of concurrent [RESEARCH] pushes from OTHER
    threads while that displacement is still in progress -- exactly the
    "push while a swap is in flight" scenario.

    Held: `send`'s own admission write and `advance`'s swap both take the
    SAME per-partner lock (`WorkingSlots.lock_for`), so every concurrent push
    blocks until the in-flight swap finishes; nothing can observe or act on
    an intermediate state. Exactly one more [RESEARCH] is admitted (the
    original plus one is the cap of 2); every other concurrent attempt is
    rejected with `over_queue`.
    """
    db = Database(path=":memory:")
    try:
        started = threading.Event()
        release = threading.Event()

        class BlockingStub(StubExtension):
            def deliver_message(self, *, partner_id_in_remote, behavior, body):
                if behavior == "[TRUTHFUL-REPORT]":
                    started.set()
                    release.wait(timeout=10)
                return super().deliver_message(
                    partner_id_in_remote=partner_id_in_remote, behavior=behavior, body=body
                )

        ext = BlockingStub(source_prefix="science_")
        core = MessagingCore(db, ext)
        pid = mk_project(core, title="S", source_prefix="science_", system_id="s-1")
        orch = mk_partner(core, project_id=pid, title="orch", remote_id="r-orch")
        core.claim_orchestrator(requester_uuid=orch["uuid"], project_id=pid, orchestrator_type="project-orchestrator")
        worker = mk_partner(core, project_id=pid, title="worker", remote_id="r-worker")
        core.handshake(requester_uuid=orch["uuid"], partner_title="worker")

        # RESEARCH #1: takes the empty slot immediately (fast, not blocked).
        core.send(requester_uuid=orch["uuid"], queried_partner_title="worker", message="r1", behavior="[RESEARCH]")

        # The displacer: TRUTHFUL-REPORT (priority 1) strictly beats RESEARCH
        # (priority 4), so this triggers a real swap whose delivery call blocks.
        displacer = threading.Thread(
            target=lambda: core.send(
                requester_uuid=orch["uuid"], queried_partner_title="worker", message="summary", behavior="[TRUTHFUL-REPORT]"
            )
        )
        displacer.start()
        assert started.wait(timeout=5), "the displacing swap never reached its blocked delivery call"

        # Fire concurrent RESEARCH pushes while the swap above is confirmed
        # in flight (blocked inside advance(), holding worker's slot lock).
        n_pushers = 5
        barrier = threading.Barrier(n_pushers)
        results: list[dict | None] = [None] * n_pushers
        errors: list[Rejected | None] = [None] * n_pushers

        def push(i: int) -> None:
            barrier.wait()
            try:
                results[i] = core.send(
                    requester_uuid=orch["uuid"], queried_partner_title="worker", message=f"extra-{i}", behavior="[RESEARCH]"
                )
            except Rejected as exc:
                errors[i] = exc

        pushers = [threading.Thread(target=push, args=(i,)) for i in range(n_pushers)]
        for t in pushers:
            t.start()
        release.set()
        displacer.join(timeout=10)
        for t in pushers:
            t.join(timeout=10)
        assert not displacer.is_alive()
        assert all(not t.is_alive() for t in pushers)

        admitted = [r for r in results if r is not None]
        rejections = [e for e in errors if e is not None]
        assert len(admitted) == 1, f"cap of 2 (1 already outstanding) should admit exactly 1 more, got {len(admitted)}"
        assert len(rejections) == n_pushers - 1
        assert all(e.code == "over_queue" for e in rejections)

        total_research = db.read_one(
            "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id=? AND caller_id=? AND behavior='[RESEARCH]'",
            (worker["id"], orch["id"]),
        )["n"]
        working = core.working_task(partner_id=worker["id"])
        working_is_research = bool(working and working["behavior"] == "[RESEARCH]" and working["caller_id"] == orch["id"])
        assert total_research + (1 if working_is_research else 0) == 2
    finally:
        db.close()


def test_report_back_gate_runs_before_admission(world: World):
    """The gate runs BEFORE admission ever sees the behavior.

    A refusal that half-applied -- a `messages` row written, or a queue row
    inserted and then rejected -- is the failure an exception-only assertion
    cannot see, so the target's queue depth is checked on both sides.
    """
    depth_before = world.db.read_one(
        "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id=?", (world.worker["id"],)
    )["n"]
    for blocked in ("[RESEARCH]", "[IDLE]"):
        with pytest.raises(Rejected) as exc:
            world.sci.report_back(
                to_partner_id=world.worker["id"], from_partner_id=world.orch["id"], behavior=blocked, body="x"
            )
        assert exc.value.code == "not_reportable", blocked
    depth_after = world.db.read_one(
        "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id=?", (world.worker["id"],)
    )["n"]
    assert depth_after == depth_before, "a refused report_back still enqueued something"


def test_report_back_still_runs_real_admission_for_a_reply_label(world: World):
    """`report_back` narrows `behavior` to reply labels -- it does not
    bypass admission for the ones it does accept. `[QUERY]`'s cap can no
    longer be reached through it (both reply labels are uncapped today; see
    the next test for that mechanism specifically), so this asserts against
    something else observable that a bypass would also get wrong: a
    `[MESSAGE-RESPONSE]` (stored=1 per `label_caps`) actually gets written
    to `messages`, and the returned `queue_depth` reflects the real insert.
    """
    depth0 = world.db.read_one(
        "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id=?", (world.worker["id"],)
    )["n"]
    messages_before = world.db.read_one(
        "SELECT COUNT(*) AS n FROM messages WHERE from_partner=? AND to_partner=?",
        (world.orch["id"], world.worker["id"]),
    )["n"]

    result = world.sci.report_back(
        to_partner_id=world.worker["id"], from_partner_id=world.orch["id"], behavior="[MESSAGE-RESPONSE]", body="a real answer"
    )
    assert result["queue_depth"] == depth0 + 1

    messages_after = world.db.read_one(
        "SELECT COUNT(*) AS n FROM messages WHERE from_partner=? AND to_partner=?",
        (world.orch["id"], world.worker["id"]),
    )["n"]
    assert messages_after == messages_before + 1, "[MESSAGE-RESPONSE] is stored=1; report_back must still write it"


def test_report_back_cap_holds_if_a_reply_label_is_ever_capped(world: World):
    """Neither label `report_back` currently accepts is capped
    (`max_outstanding IS NULL` for both `[MESSAGE-RESPONSE]` and
    `[TRUTHFUL-REPORT]` today), so there is no live way to observe the cap
    through `report_back` against the current seed data. `label_caps` is
    data, not a hardcoded rule, so this tests the MECHANISM rather than
    today's seed values: temporarily cap `[MESSAGE-RESPONSE]` at 2 in this
    test's own database, confirm `report_back` still enforces it exactly
    like `send` would, then restore the row.
    """
    def _set_cap(n):
        return lambda conn: conn.execute(
            "UPDATE label_caps SET max_outstanding = ? WHERE behavior = '[MESSAGE-RESPONSE]'", (n,)
        )

    world.db.write(_set_cap(2))
    try:
        for i in range(2):
            world.sci.report_back(
                to_partner_id=world.worker["id"], from_partner_id=world.orch["id"], behavior="[MESSAGE-RESPONSE]", body=f"r{i}"
            )
        with pytest.raises(Rejected) as exc:
            world.sci.report_back(
                to_partner_id=world.worker["id"], from_partner_id=world.orch["id"], behavior="[MESSAGE-RESPONSE]", body="over"
            )
        assert exc.value.code == "over_queue"
    finally:
        world.db.write(_set_cap(None))

    depth = world.db.read_one(
        "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id=? AND caller_id=? AND behavior='[MESSAGE-RESPONSE]'",
        (world.worker["id"], world.orch["id"]),
    )["n"]
    # report_back never calls advance(), so the working slot never absorbs
    # one of these -- every admitted [MESSAGE-RESPONSE] stays queued.
    assert depth == 2

    row = world.db.read_one("SELECT max_outstanding FROM label_caps WHERE behavior = '[MESSAGE-RESPONSE]'")
    assert row["max_outstanding"] is None, "the temporary cap must be restored, not left mutated"


# ===========================================================================
# Claim 2 -- Priority: strict beats only, in_process is a same-label
# tie-break only, [IDLE] is a hold that is never requeued.
# ===========================================================================


def test_equal_priority_never_displaces_no_ping_pong(world: World):
    """ATTACK: two different callers alternate sending [QUERY] (same priority)
    to one partner, trying to make it ping-pong between them forever. `bridgeA`
    is the one target BOTH a project-orchestrator and a code_ partner can
    legitimately hold a standing handshake to at once, so this is a real,
    reachable two-caller shape rather than a synthetic one. Held: `advance`
    requires the head to STRICTLY beat the working task (`head_priority >=
    working["priority"]` blocks the swap), so an arriving [QUERY] never
    displaces a [QUERY] already being worked, regardless of who sent either.
    """
    world.sci.handshake(requester_uuid=world.orch["uuid"], partner_title="bridgeA")
    world.code.handshake(requester_uuid=world.code_partner["uuid"], partner_title="bridgeA")

    r1 = world.sci.send(
        requester_uuid=world.orch["uuid"], queried_partner_title="bridgeA", message="a1", behavior="[QUERY]"
    )
    assert r1["delivered"] == "[QUERY]"
    working_before = world.sci.working_task(partner_id=world.bridge["id"])
    assert working_before["caller_id"] == world.orch["id"]

    for i in range(3):
        # Delivered through world.sci -- the core matching bridgeA's OWN
        # source_prefix, exactly like the shared, multi-extension Polling
        # Server would (see MessagingCore.advance: it resolves the extension
        # from the RECIPIENT's project, never the caller's). Calling this
        # through world.code (code_partner's own single-source server
        # process) would correctly raise NeedsRemote for delivery -- the
        # message is admitted there, then delivered by whichever process
        # actually holds a matching extension.
        r2 = world.sci.send(
            requester_uuid=world.code_partner["uuid"], queried_partner_title="bridgeA", message=f"b{i}", behavior="[QUERY]"
        )
        assert r2["delivered"] is None, "an equal-priority QUERY displaced the working QUERY"
        working_now = world.sci.working_task(partner_id=world.bridge["id"])
        assert working_now["caller_id"] == world.orch["id"], "the working slot ping-ponged to the second caller"
        assert working_now["message_id"] == working_before["message_id"] or working_now["body"] == working_before["body"]


def test_paused_task_beats_fresh_task_of_the_same_label_only(world: World):
    """Positive control needed by the next two attacks: a paused (in_process=1)
    task must win the SAME-label tie-break over a fresh one, per the
    `message_queue_order` index and `_HEAD_SQL`'s `in_process DESC` term --
    specifically the tie-break, not merely "whichever arrived first".

    Built so chronology points the WRONG way on its own: "second" is queued
    BEFORE "first" is ever displaced, so "second" holds the earlier
    `enqueued_at` (and the lower `id`) of the two candidates. "first" only
    gets requeued (with a fresh, LATER timestamp) once it is displaced by
    the forced interruption. If `in_process DESC` were dropped from the sort
    -- ties would fall back to `enqueued_at ASC, id ASC` -- "second" would
    win instead, purely by having arrived first; only the explicit
    same-label tie-break makes "first" (paused) win despite arriving later.
    """
    world.sci.handshake(requester_uuid=world.orch["uuid"], partner_title="workerA")
    world.sci.send(requester_uuid=world.orch["uuid"], queried_partner_title="workerA", message="first", behavior="[QUERY]")
    # Equal priority: "second" does NOT displace "first" from the slot, and
    # is queued fresh (in_process=0) with the EARLIER of the two timestamps.
    world.sci.send(requester_uuid=world.orch["uuid"], queried_partner_title="workerA", message="second", behavior="[QUERY]")

    # Displaces "first" out of the slot; it re-enters the queue paused
    # (in_process=1) with a NEW, LATER enqueued_at than "second" already has.
    world.sci.interrupt_partner(requester_uuid=world.orch["uuid"], partner_title="workerA", reason="hold")
    row = world.db.read_one(
        "SELECT enqueued_at FROM message_queue WHERE partner_id=? AND body='second'", (world.worker["id"],)
    )
    paused_row = world.db.read_one(
        "SELECT enqueued_at FROM message_queue WHERE partner_id=? AND body='first'", (world.worker["id"],)
    )
    assert paused_row["enqueued_at"] >= row["enqueued_at"], (
        "test setup assumption broken: the paused row must not be chronologically "
        "earlier than the fresh one, or this stops isolating the tie-break"
    )

    # holding=True (slot holds [IDLE]) -- force the same head decision
    # `send`'s own advance() call would make, without going through send()
    # again (which would also work, but this isolates _HEAD_SQL directly).
    world.sci.advance(partner_id=world.worker["id"])
    working = world.sci.working_task(partner_id=world.worker["id"])
    assert working["behavior"] == "[QUERY]"
    assert working["body"] == "first", (
        f"a fresh QUERY (arrived first) beat the paused QUERY of the same label: working={working!r}"
    )
    assert bool(working["in_process"]) is True


def test_paused_research_does_not_beat_a_fresh_query(world: World):
    """ATTACK: can a paused [RESEARCH] (priority 4) beat a FRESH [QUERY]
    (priority 2, a different label) into the slot? `bridgeA` is legitimately
    reachable with [RESEARCH] from `code_partner` (layer 0, downward to the
    bridge's layer 1) and with ordinary messages from `orch` (its own
    project-orchestrator). Held: priority is compared globally first;
    in_process only tie-breaks within one label. A displaced, paused RESEARCH
    sits behind any fresh QUERY regardless of pause state.
    """
    world.sci.handshake(requester_uuid=world.orch["uuid"], partner_title="bridgeA")
    world.code.handshake(requester_uuid=world.code_partner["uuid"], partner_title="bridgeA")

    world.sci.send(
        requester_uuid=world.code_partner["uuid"], queried_partner_title="bridgeA", message="do-research", behavior="[RESEARCH]"
    )
    working = world.sci.working_task(partner_id=world.bridge["id"])
    assert working["behavior"] == "[RESEARCH]"

    world.sci.interrupt_partner(requester_uuid=world.orch["uuid"], partner_title="bridgeA", reason="hold")
    row = world.db.read_one(
        "SELECT in_process FROM message_queue WHERE partner_id=? AND behavior='[RESEARCH]'", (world.bridge["id"],)
    )
    assert row["in_process"] == 1, "the displaced RESEARCH was not marked paused"

    world.sci.send(requester_uuid=world.orch["uuid"], queried_partner_title="bridgeA", message="q", behavior="[QUERY]")
    working_after = world.sci.working_task(partner_id=world.bridge["id"])
    assert working_after["behavior"] == "[QUERY]", (
        f"a paused [RESEARCH] beat a fresh [QUERY] into the slot: working={working_after!r}"
    )
    assert bool(working_after["in_process"]) is False


def test_idle_hold_is_never_requeued_after_being_displaced(world: World):
    """ATTACK: after an [IDLE] hold takes the slot, can it come back? Held:
    when the hold itself is displaced, `advance`'s swap only requeues
    `working` `if working is not None and not holding` -- the hold's own
    displacement has `holding=True`, so it is dropped, never re-inserted.
    """
    world.sci.handshake(requester_uuid=world.orch["uuid"], partner_title="bridgeA")
    world.code.handshake(requester_uuid=world.code_partner["uuid"], partner_title="bridgeA")

    world.sci.send(requester_uuid=world.orch["uuid"], queried_partner_title="bridgeA", message="q0", behavior="[QUERY]")
    world.sci.interrupt_partner(requester_uuid=world.orch["uuid"], partner_title="bridgeA", reason="stop")
    assert world.sci.working_task(partner_id=world.bridge["id"])["behavior"] == "[IDLE]"

    world.sci.send(
        requester_uuid=world.code_partner["uuid"], queried_partner_title="bridgeA", message="q1", behavior="[QUERY]"
    )
    assert world.sci.working_task(partner_id=world.bridge["id"])["behavior"] == "[QUERY]"

    idle_rows = world.db.read(
        "SELECT * FROM message_queue WHERE partner_id=? AND behavior='[IDLE]'", (world.bridge["id"],)
    )
    assert idle_rows == [], f"a displaced [IDLE] hold was requeued: {[dict(r) for r in idle_rows]}"




def test_paused_query_does_not_beat_a_fresh_error_of_equal_priority(world: World):
    """ATTACK (the bug that actually shipped, per the incident report):
    `[QUERY]` and `[ERROR]` deliberately share priority 2. Before the pop
    order was split into `_HEAD_LABEL_SQL` (which LABEL runs next) and
    `_HEAD_ROW_SQL` (which ROW of that label), a single `ORDER BY` applied
    `in_process DESC` globally -- so a paused `[QUERY]` beat a FRESH
    `[ERROR]` arriving at the same priority. A Partner interrupted mid-
    question was handed "resume your previous [QUERY]" instead of the
    `[ERROR]` its Caller had just sent explaining what had gone wrong, and
    the correction was never delivered -- exactly the route a blocked
    Partner is supposed to get unblocked through under the approval
    doctrine.

    Held now: `_HEAD_LABEL_SQL` breaks a same-priority tie between labels by
    `MIN(in_process) ASC` -- a label with any fresh row outranks one that is
    only waiting to resume -- so `in_process` only ever tie-breaks WITHIN
    one label, never across two sharing a priority.
    """
    world.sci.handshake(requester_uuid=world.orch["uuid"], partner_title="workerA")

    # 1. A [QUERY] takes the empty working slot.
    r0 = world.sci.send(
        requester_uuid=world.orch["uuid"], queried_partner_title="workerA", message="what is X?", behavior="[QUERY]"
    )
    assert r0["delivered"] == "[QUERY]"

    # 2. Interrupted: [IDLE] takes the slot, the [QUERY] is queued paused
    # (in_process=1).
    world.sci.interrupt_partner(requester_uuid=world.orch["uuid"], partner_title="workerA", reason="stop")
    paused = world.db.read_one(
        "SELECT in_process FROM message_queue WHERE partner_id=? AND behavior='[QUERY]'", (world.worker["id"],)
    )
    assert paused["in_process"] == 1

    # 3. A fresh [ERROR] arrives -- same priority (2) as the paused [QUERY].
    r1 = world.sci.send(
        requester_uuid=world.orch["uuid"], queried_partner_title="workerA",
        message="that call failed: bad path", behavior="[ERROR]",
    )

    # 4. The [ERROR] -- not the paused [QUERY] -- is what reaches the remote,
    # delivered as a plain relay, never the "resume" template.
    assert r1["delivered"] == "[ERROR]"
    working = world.sci.working_task(partner_id=world.worker["id"])
    assert working["behavior"] == "[ERROR]", (
        f"a paused [QUERY] beat a fresh [ERROR] of equal priority: working={working!r}"
    )
    assert bool(working["in_process"]) is False
    assert "resume" not in working["prompt"].lower(), (
        f"the resume template was delivered instead of the [ERROR]'s own content: {working['prompt']!r}"
    )
    assert "bad path" in working["prompt"]

    # 5. Once the [ERROR] finishes and the slot is released, the paused
    # [QUERY] is what resumes next -- with its ORIGINAL body intact.
    world.sci.release(partner_id=world.worker["id"])
    world.sci.advance(partner_id=world.worker["id"])
    resumed = world.sci.working_task(partner_id=world.worker["id"])
    assert resumed["behavior"] == "[QUERY]"
    assert resumed["body"] == "what is X?"
    assert bool(resumed["in_process"]) is True


def test_paused_research_beats_a_fresh_research_same_label(world: World):
    """Companion to the cross-label test above, proving the scoping cuts
    both ways: WITHIN one label (here [RESEARCH], cap 2), a paused row still
    wins over a fresh one -- `_HEAD_ROW_SQL`'s `in_process DESC` is exactly
    the tie-break the cross-label test shows must NOT leak between labels.
    (The other half -- a paused [RESEARCH] loses to a fresh, higher-priority
    [QUERY] -- is already covered by test_paused_research_does_not_beat_a_fresh_query.)

    Built the same way as the same-label [QUERY] test above: "second" is
    queued before "first" is ever displaced, so chronology alone would favor
    "second"; only the same-label pause tie-break explains "first" winning.
    """
    world.sci.handshake(requester_uuid=world.orch["uuid"], partner_title="bridgeA")
    world.code.handshake(requester_uuid=world.code_partner["uuid"], partner_title="bridgeA")

    world.sci.send(
        requester_uuid=world.code_partner["uuid"], queried_partner_title="bridgeA", message="first", behavior="[RESEARCH]"
    )
    # Equal priority: does not displace "first"; queued fresh with the
    # EARLIER of the two timestamps this test cares about.
    world.sci.send(
        requester_uuid=world.code_partner["uuid"], queried_partner_title="bridgeA", message="second", behavior="[RESEARCH]"
    )

    # Displaces "first" out of the slot; it re-enters the queue paused
    # (in_process=1) with a NEW, LATER enqueued_at than "second" already has.
    world.sci.interrupt_partner(requester_uuid=world.orch["uuid"], partner_title="bridgeA", reason="hold")

    world.sci.advance(partner_id=world.bridge["id"])
    working = world.sci.working_task(partner_id=world.bridge["id"])
    assert working["behavior"] == "[RESEARCH]"
    assert working["body"] == "first", (
        f"a fresh RESEARCH (arrived first) beat the paused RESEARCH of the same label: working={working!r}"
    )
    assert bool(working["in_process"]) is True


# ===========================================================================
# Claim 3 -- Hierarchy: [RESEARCH] may not travel to a higher agent_layers
# partner. nlm_ never sends, never receives [RESEARCH].
# ===========================================================================


def test_research_cannot_flow_upward_through_send(world: World):
    """ATTACK: project-orchestrator (layer 2) sends [RESEARCH] to its own
    project's bridge-scientist (layer 1) -- a junior-to-senior hop inside ONE
    project, with a legitimate handshake already in place. Held: `send`
    refuses with `research_cannot_flow_upward` before admission; queue and
    `messages` are both untouched.
    """
    world.sci.handshake(requester_uuid=world.orch["uuid"], partner_title="bridgeA")
    depth_before = world.db.read_one(
        "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id=?", (world.bridge["id"],)
    )["n"]

    with pytest.raises(Rejected) as exc:
        world.sci.send(
            requester_uuid=world.orch["uuid"], queried_partner_title="bridgeA", message="do this", behavior="[RESEARCH]"
        )
    assert exc.value.code == "research_cannot_flow_upward"

    depth_after = world.db.read_one(
        "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id=?", (world.bridge["id"],)
    )["n"]
    assert depth_after == depth_before
    assert world.sci.working_task(partner_id=world.bridge["id"]) is None


def test_nlm_never_receives_research(world: World):
    """ATTACK: send [RESEARCH] to an nlm_ partner (accepts_research=0). Held:
    refused with `research_not_accepted` before admission.
    """
    with pytest.raises(Rejected) as exc:
        world.code.send(
            requester_uuid=world.code_partner["uuid"],
            queried_partner_title="notebook",
            message="go look this up",
            behavior="[RESEARCH]",
        )
    assert exc.value.code == "research_not_accepted"
    depth = world.db.read_one(
        "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id=?", (world.nlm_partner["id"],)
    )["n"]
    assert depth == 0


def test_nlm_can_never_send_anything(world: World):
    """ATTACK: have the nlm_ partner itself originate a message of any label
    (can_send=0). Held: refused with `source_cannot_send` for every label
    tried, before admission.
    """
    for behavior in ("[QUERY]", "[ERROR]", "[MESSAGE-RESPONSE]", "[TRUTHFUL-REPORT]", "[RESEARCH]"):
        with pytest.raises(Rejected) as exc:
            world.nlm.send(
                requester_uuid=world.nlm_partner["uuid"],
                queried_partner_title="ciagent",
                message="hi",
                behavior=behavior,
            )
        assert exc.value.code == "source_cannot_send", behavior
    depth = world.db.read_one(
        "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id=?", (world.code_partner["id"],)
    )["n"]
    assert depth == 0


def test_interrupt_partner_cannot_cross_projects(world: World):
    """ATTACK: interrupt a partner in a DIFFERENT project. Held: refused
    `different_project`; no [IDLE] admitted, nothing recorded in
    `drain_threads`, and the target's working slot is untouched.
    """
    with pytest.raises(Rejected) as exc:
        world.sci.interrupt_partner(requester_uuid=world.orch["uuid"], partner_title="orchB", reason="stop")
    assert exc.value.code == "different_project"
    depth = world.db.read_one(
        "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id=?", (world.orch_b["id"],)
    )["n"]
    assert depth == 0
    assert world.sci.working_task(partner_id=world.orch_b["id"]) is None


def test_report_back_refuses_a_non_reply_behavior_no_research_upward(world: World):
    """ATTACK, now HELD (fixed since this file's first pass -- see the
    docstring history in the report). `report_back` is the second writer into
    `message_queue` -- used internally by the Polling Server's drain loop to
    push a finished task's answer back to its caller. It used to call the
    shared `_admit` directly with NO check that `behavior` was even an answer,
    which let `[RESEARCH]` travel upward through it after `send()` had
    already refused the identical from/to pair. It now checks `behavior`
    against `label_caps.reply_behavior` first and refuses anything that is
    not some label's actual reply with `not_a_reply_behavior` --
    `[MESSAGE-RESPONSE]` and `[TRUTHFUL-REPORT]` are the only two values that
    ever pass.

    Exact reproduction that must now be refused:

        core.report_back(
            to_partner_id=<a project-orchestrator, layer 2>,
            from_partner_id=<a bridge-scientist under the SAME project, layer 1>,
            behavior="[RESEARCH]",
            body="evil upward research",
        )
        # -> Rejected("not_reportable"); nothing lands in the queue.

    The rule report_back enforces is narrower than "only answers": it refuses
    what a Partner cannot REPORT. [RESEARCH] is delegation, and admitting it
    here would be a second door into a superior's queue that skips the layer
    check send makes. [IDLE] is a hold and means nothing in a queue.

    [QUERY] and [ERROR] must NOT be refused, and that matters as much: they are
    how a Partner that cannot speak for itself gets heard -- an agent stopped on
    a permission prompt is not running, and nothing else is watching it.
    """
    orch_row = world.db.read_one("SELECT * FROM partners WHERE id=?", (world.orch["id"],))
    bridge_row = world.db.read_one("SELECT * FROM partners WHERE id=?", (world.bridge["id"],))
    assert world.sci._layer(bridge_row) < world.sci._layer(orch_row), "bridge must outrank project-orchestrator"

    with pytest.raises(Rejected) as exc:
        world.sci.send(
            requester_uuid=world.orch["uuid"], queried_partner_title="bridgeA", message="x", behavior="[RESEARCH]"
        )
    assert exc.value.code == "research_cannot_flow_upward"  # send() blocks this direction

    depth_before = world.db.read_one(
        "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id=?", (world.orch["id"],)
    )["n"]

    for blocked in ("[RESEARCH]", "[IDLE]"):
        with pytest.raises(Rejected) as exc2:
            world.sci.report_back(
                to_partner_id=world.orch["id"], from_partner_id=world.bridge["id"], behavior=blocked, body="evil upward research"
            )
        assert exc2.value.code == "not_reportable", blocked

    depth_after = world.db.read_one(
        "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id=?", (world.orch["id"],)
    )["n"]
    assert depth_after == depth_before, "a refused report_back call still enqueued something"

    # Everything a Partner can genuinely report must still be admitted.
    for real_reply in ("[MESSAGE-RESPONSE]", "[TRUTHFUL-REPORT]", "[QUERY]", "[ERROR]"):
        result = world.sci.report_back(
            to_partner_id=world.orch["id"], from_partner_id=world.bridge["id"], behavior=real_reply, body="a real answer"
        )
        assert result["behavior"] == real_reply


# ===========================================================================
# Claim 4 -- Handshake legality.
# ===========================================================================


def test_code_handshake_is_restricted_to_bridge_scientist_only(world: World):
    """ATTACK: (a) a code_ partner tries to handshake a plain science_ worker
    (not a bridge-scientist); (b) a plain science_ worker tries to handshake
    a code_ partner. Held: both directions refused
    `code_handshakes_bridge_only`; no row inserted either time.
    """
    with pytest.raises(Rejected) as exc_a:
        world.code.handshake(requester_uuid=world.code_partner["uuid"], partner_title="workerA")
    assert exc_a.value.code == "code_handshakes_bridge_only"

    with pytest.raises(Rejected) as exc_b:
        world.sci.handshake(requester_uuid=world.worker["uuid"], partner_title="ciagent")
    assert exc_b.value.code == "code_handshakes_bridge_only"

    count = world.db.read_one(
        "SELECT COUNT(*) AS n FROM handshakes WHERE from_partner IN (?, ?)",
        (world.code_partner["id"], world.worker["id"]),
    )["n"]
    assert count == 0


def test_bridge_holds_at_most_one_code_partner(world: World):
    """ATTACK: bridge-scientist already handshaken with one code_ partner;
    try to add a SECOND, different code_ partner. Held: refused
    `bridge_single_code_partner`; the second handshake is never created.
    """
    world.code.handshake(requester_uuid=world.code_partner["uuid"], partner_title="bridgeA")
    with pytest.raises(Rejected) as exc:
        world.code.handshake(requester_uuid=world.code_partner2["uuid"], partner_title="bridgeA")
    assert exc.value.code == "bridge_single_code_partner"

    partners = {
        r["from_partner"]
        for r in world.db.read("SELECT from_partner FROM handshakes WHERE to_partner=?", (world.bridge["id"],))
    }
    assert partners == {world.code_partner["id"]}


def test_two_gemini_partners_can_never_handshake(world: World):
    """ATTACK, two layers, and the outer one got stronger.

    A gemini_ partner has no role, so it is refused at the generic orchestrator
    gate. It also cannot ACQUIRE one any more: every orchestrator role is Claude
    Science only, so `claim_orchestrator` refuses it outright. An earlier version
    of this test exploited exactly that gap to get past the gate.

    The attack therefore goes around `claim_orchestrator` entirely and writes the
    role straight into the database -- which is the only remaining way to reach
    the deeper check, and precisely the code path a rule enforced in one
    capability alone would miss. The unconditional `no_handshake_between_gemini`
    in `handshake()` still catches it.
    """
    with pytest.raises(Rejected) as exc_plain:
        world.gem.handshake(requester_uuid=world.gemini_partner["uuid"], partner_title="gemw2")
    assert exc_plain.value.code == "requester_not_orchestrator"

    same_project_pid = world.gem_pid
    peer = mk_partner(world.gem, project_id=same_project_pid, title="gemw-peer", remote_id="rg-peer")
    with pytest.raises(Rejected) as exc_role:
        world.gem.claim_orchestrator(
            requester_uuid=world.gemini_partner["uuid"], project_id=same_project_pid,
            orchestrator_type="bridge-scientist")
    assert exc_role.value.code == "orchestrator_requires_science_project", (
        "a gemini_ partner must not be able to claim any orchestrator role"
    )
    world.db.write(lambda c: c.execute(
        "UPDATE partners SET orchestrator_type='bridge-scientist' WHERE uuid=?",
        (world.gemini_partner["uuid"],)))
    with pytest.raises(Rejected) as exc_pseudo:
        world.gem.handshake(requester_uuid=world.gemini_partner["uuid"], partner_title="gemw-peer")
    assert exc_pseudo.value.code == "no_handshake_between_gemini"

    count = world.db.read_one(
        "SELECT COUNT(*) AS n FROM handshakes WHERE from_partner=?", (world.gemini_partner["id"],)
    )["n"]
    assert count == 0


def test_gemini_to_science_direction_is_never_legal_even_for_a_pseudo_orchestrator(world: World):
    """ATTACK: same shape as above, aimed at the gemini_ -> science_ direction.

    The role can no longer be claimed, so it is forced in directly. Held:
    `gemini_to_science_illegal` fires unconditionally once the requester looks
    like an orchestrator; the legitimate direction is science_ -> gemini_ only,
    and it is never inherited in reverse.
    """
    with pytest.raises(Rejected) as exc_role:
        world.gem.claim_orchestrator(
            requester_uuid=world.gemini_partner2["uuid"], project_id=world.gem_pid2,
            orchestrator_type="project-orchestrator")
    assert exc_role.value.code == "orchestrator_requires_science_project"
    world.db.write(lambda c: c.execute(
        "UPDATE partners SET orchestrator_type='project-orchestrator' WHERE uuid=?",
        (world.gemini_partner2["uuid"],)))
    with pytest.raises(Rejected) as exc:
        world.gem.handshake(requester_uuid=world.gemini_partner2["uuid"], partner_title="orchA")
    assert exc.value.code == "gemini_to_science_illegal"

    count = world.db.read_one(
        "SELECT COUNT(*) AS n FROM handshakes WHERE from_partner=?", (world.gemini_partner2["id"],)
    )["n"]
    assert count == 0


def test_gemini_partner_reachable_from_at_most_one_science_source(world: World):
    """ATTACK: two DIFFERENT science_ gemini-orchestrators (different
    projects) both try to reach the SAME gemini_ partner. Held: the first
    handshake succeeds; the second is refused `gemini_single_science_source`
    and no second row is created.
    """
    r1 = world.sci.handshake(requester_uuid=world.gem_orch["uuid"], partner_title="gemw")
    assert r1["handshake_id"]

    with pytest.raises(Rejected) as exc:
        world.sci.handshake(requester_uuid=world.gem_orch_b["uuid"], partner_title="gemw")
    assert exc.value.code == "gemini_single_science_source"

    sources = {
        r["from_partner"]
        for r in world.db.read("SELECT from_partner FROM handshakes WHERE to_partner=?", (world.gemini_partner["id"],))
    }
    assert sources == {world.gem_orch["id"]}


def test_project_extension_grants_nothing_within_a_single_project(world: World):
    """ATTACK: try to declare a project an extension of ITSELF, to see if the
    same "extension" machinery that loosens same-source cross-project
    handshakes can be turned into a grant WITHIN one project. Held: refused
    `self_extension`; `project_extension` gains no row, and (structurally)
    two partners of one project never even reach the extension code path --
    their `project_id` is already equal.
    """
    with pytest.raises(Rejected) as exc:
        world.sci.extend_project(requester_uuid=world.orch["uuid"], project_title="SciA")
    assert exc.value.code == "self_extension"
    count = world.db.read_one("SELECT COUNT(*) AS n FROM project_extension")["n"]
    assert count == 0


def test_project_extension_requires_matching_roles_no_inheritance(world: World):
    """ATTACK: extend Project A and Project B, then try to have B's
    gemini-orchestrator handshake A's project-orchestrator directly --
    "inheriting a superior it was never given". Held: refused
    `cross_project_requires_same_role` in both directions; no handshake row
    created.
    """
    world.sci.extend_project(requester_uuid=world.orch["uuid"], project_title="SciB")

    with pytest.raises(Rejected) as exc1:
        world.sci.handshake(requester_uuid=world.gem_orch_b["uuid"], partner_title="orchA")
    assert exc1.value.code == "cross_project_requires_same_role"

    with pytest.raises(Rejected) as exc2:
        world.sci.handshake(requester_uuid=world.orch["uuid"], partner_title="gemorchB")
    assert exc2.value.code == "cross_project_requires_same_role"

    count = world.db.read_one(
        "SELECT COUNT(*) AS n FROM handshakes WHERE from_partner IN (?, ?)",
        (world.gem_orch_b["id"], world.orch["id"]),
    )["n"]
    assert count == 0


def test_project_extension_requires_the_same_source(world: World):
    """ATTACK: extend a science_ project with a gemini_ project (cross-source
    extension is meaningless -- that direction already handshakes without
    one). Held: refused `cross_source_extension`; no row created.
    """
    with pytest.raises(Rejected) as exc:
        world.sci.extend_project(requester_uuid=world.orch["uuid"], project_title="GemWing")
    assert exc.value.code == "cross_source_extension"
    count = world.db.read_one("SELECT COUNT(*) AS n FROM project_extension")["n"]
    assert count == 0


def test_project_extension_grants_messaging_only_not_administrative_reach(world: World):
    """ATTACK: after a LEGITIMATE same-role extension + handshake between
    Project A's and Project B's project-orchestrators, does A's orchestrator
    gain any administrative reach into B (archive/delete/budget) alongside
    the messaging capability the extension is meant to grant? Held:
    `archive_sessions` is scoped by `project_id` equality directly and is
    unaffected by `handshakes` or `project_extension` -- the target is
    skipped `different_project`, not archived.
    """
    world.sci.extend_project(requester_uuid=world.orch["uuid"], project_title="SciB")
    world.sci.handshake(requester_uuid=world.orch["uuid"], partner_title="orchB")

    result = world.sci.archive_sessions(requester_uuid=world.orch["uuid"], titles=["orchB"])
    assert result["archived"] == []
    assert result["skipped"] == [{"title": "orchB", "reason": "different_project"}]

    row = world.db.read_one("SELECT archived_at FROM partners WHERE id=?", (world.orch_b["id"],))
    assert row["archived_at"] is None


# ===========================================================================
# Claim 5 -- Identity and disclosure.
# ===========================================================================


def test_a_title_cannot_be_used_as_a_requester_uuid(world: World):
    """ATTACK: pass a perfectly valid partner TITLE where `requester_uuid` is
    expected. Held: `_resolve_requester` matches the `uuid` column only, so
    this resolves to nobody, refused `unknown_requester`; no state change.
    """
    before = world.db.read_one(
        "SELECT COUNT(*) AS n FROM handshakes WHERE from_partner=?", (world.worker["id"],)
    )["n"]
    with pytest.raises(Rejected) as exc:
        world.sci.status(requester_uuid="workerA")  # a real title, not a uuid
    assert exc.value.code == "unknown_requester"
    with pytest.raises(Rejected):
        world.sci.handshake(requester_uuid="workerA", partner_title="bridgeA")
    after = world.db.read_one(
        "SELECT COUNT(*) AS n FROM handshakes WHERE from_partner=?", (world.worker["id"],)
    )["n"]
    assert after == before


def test_unknown_and_archived_requesters_are_indistinguishable(world: World):
    """ATTACK (inference): does an archived identity's own refusal differ
    from a uuid that never existed at all? Held: `_resolve_requester` filters
    `archived_at IS NULL`, so both miss identically, same code AND same
    message (the message contains no identifying detail either).
    """
    archived_uuid = world.worker["uuid"]
    world.sci.archive_sessions(requester_uuid=world.orch["uuid"], titles=["workerA"])
    never_existed = str(uuid_lib.uuid4())

    with pytest.raises(Rejected) as exc_a:
        world.sci.status(requester_uuid=archived_uuid)
    with pytest.raises(Rejected) as exc_b:
        world.sci.status(requester_uuid=never_existed)

    assert exc_a.value.code == exc_b.value.code == "unknown_requester"
    assert exc_a.value.message == exc_b.value.message


def test_grant_gemini_budget_orders_authorization_before_existence(world: World):
    """ATTACK (information flow). An attacker who holds NO role and shares no
    project with anything probes `grant_gemini_budget` with three
    `grantee_uuid` guesses that ought to fall into three different classes
    (nonexistent / real-but-wrong-role / real-and-correct-role-elsewhere).
    Held: the requester's own `project-orchestrator` check runs first and
    fails identically for all three -- not a single query about
    `grantee_uuid` ever needs to run to produce the refusal, so the response
    cannot be used to test whether a guessed uuid exists.
    """
    attacker = world.code_partner  # no role, no project overlap with the science_ world
    guesses = {
        "nonexistent": str(uuid_lib.uuid4()),
        "real_partner_wrong_role": world.worker["uuid"],
        "real_gemini_orchestrator_elsewhere": world.gem_orch["uuid"],
    }
    codes, messages = set(), set()
    for guess in guesses.values():
        with pytest.raises(Rejected) as exc:
            world.code.grant_gemini_budget(requester_uuid=attacker["uuid"], grantee_uuid=guess, budget_count=1)
        codes.add(exc.value.code)
        messages.add(exc.value.message)
    assert codes == {"not_authorized"}, f"refusal differs by probe, an enumeration oracle: {codes!r}"
    assert len(messages) == 1

    grant = world.db.read_one(
        "SELECT budget_count FROM budget_grants WHERE grantee_partner=?", (world.gem_orch["id"],)
    )
    assert grant["budget_count"] == 2, "a probe must never alter the real budget"


def test_grant_gemini_budget_does_not_leak_a_live_targets_existence_across_projects(world: World):
    """ATTACK (information flow, sharper). This requester genuinely IS a
    project-orchestrator -- just not of the grantee's project. Held: because
    the grantee lookup is scoped to the REQUESTER's own project, a live,
    correctly-roled gemini-orchestrator belonging to a DIFFERENT project
    refuses IDENTICALLY to a uuid that names nothing at all -- an authorized-
    but-misdirected requester learns nothing about what exists elsewhere.
    """
    guesses = {
        "nonexistent": str(uuid_lib.uuid4()),
        "lives_in_project_b_correct_role": world.gem_orch_b["uuid"],
        "lives_in_project_b_wrong_role": world.orch_b["uuid"],
    }
    codes = set()
    for guess in guesses.values():
        with pytest.raises(Rejected) as exc:
            world.sci.grant_gemini_budget(requester_uuid=world.orch["uuid"], grantee_uuid=guess, budget_count=1)
        codes.add(exc.value.code)
    assert codes == {"not_authorized"}

    for pid in (world.gem_orch_b["id"], world.orch_b["id"]):
        row = world.db.read_one("SELECT budget_count FROM budget_grants WHERE grantee_partner=?", (pid,))
        assert row is None or pid == world.gem_orch_b["id"] and row["budget_count"] == 2


def test_no_tool_accepts_a_requester_title(world: World):
    """ATTACK: sweep every registered MCP tool's input schema for a
    `requester_title` parameter -- the disclosure claim is specifically that
    no tool takes one; every tool identifies its caller by uuid only.
    """
    server = build_server(name="boundary-test", core=world.sci)
    tools = asyncio.run(server.list_tools())
    offenders = []
    for tool in tools:
        properties = (tool.inputSchema or {}).get("properties", {})
        if "requester_title" in properties:
            offenders.append(tool.name)
    assert offenders == [], f"tool(s) accept a requester_title: {offenders!r}"


def test_no_capability_leaks_a_foreign_uuid(world: World):
    """ATTACK (inference): sweep every capability that returns data for any
    OTHER partner's uuid leaking into the response, in any field or nested
    structure.
    """
    forbidden = world.all_uuids() - {world.orch["uuid"]}

    world.sci.handshake(requester_uuid=world.orch["uuid"], partner_title="workerA")
    calls = [
        lambda: world.sci.status(requester_uuid=world.orch["uuid"]),
        lambda: world.sci.search_partner(requester_uuid=world.orch["uuid"], query_title="work"),
        lambda: world.sci.search_project(requester_uuid=world.orch["uuid"], query_title="Sci"),
        lambda: world.sci.handshake(requester_uuid=world.orch["uuid"], partner_title="gemorchA"),
        lambda: world.sci.send(
            requester_uuid=world.orch["uuid"], queried_partner_title="workerA", message="hi", behavior="[QUERY]"
        ),
        lambda: world.sci.read(requester_uuid=world.orch["uuid"], partner_title="workerA"),
        lambda: world.sci.interrupt_partner(requester_uuid=world.orch["uuid"], partner_title="workerA", reason="x"),
    ]
    for call in calls:
        result = call()
        assert_no_foreign_uuid(result, forbidden)
    for hit in world.sci.search_partner(requester_uuid=world.orch["uuid"], query_title="work"):
        assert "uuid" not in hit


# ===========================================================================
# Claim 6 -- Archived partners.
# ===========================================================================


def _archive(world: World, title: str) -> None:
    result = world.sci.archive_sessions(requester_uuid=world.orch["uuid"], titles=[title])
    assert result["archived_count"] == 1


def test_cannot_message_handshake_interrupt_or_read_an_archived_partner(world: World):
    world.sci.handshake(requester_uuid=world.orch["uuid"], partner_title="workerA")
    _archive(world, "workerA")

    depth_before = world.db.read_one(
        "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id=?", (world.worker["id"],)
    )["n"]

    with pytest.raises(Rejected) as exc1:
        world.sci.send(
            requester_uuid=world.orch["uuid"], queried_partner_title="workerA", message="hi", behavior="[QUERY]"
        )
    assert exc1.value.code == "no_such_partner"

    with pytest.raises(Rejected) as exc2:
        world.sci.handshake(requester_uuid=world.orch["uuid"], partner_title="workerA")
    assert exc2.value.code == "no_such_partner"

    with pytest.raises(Rejected) as exc3:
        world.sci.interrupt_partner(requester_uuid=world.orch["uuid"], partner_title="workerA", reason="x")
    assert exc3.value.code == "no_such_partner"

    with pytest.raises(Rejected) as exc4:
        world.sci.read(requester_uuid=world.orch["uuid"], partner_title="workerA")
    assert exc4.value.code == "no_such_partner"

    depth_after = world.db.read_one(
        "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id=?", (world.worker["id"],)
    )["n"]
    assert depth_after == depth_before


def test_archived_partner_cannot_act_as_itself(world: World):
    """ATTACK: the archived partner itself tries to act -- status, handshake,
    send, archive_sessions. Held: `unknown_requester` in every case, since
    `_resolve_requester` filters `archived_at IS NULL`.
    """
    archived_uuid = world.worker["uuid"]
    _archive(world, "workerA")

    calls = [
        lambda: world.sci.status(requester_uuid=archived_uuid),
        lambda: world.sci.handshake(requester_uuid=archived_uuid, partner_title="bridgeA"),
        lambda: world.sci.send(
            requester_uuid=archived_uuid, queried_partner_title="bridgeA", message="hi", behavior="[QUERY]"
        ),
        lambda: world.sci.archive_sessions(requester_uuid=archived_uuid, titles=["bridgeA"]),
    ]
    for call in calls:
        with pytest.raises(Rejected) as exc:
            call()
        assert exc.value.code == "unknown_requester"


def test_status_never_lists_an_archived_partner_in_handshakes(world: World):
    """Regression lock-in: `status()`'s handshake queries filter
    `p.archived_at IS NULL` on both the in and out direction, so an archived
    counterpart drops out of a live partner's status immediately.
    """
    world.sci.handshake(requester_uuid=world.orch["uuid"], partner_title="workerA")
    _archive(world, "workerA")
    result = world.sci.status(requester_uuid=world.orch["uuid"])
    assert "workerA" not in result["handshakes_out"]


def test_work_queued_before_archiving_is_discarded_not_delivered(world: World):
    """ATTACK: get one message into the working slot and a SECOND, equal-
    priority message sitting in the queue, then archive the partner, then
    call `advance()` again (exactly what the drain thread does on its next
    pass). Held: the queued work is deleted rather than delivered; the
    working slot is cleared; no new `deliver_message` call is made for it.
    """
    world.sci.handshake(requester_uuid=world.orch["uuid"], partner_title="workerA")
    world.sci.send(requester_uuid=world.orch["uuid"], queried_partner_title="workerA", message="m1", behavior="[QUERY]")
    world.sci.send(requester_uuid=world.orch["uuid"], queried_partner_title="workerA", message="m2", behavior="[QUERY]")

    depth_before = world.db.read_one(
        "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id=?", (world.worker["id"],)
    )["n"]
    assert depth_before == 1, "m2 should be sitting in queue behind m1's working slot"

    _archive(world, "workerA")
    calls_before = len(world.sci_ext.calls)

    outcome = world.sci.advance(partner_id=world.worker["id"])
    assert outcome is None

    depth_after = world.db.read_one(
        "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id=?", (world.worker["id"],)
    )["n"]
    assert depth_after == 0, "work queued before archiving must be discarded, not left in place"
    assert world.sci.working_task(partner_id=world.worker["id"]) is None
    new_calls = world.sci_ext.calls[calls_before:]
    assert not any(name == "deliver_message" for name, _ in new_calls), (
        f"advance() delivered to an archived partner: {new_calls!r}"
    )


def test_archived_title_stays_permanently_spent(world: World):
    """ATTACK: archive a partner, then try to create a NEW partner reusing
    its exact title. Held: `create_partner`'s title check has no
    `archived_at` filter, so the title is refused `duplicate_partner_title`
    even though the original holder is gone.
    """
    _archive(world, "workerA")
    with pytest.raises(Rejected) as exc:
        world.sci.create_partner(
            project_id=world.pid_a, title="workerA", partner_id_in_remote="ra-worker-2", descr="new"
        )
    assert exc.value.code == "duplicate_partner_title"


def test_direct_sql_cannot_rename_an_archived_partner(world: World):
    """ATTACK (path and indirection): bypass every application-level guard
    entirely and issue the rename as a raw UPDATE through `Database.write`
    directly. Held: `partners_no_rename_archived` lives in the schema, not in
    application code, so even a caller with direct write access to the
    connection cannot rename an archived partner's title.
    """
    _archive(world, "workerA")

    def _rename(conn):
        conn.execute("UPDATE partners SET title = ? WHERE id = ?", ("workerA-reborn", world.worker["id"]))

    with pytest.raises(Exception) as exc:
        world.db.write(_rename)
    assert "cannot be renamed" in str(exc.value)

    row = world.db.read_one("SELECT title FROM partners WHERE id=?", (world.worker["id"],))
    assert row["title"] == "workerA"


def test_archive_sessions_requires_only_same_project_no_role(world: World):
    """NOT a boundary crossing (confirmed against the spec, not just the
    code): `archive_sessions` deliberately has no orchestrator-role
    requirement. The authority it actually claims is narrower than
    `delete_partner`'s on purpose -- archiving frees a live-partner slot and
    leaves the row and its permanently-spent title in place; it is
    reversible in effect (a fresh partner can be created to replace the
    slot) in a way deletion is not, so deletion alone carries the extra
    role requirement.

    (An earlier version of this file asserted the opposite -- that
    archive_sessions should require project-orchestrator -- based on a
    comment on `delete_partner` claiming the two were "scoped exactly
    alike". That comment was wrong and has been corrected; this test
    replaces that one with the rule the spec actually makes.)

    Attacked here: a role-less partner CAN archive another live partner
    of its OWN project (no role needed), and CANNOT archive one belonging
    to a DIFFERENT project even by naming it correctly -- the only scope
    check `archive_sessions` makes is `project_id` equality, and it is
    enforced.
    """
    # A plain worker, holding no role at all, archiving its own project's
    # project-orchestrator: allowed. No role requirement to defeat.
    result = world.sci.archive_sessions(requester_uuid=world.worker["uuid"], titles=["orchA"])
    assert result["archived"] == ["orchA"]
    assert result["skipped"] == []
    row = world.db.read_one("SELECT archived_at FROM partners WHERE id=?", (world.orch["id"],))
    assert row["archived_at"] is not None

    # The SAME role-less worker reaching into a DIFFERENT project: refused
    # by the project-scope check, not by a role check -- `orchB` stays live.
    result2 = world.sci.archive_sessions(requester_uuid=world.worker["uuid"], titles=["orchB"])
    assert result2["archived"] == []
    assert result2["skipped"] == [{"title": "orchB", "reason": "different_project"}]
    row_b = world.db.read_one("SELECT archived_at FROM partners WHERE id=?", (world.orch_b["id"],))
    assert row_b["archived_at"] is None


# ===========================================================================
# Claim 7 -- Permissions.
# ===========================================================================


def test_partner_paths_trigger_rejects_non_gemini_partners_via_raw_sql(world: World):
    """ATTACK (path and indirection): bypass `_require_gemini` entirely and
    insert a `partner_paths` row for a non-gemini_ partner directly through
    `Database.write`. Held: `partner_paths_gemini_only` is a schema trigger,
    not an application check, so it still fires.
    """
    def _insert(conn):
        conn.execute(
            "INSERT INTO partner_paths (partner_id, kind, path) VALUES (?, 'read', '/x')",
            (world.code_partner["id"],),
        )

    with pytest.raises(Exception) as exc:
        world.db.write(_insert)
    assert "gemini_ partner" in str(exc.value)

    rows = world.db.read("SELECT * FROM partner_paths WHERE partner_id=?", (world.code_partner["id"],))
    assert rows == []


def test_add_permissions_never_records_a_write_the_remote_silently_refused(world: World):
    """ATTACK: `StubExtension.permissions_refuse` makes the remote silently
    accept the add call without actually applying the rule -- exactly the
    failure `_apply_and_verify`'s read-back exists to catch. Held: refused
    `permission_not_applied`, and `partner_paths` gains no row even though
    nothing about the write itself looked wrong.
    """
    world.gem_ext.permissions_refuse.add("read_file(/secret)")
    with pytest.raises(Rejected) as exc:
        world.gem.add_permissions(
            requester_uuid=world.gem_orch["uuid"], partner_title="gemw", read_paths=["/secret"]
        )
    assert exc.value.code == "permission_not_applied"

    rows = world.db.read("SELECT * FROM partner_paths WHERE partner_id=?", (world.gemini_partner["id"],))
    assert rows == [], f"a refused permission write was recorded locally anyway: {[dict(r) for r in rows]!r}"


def test_delete_permissions_never_forgets_a_revoke_the_remote_silently_refused(world: World):
    """ATTACK: grant a path for real, then make the remote silently refuse to
    revoke it. Held: refused `permission_not_applied`, and the local record
    still shows the path as granted (it was never actually revoked).
    """
    world.gem.add_permissions(requester_uuid=world.gem_orch["uuid"], partner_title="gemw", read_paths=["/keep"])
    world.gem_ext.permissions_refuse.add("read_file(/keep)")

    with pytest.raises(Rejected) as exc:
        world.gem.delete_permissions(requester_uuid=world.gem_orch["uuid"], partner_title="gemw", paths=["/keep"])
    assert exc.value.code == "permission_not_applied"

    row = world.db.read_one(
        "SELECT * FROM partner_paths WHERE partner_id=? AND kind='read' AND path='/keep'", (world.gemini_partner["id"],)
    )
    assert row is not None, "a refused revoke was forgotten locally even though the remote still holds it"


def test_permission_tools_refuse_non_gemini_partners(world: World):
    """ATTACK: call all three permission tools against a science_ partner.
    Held: refused `not_path_configurable` for each; no `partner_paths` row.
    """
    for call in (
        lambda: world.sci.get_permissions(requester_uuid=world.orch["uuid"], partner_title="workerA"),
        lambda: world.sci.add_permissions(requester_uuid=world.orch["uuid"], partner_title="workerA", read_paths=["/x"]),
        lambda: world.sci.delete_permissions(requester_uuid=world.orch["uuid"], partner_title="workerA", paths=["/x"]),
    ):
        with pytest.raises(Rejected) as exc:
            call()
        assert exc.value.code == "not_path_configurable"
    rows = world.db.read("SELECT * FROM partner_paths WHERE partner_id=?", (world.worker["id"],))
    assert rows == []


# ===========================================================================
# Claim 8 -- Write discipline: one writer thread, OS-level read-only readers.
# ===========================================================================


def test_reader_connection_cannot_write_even_via_pragma_query_only_off(tmp_path):
    """ATTACK: an on-disk `Database` (the real deployment shape, not
    `:memory:`) opens reader connections as `file:{path}?mode=ro`. Try to
    write straight through the read API, then try to flip `PRAGMA
    query_only = OFF` through the SAME read API and write again on the same
    (thread-cached) reader connection. Held: the OS-level read-only file
    descriptor is not undone by the pragma -- both write attempts raise
    `sqlite3.OperationalError`, and nothing is persisted.
    """
    database = Database(path=str(tmp_path / "boundary.sqlite3"))
    try:
        with pytest.raises(Exception) as exc1:
            database.read(
                "INSERT INTO projects (source_prefix, project_system_id, title) VALUES ('code_', 'x', 'y')"
            )
        assert "readonly" in str(exc1.value).lower()

        # Flipping the pragma through the read API is not itself an error --
        # it is a per-connection setting, unrelated to the OS-level fd.
        database.read("PRAGMA query_only = OFF")

        with pytest.raises(Exception) as exc2:
            database.read(
                "INSERT INTO projects (source_prefix, project_system_id, title) VALUES ('code_', 'x2', 'y2')"
            )
        assert "readonly" in str(exc2.value).lower()

        count = database.read_one("SELECT COUNT(*) AS n FROM projects")["n"]
        assert count == 0, "a write reached the database through the read API"
    finally:
        database.close()


# ===========================================================================
# Claim 9 -- Approval doctrine: nothing anywhere answers a permission prompt.
# ===========================================================================


def _extension_classes() -> list[type]:
    classes = [RemoteExtension, NonExecutingExtension, StubExtension, AntigravityExtension]
    if ClaudeScienceExtension is not None:
        classes.append(ClaudeScienceExtension)
    if NotebookLMExtension is not None:
        classes.append(NotebookLMExtension)
    return classes


_PROMPT_ANSWERING_NAME_FRAGMENTS = ("answer", "approve", "respond_to_prompt", "grant_prompt", "confirm_prompt", "allow_prompt")


def test_no_extension_anywhere_implements_a_prompt_answering_method():
    """ATTACK (by absence): sweep every `RemoteExtension` in this codebase --
    the ABC itself, both bases, and every concrete adapter -- for any method
    whose name suggests it answers an interactive permission/approval
    prompt. Held: none exists. `get_permissions`/`add_permissions`/
    `delete_permissions` are a different concept (filesystem grants
    configured in advance) and are explicitly excluded from the name sweep.
    """
    offenders = []
    for cls in _extension_classes():
        for name in dir(cls):
            if name.startswith("_"):
                continue
            if any(fragment in name.lower() for fragment in _PROMPT_ANSWERING_NAME_FRAGMENTS):
                offenders.append(f"{cls.__name__}.{name}")
    assert offenders == [], f"found a prompt-answering method: {offenders!r}"


def test_antigravity_poll_completion_raises_rather_than_answering_a_prompt(monkeypatch):
    """ATTACK: feed `poll_completion` a pane capture that shows a real
    approval/permission prompt header. Held: it raises
    `Rejected("approval_is_an_error")` -- it never returns `True` (treating
    the prompt as "done") or `False` (treating it as ordinary "still busy"),
    either of which would silently let something proceed past an
    unanswered prompt.
    """
    ext = AntigravityExtension(tmux_path="/bin/true")

    def fake_tmux(*args):
        if args[0] == "capture-pane":
            return subprocess.CompletedProcess(args, returncode=0, stdout="requesting permission for: write_file(/x)\n", stderr="")
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ext, "_tmux", fake_tmux)

    with pytest.raises(Rejected) as exc:
        ext.poll_completion(partner_id_in_remote="abcdef12")
    assert exc.value.code == "approval_is_an_error"


# ===========================================================================
# Summary of what was attacked and held, for anyone re-running this file.
# ===========================================================================
#
# Claim 1 (cap):         held, incl. concurrent hammering, across an in-flight
#                        swap. report_back's admission path is exercised
#                        directly too: it refuses every non-reply label
#                        before admission runs at all, still performs real
#                        admission (storage + queue_depth) for the reply
#                        labels it does accept, and the cap MECHANISM itself
#                        (not just today's uncapped reply labels) is
#                        confirmed by temporarily capping [MESSAGE-RESPONSE].
# Claim 2 (priority):   held -- no ping-pong, no cross-label tie-break theft,
#                        [IDLE] never requeued after being displaced.
# Claim 3 (hierarchy):  held through send(). A prior finding against
#                        report_back (it admitted [RESEARCH] upward with
#                        none of send()'s checks) has been fixed upstream:
#                        report_back now refuses any behavior that is not
#                        some label's reply_behavior, closing that path.
#                        This file's test for it was converted from a
#                        pinned xfail into a normal passing assertion of the
#                        fix, swept across every non-reply label.
# Claim 4 (handshake):  held on every combination attacked, including two
#                        attacks that first had to exploit the claim_orchestrator
#                        gap noted below just to reach the specific check.
# Claim 5 (identity):   held -- title-as-uuid, archived-vs-unknown, the
#                        grant_gemini_budget ordering, and the uuid sweep.
# Claim 6 (archived):   held on every reachability/disclosure check AND on
#                        authorization. A prior test here asserted that
#                        archive_sessions should require project-orchestrator,
#                        based on a delete_partner comment claiming the two
#                        were scoped alike; that comment was wrong (and has
#                        been corrected) and the spec is explicit that
#                        archive_sessions takes only same-project scope, no
#                        role. That test has been replaced with one that
#                        attacks the rule the spec actually makes: a
#                        role-less partner CAN archive a live session in its
#                        own project, and CANNOT reach a different project's.
# Claim 7 (permissions): held, including the "remote silently refuses" case
#                        in both directions (grant and revoke).
# Claim 8 (write path):  held -- mode=ro survives PRAGMA query_only = OFF.
# Claim 9 (approval):    held -- confirmed by absence and by direct attack on
#                        the one extension with real prompt detection.
#
# Adjacent, out of scope, escalated rather than decided here: `claim_orchestrator`
# does not restrict "project-orchestrator"/"bridge-scientist" to a science_/
# code_ project the way it restricts "gemini-orchestrator" to science_ --
# a gemini_ partner can claim either role. This is a genuine spec ambiguity
# (the spec restricts only "gemini-orchestrator", and says nothing about the
# other two), not a settled rule either way, so no test in this file asserts
# a verdict on it in either direction -- it is deliberately left undecided
# here pending that escalation, not silently accepted. Every path this WAS
# checked against (gemini-gemini handshake, gemini->science handshake,
# budget granting, layer computation) turned out to be independently
# guarded regardless of how the ambiguity resolves, so no named boundary
# above is crossed by it either way.
