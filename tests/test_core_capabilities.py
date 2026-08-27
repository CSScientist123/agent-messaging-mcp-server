"""Tests for messaging_core.core.MessagingCore against the current (single-priority-queue)
messaging model.

The old model this file used to encode -- forward/backward queues, causal roles
(role="opens"/"continues"), polling_tasks, open_issues, resume_partner -- is gone. There is
ONE priority queue per partner (`message_queue`), and every message, `send`,
`interrupt_partner`, and the drain thread's own promotions all go through it via `advance`.
This file is rewritten from scratch against `messaging_core/core.py`, which is the sole
authority for every code and every ordering claim below -- not `docs/`, which describes the
old model and is being rewritten separately.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from extension.base import StubExtension
from messaging_core import templates
from messaging_core.core import MessagingCore
from messaging_core.db import Database
from messaging_core.errors import NeedsRemote, Rejected


# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    database = Database(path=":memory:")
    yield database
    database.close()


@pytest.fixture
def core(db):
    return MessagingCore(db)


def set_ext(core: MessagingCore, prefix: str = "science_") -> StubExtension:
    """Point `core` at a fresh StubExtension for `prefix` and return it.

    A single MessagingCore holds at most one extension at a time (see the
    module docstring in core.py), so any capability that needs to reach a
    Partner in a Project of a DIFFERENT source_prefix than whatever is
    currently configured must have this called again first.
    """
    e = StubExtension(source_prefix=prefix)
    core.extension = e
    return e


def mk_project(core: MessagingCore, *, title: str, source_prefix: str, system_id: str) -> int:
    set_ext(core, source_prefix)
    return core.create_project(title=title, source_prefix=source_prefix, project_system_id=system_id)


def mk_partner(
    core: MessagingCore, project: dict, *, title: str, remote_id: str, descr: str = "d"
) -> dict:
    set_ext(core, project["source_prefix"])
    result = core.create_partner(
        project_id=project["id"], title=title, partner_id_in_remote=remote_id, descr=descr
    )
    # create_partner's own return value deliberately does not echo
    # partner_id_in_remote back; stash it here purely so test helpers that
    # need to reach into the stub's permission/call records by that key
    # do not have to re-query the database for it.
    result["partner_id_in_remote"] = remote_id
    return result


def push_raw(
    db: Database, *, partner_id: int, caller_id: int, behavior: str, body: str, in_process: int = 0
) -> None:
    """Insert directly into `message_queue`, bypassing `send`'s admission checks.

    Used only by the advance()/swap-rule tests below, which need precise
    control over priority and in_process without fighting handshake or
    queue-cap setup that has nothing to do with what they are testing.
    """

    def _ins(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO message_queue (partner_id, caller_id, behavior, body, in_process) "
            "VALUES (?, ?, ?, ?, ?)",
            (partner_id, caller_id, behavior, body, in_process),
        )

    db.write(_ins)


def priority_of(db: Database, behavior: str) -> int:
    return db.read_one("SELECT priority FROM label_caps WHERE behavior = ?", (behavior,))["priority"]


def queue_count(
    db: Database, partner_id: int, *, caller_id: int | None = None, behavior: str | None = None
) -> int:
    sql = "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id = ?"
    params: list = [partner_id]
    if caller_id is not None:
        sql += " AND caller_id = ?"
        params.append(caller_id)
    if behavior is not None:
        sql += " AND behavior = ?"
        params.append(behavior)
    return db.read_one(sql, params)["n"]


@pytest.fixture
def world(core: MessagingCore) -> dict:
    """One science_ Project holding a project-orchestrator, a gemini-orchestrator, a
    bridge-scientist and a plain (roleless) worker, plus one sibling Project per other
    source -- gemini_, nlm_, code_ -- since a Partner's type is its Project's
    source_prefix and a Project has exactly one (see core.py's module docstring)."""
    pid = mk_project(core, title="Alpha", source_prefix="science_", system_id="sess-1")
    project = {"id": pid, "source_prefix": "science_"}

    orch = mk_partner(core, project, title="orchestrator", remote_id="r-orch")
    core.claim_orchestrator(
        requester_uuid=orch["uuid"], project_id=pid, orchestrator_type="project-orchestrator"
    )

    gem_orch = mk_partner(core, project, title="gemini-liaison", remote_id="r-gemorch")
    core.claim_orchestrator(
        requester_uuid=gem_orch["uuid"], project_id=pid, orchestrator_type="gemini-orchestrator"
    )

    bridge = mk_partner(core, project, title="bridge", remote_id="r-bridge")
    core.claim_orchestrator(
        requester_uuid=bridge["uuid"], project_id=pid, orchestrator_type="bridge-scientist"
    )

    worker = mk_partner(core, project, title="lit-review", remote_id="r-worker")

    gemini_pid = mk_project(core, title="GeminiWing", source_prefix="gemini_", system_id="gsess-1")
    gemini_project = {"id": gemini_pid, "source_prefix": "gemini_"}
    gemini_partner = mk_partner(core, gemini_project, title="gemini-worker", remote_id="r-gemini")

    nlm_pid = mk_project(core, title="Notebook", source_prefix="nlm_", system_id="nsess-1")
    nlm_project = {"id": nlm_pid, "source_prefix": "nlm_"}
    nlm_partner = mk_partner(core, nlm_project, title="notebook-source", remote_id="r-nlm")

    code_pid = mk_project(core, title="Repo", source_prefix="code_", system_id="csess-1")
    code_project = {"id": code_pid, "source_prefix": "code_"}
    code_partner = mk_partner(core, code_project, title="ci-agent", remote_id="r-code")

    core.grant_gemini_budget(
        requester_uuid=orch["uuid"], grantee_uuid=gem_orch["uuid"], budget_count=2
    )

    return {
        "project_id": pid,
        "project": project,
        "orch": orch,
        "gem_orch": gem_orch,
        "bridge": bridge,
        "worker": worker,
        "gemini_project": gemini_project,
        "gemini_partner": gemini_partner,
        "nlm_project": nlm_project,
        "nlm_partner": nlm_partner,
        "code_project": code_project,
        "code_partner": code_partner,
    }


# ---------------------------------------------------------------------------
# create_project
# ---------------------------------------------------------------------------


def test_create_project_success(core):
    set_ext(core, "code_")
    pid = core.create_project(title="MyRepo", source_prefix="code_", project_system_id="repo-1")
    assert isinstance(pid, int), f"expected an int project id, got {pid!r}"


def test_create_project_needs_remote_without_extension(core):
    with pytest.raises(NeedsRemote):
        core.create_project(title="MyRepo", source_prefix="code_", project_system_id="repo-1")


def test_create_project_rejects_duplicate_title(core):
    set_ext(core, "code_")
    core.create_project(title="MyRepo", source_prefix="code_", project_system_id="repo-1")
    with pytest.raises(Rejected) as exc:
        core.create_project(title="MyRepo", source_prefix="code_", project_system_id="repo-2")
    assert exc.value.code == "duplicate_project_title", f"expected duplicate_project_title, got {exc.value.code!r}"


def test_create_project_rejects_duplicate_system_id_pair(core):
    set_ext(core, "code_")
    core.create_project(title="RepoA", source_prefix="code_", project_system_id="repo-1")
    with pytest.raises(Rejected) as exc:
        core.create_project(title="RepoB", source_prefix="code_", project_system_id="repo-1")
    assert exc.value.code == "duplicate_project_system_id", (
        f"expected duplicate_project_system_id, got {exc.value.code!r}"
    )


def test_create_project_rejects_invalid_source_prefix(core):
    with pytest.raises(Rejected) as exc:
        core.create_project(title="X", source_prefix="bogus_", project_system_id="1")
    assert exc.value.code == "invalid_source_prefix", f"expected invalid_source_prefix, got {exc.value.code!r}"


def test_create_project_rejects_when_remote_verification_fails(core):
    e = StubExtension(source_prefix="code_")
    e.verify_project_system_id_result = False
    core.extension = e
    with pytest.raises(Rejected) as exc:
        core.create_project(title="X", source_prefix="code_", project_system_id="nope")
    assert exc.value.code == "project_system_id_not_found", (
        f"expected project_system_id_not_found, got {exc.value.code!r}"
    )


# ---------------------------------------------------------------------------
# create_partner
# ---------------------------------------------------------------------------


def test_create_partner_success(core):
    pid = mk_project(core, title="Alpha", source_prefix="science_", system_id="s1")
    result = mk_partner(core, {"id": pid, "source_prefix": "science_"}, title="surveyor", remote_id="r1")
    assert result["title"] == "surveyor", f"expected title 'surveyor', got {result!r}"
    assert result.get("uuid"), f"expected a non-empty uuid, got {result!r}"


def test_create_partner_rejects_duplicate_remote_id(core):
    pid = mk_project(core, title="Alpha", source_prefix="science_", system_id="s1")
    project = {"id": pid, "source_prefix": "science_"}
    mk_partner(core, project, title="surveyor-a", remote_id="dup")
    set_ext(core, "science_")
    with pytest.raises(Rejected) as exc:
        core.create_partner(project_id=pid, title="surveyor-b", partner_id_in_remote="dup", descr="d")
    assert exc.value.code == "partner_id_in_remote_taken", (
        f"expected partner_id_in_remote_taken, got {exc.value.code!r}"
    )


def test_create_partner_concurrent_same_remote_id_exactly_one_wins(db):
    """N threads race to create a partner in the SAME project with the SAME
    partner_id_in_remote. The read-then-insert pre-check is only a fast path;
    the schema's UNIQUE (project_id, partner_id_in_remote) constraint is what
    actually settles the race."""
    core = MessagingCore(db, StubExtension(source_prefix="science_"))
    project_id = core.create_project(
        title="RemoteIdRaceProject", source_prefix="science_", project_system_id="race-remote-id"
    )
    N = 12
    results: list[tuple[str, object]] = []
    lock = threading.Lock()

    def attempt(i: int) -> None:
        try:
            r = core.create_partner(
                project_id=project_id, title=f"cand-{i}", partner_id_in_remote="dup-remote", descr="d"
            )
            with lock:
                results.append(("ok", r["id"]))
        except Rejected as e:
            with lock:
                results.append(("rejected", e.code))

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [r for r in results if r[0] == "ok"]
    assert len(oks) == 1, f"expected exactly one winner of the race, got {results!r}"
    assert all(code == "partner_id_in_remote_taken" for kind, code in results if kind == "rejected"), (
        f"every loser must be refused with partner_id_in_remote_taken, got {results!r}"
    )
    survivors = db.read(
        "SELECT id FROM partners WHERE project_id = ? AND partner_id_in_remote = 'dup-remote'",
        (project_id,),
    )
    assert len(survivors) == 1, f"exactly one row may hold this (project, remote id) pair, got {len(survivors)}"


def test_create_partner_needs_remote_without_extension(core):
    pid = mk_project(core, title="Alpha", source_prefix="science_", system_id="s1")
    core.extension = None
    with pytest.raises(NeedsRemote):
        core.create_partner(project_id=pid, title="surveyor", partner_id_in_remote="r1", descr="d")


def test_create_partner_title_unique_across_different_projects(core):
    pid1 = mk_project(core, title="Alpha", source_prefix="science_", system_id="s1")
    pid2 = mk_project(core, title="Beta", source_prefix="code_", system_id="s2")
    mk_partner(core, {"id": pid1, "source_prefix": "science_"}, title="shared-title", remote_id="r1")
    with pytest.raises(Rejected) as exc:
        mk_partner(core, {"id": pid2, "source_prefix": "code_"}, title="shared-title", remote_id="r2")
    assert exc.value.code == "duplicate_partner_title", (
        f"expected duplicate_partner_title, got {exc.value.code!r}"
    )


def test_create_partner_live_partner_limit(core):
    pid = mk_project(core, title="Ceiling", source_prefix="code_", system_id="s1")
    project = {"id": pid, "source_prefix": "code_"}
    for i in range(10):
        mk_partner(core, project, title=f"worker-{i}", remote_id=f"r{i}")
    with pytest.raises(Rejected) as exc:
        mk_partner(core, project, title="overflow-worker", remote_id="r-overflow")
    assert exc.value.code == "live_partner_limit", f"expected live_partner_limit, got {exc.value.code!r}"


# ---------------------------------------------------------------------------
# search_partner / search_project
# ---------------------------------------------------------------------------


def test_search_partner_success(core, world):
    results = core.search_partner(requester_uuid=world["orch"]["uuid"], query_title="lit-rev")
    assert results, "expected at least one search result"
    assert results[0]["title"] == "lit-review", f"expected the closest match to be 'lit-review', got {results[0]!r}"
    assert "uuid" not in results[0], f"search_partner must never expose a uuid, got {results[0]!r}"


def test_search_partner_excludes_archived(core, world):
    core.archive_sessions(requester_uuid=world["orch"]["uuid"], titles=["lit-review"])
    results = core.search_partner(requester_uuid=world["orch"]["uuid"], query_title="lit-review")
    titles = [r["title"] for r in results]
    assert "lit-review" not in titles, f"expected archived partner excluded, got {titles!r}"


def test_search_partner_rejects_unknown_requester(core):
    with pytest.raises(Rejected) as exc:
        core.search_partner(requester_uuid="not-a-real-uuid", query_title="x")
    assert exc.value.code == "unknown_requester", f"expected unknown_requester, got {exc.value.code!r}"


def test_search_project_success(core, world):
    results = core.search_project(requester_uuid=world["orch"]["uuid"], query_title="Alpha")
    assert results and results[0]["title"] == "Alpha", f"expected 'Alpha' as top match, got {results!r}"


def test_search_project_rejects_unknown_requester(core):
    with pytest.raises(Rejected) as exc:
        core.search_project(requester_uuid="nope", query_title="x")
    assert exc.value.code == "unknown_requester", f"expected unknown_requester, got {exc.value.code!r}"


# ---------------------------------------------------------------------------
# delete_partner / delete_project
# ---------------------------------------------------------------------------


def test_delete_partner_success(core, world):
    result = core.delete_partner(requester_uuid=world["orch"]["uuid"], partner_title="lit-review")
    assert result["title"] == "lit-review", f"expected title 'lit-review', got {result!r}"
    with pytest.raises(Rejected):
        core._resolve_live_partner_by_title("lit-review")


def test_delete_partner_rejects_unknown_title(core, world):
    with pytest.raises(Rejected) as exc:
        core.delete_partner(requester_uuid=world["orch"]["uuid"], partner_title="ghost-partner")
    assert exc.value.code == "no_such_partner", f"expected no_such_partner, got {exc.value.code!r}"


def test_delete_partner_rejects_when_it_has_dependents(core, world):
    with pytest.raises(Rejected) as exc:
        core.delete_partner(requester_uuid=world["orch"]["uuid"], partner_title="orchestrator")
    assert exc.value.code == "partner_has_dependents", f"expected partner_has_dependents, got {exc.value.code!r}"


def test_delete_project_success(core, world):
    before = core.db.read_one(
        "SELECT COUNT(*) AS n FROM partners WHERE project_id = ?", (world["project_id"],)
    )["n"]
    result = core.delete_project(requester_uuid=world["orch"]["uuid"], project_title="Alpha")
    assert result["title"] == "Alpha", f"expected title 'Alpha', got {result!r}"
    assert result["partners_deleted"] == before, (
        f"expected partners_deleted == {before}, got {result['partners_deleted']}"
    )


def test_delete_project_rejects_unknown_title(core, world):
    with pytest.raises(Rejected) as exc:
        core.delete_project(requester_uuid=world["orch"]["uuid"], project_title="Nonexistent")
    assert exc.value.code == "no_such_project", f"expected no_such_project, got {exc.value.code!r}"


# ---------------------------------------------------------------------------
# claim_orchestrator
# ---------------------------------------------------------------------------


def test_claim_orchestrator_success(core):
    pid = mk_project(core, title="Bridge", source_prefix="science_", system_id="s1")
    p = mk_partner(core, {"id": pid, "source_prefix": "science_"}, title="bridge-worker", remote_id="r1")
    result = core.claim_orchestrator(requester_uuid=p["uuid"], project_id=pid, orchestrator_type="bridge-scientist")
    assert result["orchestrator_type"] == "bridge-scientist", f"expected 'bridge-scientist', got {result!r}"


def test_claim_orchestrator_rejects_double_claim_same_project(core):
    pid = mk_project(core, title="Bridge", source_prefix="science_", system_id="s1")
    project = {"id": pid, "source_prefix": "science_"}
    p1 = mk_partner(core, project, title="first-claim", remote_id="r1")
    p2 = mk_partner(core, project, title="second-claim", remote_id="r2")
    core.claim_orchestrator(requester_uuid=p1["uuid"], project_id=pid, orchestrator_type="project-orchestrator")
    with pytest.raises(Rejected) as exc:
        core.claim_orchestrator(requester_uuid=p2["uuid"], project_id=pid, orchestrator_type="project-orchestrator")
    assert exc.value.code == "role_already_claimed", f"expected role_already_claimed, got {exc.value.code!r}"


def test_claim_orchestrator_rejects_reassignment(core, world):
    with pytest.raises(Rejected) as exc:
        core.claim_orchestrator(
            requester_uuid=world["orch"]["uuid"],
            project_id=world["project_id"],
            orchestrator_type="bridge-scientist",
        )
    assert exc.value.code == "already_has_role", f"expected already_has_role, got {exc.value.code!r}"


@pytest.mark.parametrize("role", ["project-orchestrator", "gemini-orchestrator", "bridge-scientist"])
@pytest.mark.parametrize("source", ["code_", "gemini_", "nlm_"])
def test_every_orchestrator_role_is_claude_science_only(core, role, source):
    """All THREE roles belong to Claude Science, not just gemini-orchestrator.

    The roles are named after what they orchestrate, not what holds them: a
    gemini-orchestrator is a Claude Science agent that directs Antigravity, and
    is never an Antigravity agent itself. An earlier version restricted only
    that one role, leaving an Antigravity or NotebookLM partner free to claim
    project-orchestrator or bridge-scientist. Nothing crossed a boundary because
    of it -- every path was independently guarded -- but "currently unreachable"
    is not a rule.
    """
    pid = mk_project(core, title=f"P-{source}-{role}", source_prefix=source,
                     system_id=f"s-{source}-{role}")
    p = mk_partner(core, {"id": pid, "source_prefix": source},
                   title=f"w-{source}-{role}", remote_id=f"r-{source}-{role}")
    with pytest.raises(Rejected) as exc:
        core.claim_orchestrator(requester_uuid=p["uuid"], project_id=pid, orchestrator_type=role)
    assert exc.value.code == "orchestrator_requires_science_project", (
        f"a {source} partner claimed {role!r}; expected "
        f"orchestrator_requires_science_project, got {exc.value.code!r}"
    )


@pytest.mark.parametrize("role", ["project-orchestrator", "gemini-orchestrator", "bridge-scientist"])
def test_every_orchestrator_role_is_claimable_inside_a_science_project(core, role):
    """The other half: the rule restricts the source, not the roles themselves."""
    pid = mk_project(core, title=f"Sci-{role}", source_prefix="science_", system_id=f"sci-{role}")
    p = mk_partner(core, {"id": pid, "source_prefix": "science_"},
                   title=f"sci-{role}", remote_id=f"rs-{role}")
    result = core.claim_orchestrator(requester_uuid=p["uuid"], project_id=pid, orchestrator_type=role)
    assert result["orchestrator_type"] == role, (
        f"a science_ partner could not claim {role!r}: {result!r}"
    )


# ---------------------------------------------------------------------------
# grant_gemini_budget
# ---------------------------------------------------------------------------


def test_grant_gemini_budget_success(core, world):
    result = core.grant_gemini_budget(
        requester_uuid=world["orch"]["uuid"], grantee_uuid=world["gem_orch"]["uuid"], budget_count=3
    )
    assert result["budget_count"] == 3, f"expected budget_count 3, got {result!r}"


def test_grant_gemini_budget_rejects_out_of_range(core, world):
    with pytest.raises(Rejected) as exc:
        core.grant_gemini_budget(
            requester_uuid=world["orch"]["uuid"], grantee_uuid=world["gem_orch"]["uuid"], budget_count=4
        )
    assert exc.value.code == "invalid_budget_count", f"expected invalid_budget_count, got {exc.value.code!r}"


def test_grant_gemini_budget_rejects_non_gemini_orchestrator_grantee(core, world):
    with pytest.raises(Rejected) as exc:
        core.grant_gemini_budget(
            requester_uuid=world["orch"]["uuid"], grantee_uuid=world["worker"]["uuid"], budget_count=1
        )
    assert exc.value.code == "grantee_not_gemini_orchestrator", (
        f"expected grantee_not_gemini_orchestrator, got {exc.value.code!r}"
    )


def test_grant_gemini_budget_rejects_unauthorized_requester(core, world):
    with pytest.raises(Rejected) as exc:
        core.grant_gemini_budget(
            requester_uuid=world["worker"]["uuid"], grantee_uuid=world["gem_orch"]["uuid"], budget_count=1
        )
    assert exc.value.code == "not_authorized", f"expected not_authorized, got {exc.value.code!r}"


# ---------------------------------------------------------------------------
# archive_sessions
# ---------------------------------------------------------------------------


def test_archive_sessions_success(core, world):
    result = core.archive_sessions(requester_uuid=world["orch"]["uuid"], titles=["lit-review"])
    assert result["archived"] == ["lit-review"], f"expected ['lit-review'], got {result!r}"
    assert result["archived_count"] == 1, f"expected archived_count 1, got {result!r}"


def test_archive_sessions_skips_unknown_and_cross_project_titles(core, world):
    other_pid = mk_project(core, title="Other", source_prefix="code_", system_id="other-1")
    mk_partner(core, {"id": other_pid, "source_prefix": "code_"}, title="other-worker", remote_id="ro1")
    result = core.archive_sessions(
        requester_uuid=world["orch"]["uuid"], titles=["ghost-partner", "other-worker"]
    )
    assert result["archived"] == [], f"expected nothing archived, got {result!r}"
    reasons = {s["title"]: s["reason"] for s in result["skipped"]}
    assert reasons["ghost-partner"] == "not_found_or_already_archived", f"got {reasons!r}"
    assert reasons["other-worker"] == "different_project", f"got {reasons!r}"


def test_archive_frees_the_live_partner_limit(core):
    # science_, because the orchestrator role this needs is Claude Science only.
    pid = mk_project(core, title="Ceiling2", source_prefix="science_", system_id="s1")
    project = {"id": pid, "source_prefix": "science_"}
    orch = mk_partner(core, project, title="orchestrator", remote_id="r-orch")
    core.claim_orchestrator(requester_uuid=orch["uuid"], project_id=pid, orchestrator_type="project-orchestrator")
    for i in range(9):
        mk_partner(core, project, title=f"worker-{i}", remote_id=f"r{i}")
    with pytest.raises(Rejected):
        mk_partner(core, project, title="overflow-worker", remote_id="r-overflow")
    core.archive_sessions(requester_uuid=orch["uuid"], titles=["worker-0"])
    mk_partner(core, project, title="newcomer", remote_id="r-newcomer")  # must not raise


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_success_reports_identity_and_layer(core, world):
    result = core.status(requester_uuid=world["orch"]["uuid"])
    assert result["title"] == "orchestrator", f"expected title 'orchestrator', got {result['title']!r}"
    assert result["orchestrator_type"] == "project-orchestrator", (
        f"expected orchestrator_type 'project-orchestrator', got {result['orchestrator_type']!r}"
    )
    assert result["project_title"] == "Alpha", f"expected project_title 'Alpha', got {result['project_title']!r}"
    assert result["layer"] == 2, f"expected layer 2 for project-orchestrator, got {result['layer']}"
    assert result["working"] is None, f"expected working=None with an empty queue, got {result['working']!r}"
    assert result["queued"] == [], f"expected an empty queued list, got {result['queued']!r}"
    assert result["queue_depth"] == 0, f"expected queue_depth 0, got {result['queue_depth']}"


def test_status_reports_queued_breakdown_highest_priority_first_and_paused(core, world):
    core.handshake(requester_uuid=world["orch"]["uuid"], partner_title="lit-review")
    set_ext(core, "science_")
    target = world["worker"]

    core.send(requester_uuid=world["orch"]["uuid"], queried_partner_title="lit-review",
              message="q1", behavior="[QUERY]")
    # Strictly higher priority: displaces the [QUERY], which becomes paused.
    core.send(requester_uuid=world["orch"]["uuid"], queried_partner_title="lit-review",
              message="tr1", behavior="[TRUTHFUL-REPORT]")
    # Lower priority than the working [TRUTHFUL-REPORT]: stays queued, fresh.
    core.send(requester_uuid=world["orch"]["uuid"], queried_partner_title="lit-review",
              message="r1", behavior="[RESEARCH]")
    core.send(requester_uuid=world["orch"]["uuid"], queried_partner_title="lit-review",
              message="mr1", behavior="[MESSAGE-RESPONSE]")

    result = core.status(requester_uuid=target["uuid"])
    assert result["working"] is not None and result["working"]["behavior"] == "[TRUTHFUL-REPORT]", (
        f"expected the working task to be [TRUTHFUL-REPORT], got {result['working']!r}"
    )
    assert result["working"]["resumed"] is False, (
        f"the fresh [TRUTHFUL-REPORT] must not be reported as resumed, got {result['working']!r}"
    )
    assert result["queue_depth"] == 3, f"expected queue_depth 3, got {result['queue_depth']}"

    behaviors_in_order = [q["behavior"] for q in result["queued"]]
    assert behaviors_in_order == ["[QUERY]", "[MESSAGE-RESPONSE]", "[RESEARCH]"], (
        f"expected queued ordered highest-priority-first (2, 3, 4), got {behaviors_in_order} "
        f"from {result['queued']!r}"
    )
    by_behavior = {q["behavior"]: q for q in result["queued"]}
    assert by_behavior["[QUERY]"]["paused"] == 1, (
        f"the displaced [QUERY] must be reported paused, got {by_behavior['[QUERY]']!r}"
    )
    assert by_behavior["[QUERY]"]["count"] == 1, f"expected count 1, got {by_behavior['[QUERY]']!r}"
    assert by_behavior["[RESEARCH]"]["paused"] == 0, (
        f"the fresh [RESEARCH] must not be reported paused, got {by_behavior['[RESEARCH]']!r}"
    )
    assert by_behavior["[MESSAGE-RESPONSE]"]["priority"] == priority_of(core.db, "[MESSAGE-RESPONSE]"), (
        f"expected the correct priority reported, got {by_behavior['[MESSAGE-RESPONSE]']!r}"
    )


def test_status_reports_gemini_budget(core, world):
    result = core.status(requester_uuid=world["gem_orch"]["uuid"])
    assert result["gemini_budget"] == {"budget_count": 2, "used": 0}, (
        f"expected budget_count=2, used=0, got {result['gemini_budget']!r}"
    )
    core.handshake(requester_uuid=world["gem_orch"]["uuid"], partner_title="gemini-worker")
    result2 = core.status(requester_uuid=world["gem_orch"]["uuid"])
    assert result2["gemini_budget"] == {"budget_count": 2, "used": 1}, (
        f"expected used to rise to 1 after one gemini_ handshake, got {result2['gemini_budget']!r}"
    )


def test_status_gemini_budget_is_none_for_non_gemini_orchestrator(core, world):
    result = core.status(requester_uuid=world["orch"]["uuid"])
    assert result["gemini_budget"] is None, f"expected gemini_budget=None, got {result['gemini_budget']!r}"


def test_status_rejects_unknown_requester(core):
    with pytest.raises(Rejected) as exc:
        core.status(requester_uuid="nope")
    assert exc.value.code == "unknown_requester", f"expected unknown_requester, got {exc.value.code!r}"


# ---------------------------------------------------------------------------
# handshake
# ---------------------------------------------------------------------------


def test_handshake_code_bridge_only_rejects_plain_science_target(core, world):
    with pytest.raises(Rejected) as exc:
        core.handshake(requester_uuid=world["code_partner"]["uuid"], partner_title="lit-review")
    assert exc.value.code == "code_handshakes_bridge_only", (
        f"expected code_handshakes_bridge_only, got {exc.value.code!r}"
    )


def test_handshake_code_bridge_only_rejects_non_bridge_requester(core, world):
    with pytest.raises(Rejected) as exc:
        core.handshake(requester_uuid=world["orch"]["uuid"], partner_title="ci-agent")
    assert exc.value.code == "code_handshakes_bridge_only", (
        f"expected code_handshakes_bridge_only, got {exc.value.code!r}"
    )


def test_handshake_bridge_to_code_is_legal(core, world):
    result = core.handshake(requester_uuid=world["bridge"]["uuid"], partner_title="ci-agent")
    assert result["to_partner_title"] == "ci-agent", f"expected to_partner_title 'ci-agent', got {result!r}"


def test_handshake_code_requester_exempt_from_orchestrator_requirement(core, world):
    """A code_ requester holding NO role at all may still handshake a
    bridge-scientist -- this is the entire reason the exemption exists.
    world['code_partner'] is created with no claim_orchestrator call, so its
    orchestrator_type is None; without the exemption this would be refused
    as `requester_not_orchestrator`.
    """
    role_row = core.db.read_one(
        "SELECT orchestrator_type FROM partners WHERE id = ?", (world["code_partner"]["id"],)
    )
    assert role_row["orchestrator_type"] is None, (
        f"precondition: ci-agent must hold no role, got {role_row['orchestrator_type']!r}"
    )
    result = core.handshake(requester_uuid=world["code_partner"]["uuid"], partner_title="bridge")
    assert result["to_partner_title"] == "bridge", (
        f"expected the roleless code_ requester to succeed, got {result!r}"
    )


def test_handshake_bridge_single_code_partner(core, world):
    core.handshake(requester_uuid=world["bridge"]["uuid"], partner_title="ci-agent")
    other_code_pid = mk_project(core, title="OtherRepo", source_prefix="code_", system_id="c-2")
    other_code = mk_partner(
        core, {"id": other_code_pid, "source_prefix": "code_"}, title="second-ci", remote_id="r-code2"
    )

    with pytest.raises(Rejected) as exc:
        core.handshake(requester_uuid=world["bridge"]["uuid"], partner_title="second-ci")
    assert exc.value.code == "bridge_single_code_partner", (
        f"expected bridge_single_code_partner, got {exc.value.code!r}"
    )
    with pytest.raises(Rejected) as exc2:
        core.handshake(requester_uuid=other_code["uuid"], partner_title="bridge")
    assert exc2.value.code == "bridge_single_code_partner", (
        f"expected bridge_single_code_partner from the reverse direction too, got {exc2.value.code!r}"
    )
    total = core.db.read_one("SELECT COUNT(*) AS n FROM handshakes")["n"]
    assert total == 1, f"only the original bridge<->ci-agent handshake should exist, found {total} rows"


def test_handshake_bridge_may_only_reach_project_orchestrator(core, world):
    with pytest.raises(Rejected) as exc:
        core.handshake(requester_uuid=world["bridge"]["uuid"], partner_title="lit-review")
    assert exc.value.code == "bridge_handshakes_orchestrator_or_code", (
        f"expected bridge_handshakes_orchestrator_or_code, got {exc.value.code!r}"
    )
    result = core.handshake(requester_uuid=world["bridge"]["uuid"], partner_title="orchestrator")
    assert result["to_partner_title"] == "orchestrator", f"bridge -> project-orchestrator must succeed, got {result!r}"


def test_handshake_requires_project_orchestrator_among_science_partners(core, world):
    with pytest.raises(Rejected) as exc:
        core.handshake(requester_uuid=world["gem_orch"]["uuid"], partner_title="lit-review")
    assert exc.value.code == "requires_project_orchestrator", (
        f"expected requires_project_orchestrator, got {exc.value.code!r}"
    )


def test_a_gemini_partner_can_never_initiate_a_handshake(core):
    """Two gemini_ partners cannot pair -- and the reason changed.

    It used to be `no_handshake_between_gemini`, reached by first giving the
    requester a role. Now that every orchestrator role is Claude Science only, a
    gemini_ partner can hold no role at all, so it never clears the generic
    orchestrator gate and is refused earlier.

    `no_handshake_between_gemini` therefore became **unreachable through the tool
    surface**. It is deliberately kept in `handshake` as defence in depth -- it
    still fires against a role written straight into the database, bypassing
    `claim_orchestrator` -- but the rule an agent actually meets is this one.
    """
    pid = mk_project(core, title="GeminiPod", source_prefix="gemini_", system_id="g1")
    project = {"id": pid, "source_prefix": "gemini_"}
    g1 = mk_partner(core, project, title="gemini-one", remote_id="r-g1")
    mk_partner(core, project, title="gemini-two", remote_id="r-g2")

    with pytest.raises(Rejected) as exc:
        core.claim_orchestrator(requester_uuid=g1["uuid"], project_id=pid,
                                orchestrator_type="bridge-scientist")
    assert exc.value.code == "orchestrator_requires_science_project"

    with pytest.raises(Rejected) as exc:
        core.handshake(requester_uuid=g1["uuid"], partner_title="gemini-two")
    assert exc.value.code == "requester_not_orchestrator", (
        f"expected requester_not_orchestrator, got {exc.value.code!r}"
    )


def test_no_handshake_between_gemini_still_fires_if_a_role_is_forced_in(core, db):
    """The deeper check is kept, and this proves it is not dead code.

    Written straight into the database, bypassing `claim_orchestrator` entirely --
    which is the only way to reach it now, and exactly the code path a rule
    enforced solely in one capability would miss.
    """
    pid = mk_project(core, title="GeminiPod2", source_prefix="gemini_", system_id="g2")
    project = {"id": pid, "source_prefix": "gemini_"}
    g1 = mk_partner(core, project, title="forced-one", remote_id="r-f1")
    mk_partner(core, project, title="forced-two", remote_id="r-f2")
    db.write(lambda c: c.execute(
        "UPDATE partners SET orchestrator_type='bridge-scientist' WHERE uuid=?", (g1["uuid"],)))

    with pytest.raises(Rejected) as exc:
        core.handshake(requester_uuid=g1["uuid"], partner_title="forced-two")
    assert exc.value.code == "no_handshake_between_gemini", (
        f"expected no_handshake_between_gemini, got {exc.value.code!r}"
    )


def test_handshake_gemini_to_science_illegal(core, world, db):
    """gemini_ -> science_ is never legal, and like the gemini-gemini rule it is
    now reached only by forcing a role in directly.

    Through the tool surface a gemini_ partner is stopped one step earlier, at
    `requester_not_orchestrator`, because no orchestrator role can be claimed
    outside Claude Science. Both routes are asserted here: the one an agent
    meets, and the one that proves the deeper check is still live.
    """
    pid = mk_project(core, title="GeminiSolo", source_prefix="gemini_", system_id="g2")
    project = {"id": pid, "source_prefix": "gemini_"}
    g = mk_partner(core, project, title="gemini-solo", remote_id="r-gsolo")

    with pytest.raises(Rejected) as exc:
        core.handshake(requester_uuid=g["uuid"], partner_title="orchestrator")
    assert exc.value.code == "requester_not_orchestrator", (
        f"through the tools, expected requester_not_orchestrator, got {exc.value.code!r}"
    )

    db.write(lambda c: c.execute(
        "UPDATE partners SET orchestrator_type='bridge-scientist' WHERE uuid=?", (g["uuid"],)))
    with pytest.raises(Rejected) as exc:
        core.handshake(requester_uuid=g["uuid"], partner_title="orchestrator")
    assert exc.value.code == "gemini_to_science_illegal", (
        f"with the role forced in, expected gemini_to_science_illegal, got {exc.value.code!r}"
    )


def test_handshake_requires_gemini_orchestrator(core, world):
    with pytest.raises(Rejected) as exc:
        core.handshake(requester_uuid=world["orch"]["uuid"], partner_title="gemini-worker")
    assert exc.value.code == "requires_gemini_orchestrator", (
        f"expected requires_gemini_orchestrator, got {exc.value.code!r}"
    )


def test_handshake_no_gemini_budget(core, world):
    """A fresh gemini-orchestrator with no budget grant, reaching a gemini_
    partner no other science_ source has ever reached -- isolates
    no_gemini_budget from gemini_single_science_source, which is checked
    first in the source."""
    pid = mk_project(core, title="Beta", source_prefix="science_", system_id="s-beta")
    project = {"id": pid, "source_prefix": "science_"}
    orch2 = mk_partner(core, project, title="beta-orchestrator", remote_id="r-beta-orch")
    core.claim_orchestrator(requester_uuid=orch2["uuid"], project_id=pid, orchestrator_type="project-orchestrator")
    gem2 = mk_partner(core, project, title="beta-gem-orch", remote_id="r-beta-gem")
    core.claim_orchestrator(requester_uuid=gem2["uuid"], project_id=pid, orchestrator_type="gemini-orchestrator")

    gemini_pid2 = mk_project(core, title="FreshGeminiWing", source_prefix="gemini_", system_id="g-fresh")
    mk_partner(core, {"id": gemini_pid2, "source_prefix": "gemini_"}, title="fresh-gemini", remote_id="r-fresh")

    with pytest.raises(Rejected) as exc:
        core.handshake(requester_uuid=gem2["uuid"], partner_title="fresh-gemini")
    assert exc.value.code == "no_gemini_budget", f"expected no_gemini_budget, got {exc.value.code!r}"


def test_handshake_gemini_budget_exceeded(core, world):
    core.grant_gemini_budget(
        requester_uuid=world["orch"]["uuid"], grantee_uuid=world["gem_orch"]["uuid"], budget_count=1
    )
    core.handshake(requester_uuid=world["gem_orch"]["uuid"], partner_title="gemini-worker")
    mk_partner(core, world["gemini_project"], title="gemini-second", remote_id="r-gsecond")
    with pytest.raises(Rejected) as exc:
        core.handshake(requester_uuid=world["gem_orch"]["uuid"], partner_title="gemini-second")
    assert exc.value.code == "gemini_budget_exceeded", f"expected gemini_budget_exceeded, got {exc.value.code!r}"


def test_handshake_gemini_single_science_source_across_different_projects(core, world):
    """The case that matters: two DIFFERENT science_ Projects' gemini-orchestrators
    both trying to reach the SAME gemini_ partner."""
    core.handshake(requester_uuid=world["gem_orch"]["uuid"], partner_title="gemini-worker")

    pid = mk_project(core, title="Beta", source_prefix="science_", system_id="s-beta2")
    project = {"id": pid, "source_prefix": "science_"}
    orch2 = mk_partner(core, project, title="beta-orchestrator", remote_id="r-beta-orch2")
    core.claim_orchestrator(requester_uuid=orch2["uuid"], project_id=pid, orchestrator_type="project-orchestrator")
    gem2 = mk_partner(core, project, title="beta-gem-orch", remote_id="r-beta-gem2")
    core.claim_orchestrator(requester_uuid=gem2["uuid"], project_id=pid, orchestrator_type="gemini-orchestrator")
    core.grant_gemini_budget(requester_uuid=orch2["uuid"], grantee_uuid=gem2["uuid"], budget_count=1)

    with pytest.raises(Rejected) as exc:
        core.handshake(requester_uuid=gem2["uuid"], partner_title="gemini-worker")
    assert exc.value.code == "gemini_single_science_source", (
        f"expected gemini_single_science_source, got {exc.value.code!r}"
    )


def test_handshake_different_project_without_extension(core, world):
    pid_b = mk_project(core, title="Beta", source_prefix="science_", system_id="s-beta3")
    mk_partner(core, {"id": pid_b, "source_prefix": "science_"}, title="beta-worker", remote_id="r-beta-w")
    with pytest.raises(Rejected) as exc:
        core.handshake(requester_uuid=world["orch"]["uuid"], partner_title="beta-worker")
    assert exc.value.code == "different_project", f"expected different_project, got {exc.value.code!r}"


def test_handshake_cross_project_requires_same_role(core, world):
    pid_b = mk_project(core, title="Beta", source_prefix="science_", system_id="s-beta4")
    project_b = {"id": pid_b, "source_prefix": "science_"}
    orch_b = mk_partner(core, project_b, title="beta-orchestrator", remote_id="r-beta-orch4")
    core.claim_orchestrator(requester_uuid=orch_b["uuid"], project_id=pid_b, orchestrator_type="project-orchestrator")
    gem_b = mk_partner(core, project_b, title="beta-gem-orch", remote_id="r-beta-gem4")
    core.claim_orchestrator(requester_uuid=gem_b["uuid"], project_id=pid_b, orchestrator_type="gemini-orchestrator")

    core.extend_project(requester_uuid=world["orch"]["uuid"], project_title="Beta")

    # Mismatched roles across the extension: a gemini-orchestrator must not
    # inherit direction from a project-orchestrator just because the
    # Projects are linked.
    with pytest.raises(Rejected) as exc:
        core.handshake(requester_uuid=world["orch"]["uuid"], partner_title="beta-gem-orch")
    assert exc.value.code == "cross_project_requires_same_role", (
        f"expected cross_project_requires_same_role, got {exc.value.code!r}"
    )

    # Same role across the extension: legal.
    result = core.handshake(requester_uuid=world["orch"]["uuid"], partner_title="beta-orchestrator")
    assert result["to_partner_title"] == "beta-orchestrator", (
        f"same-role handshake across a registered extension must succeed, got {result!r}"
    )


def test_extension_grants_nothing_inside_one_project(core, world):
    """An extension links two PROJECTS. It must not loosen anything for two
    partners that are already in the SAME project: 'lit-review' (no role)
    handshaking 'orchestrator' inside Alpha is still refused exactly as if
    no extension existed anywhere.
    """
    mk_project(core, title="Beta", source_prefix="science_", system_id="s-beta5")
    core.extend_project(requester_uuid=world["orch"]["uuid"], project_title="Beta")

    with pytest.raises(Rejected) as exc:
        core.handshake(requester_uuid=world["worker"]["uuid"], partner_title="orchestrator")
    assert exc.value.code == "requester_not_orchestrator", (
        f"an extension with another project must not affect same-project rules; "
        f"expected requester_not_orchestrator, got {exc.value.code!r}"
    )


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


def test_send_rejects_unknown_behavior(core, world):
    before = queue_count(core.db, world["worker"]["id"])
    with pytest.raises(Rejected) as exc:
        core.send(
            requester_uuid=world["orch"]["uuid"], queried_partner_title="lit-review",
            message="hi", behavior="[NOT-A-REAL-LABEL]",
        )
    assert exc.value.code == "unknown_behavior", f"expected unknown_behavior, got {exc.value.code!r}"
    after = queue_count(core.db, world["worker"]["id"])
    assert after == before, f"an unrecognized behavior must not be queued: before={before}, after={after}"


def test_send_rejects_idle_not_sendable(core, world):
    before = queue_count(core.db, world["worker"]["id"])
    with pytest.raises(Rejected) as exc:
        core.send(
            requester_uuid=world["orch"]["uuid"], queried_partner_title="lit-review",
            message="stop", behavior="[IDLE]",
        )
    assert exc.value.code == "idle_not_sendable", f"expected idle_not_sendable, got {exc.value.code!r}"
    after = queue_count(core.db, world["worker"]["id"])
    assert after == before, "[IDLE] must never be queued via send()"


def test_send_rejects_source_cannot_send(core, world):
    """An nlm_ partner never originates a message (source_caps.can_send=0). No
    handshake is set up at all -- source_cannot_send is checked before the
    handshake check in send(), so this refusal cannot be `no_handshake` in
    disguise regardless.
    """
    before = queue_count(core.db, world["worker"]["id"])
    with pytest.raises(Rejected) as exc:
        core.send(
            requester_uuid=world["nlm_partner"]["uuid"], queried_partner_title="lit-review",
            message="hi", behavior="[QUERY]",
        )
    assert exc.value.code == "source_cannot_send", f"expected source_cannot_send, got {exc.value.code!r}"
    after = queue_count(core.db, world["worker"]["id"])
    assert after == before, "a refused send must not be queued"


def test_send_rejects_research_not_accepted(core, world):
    """An nlm_ partner never accepts [RESEARCH] (source_caps.accepts_research=0).
    This check fires unconditionally before the hierarchy check when the
    target does not accept research, so it cannot be
    `research_cannot_flow_upward` in disguise -- and no handshake is needed
    since nlm_ needs_handshake=0.
    """
    before = queue_count(core.db, world["nlm_partner"]["id"])
    with pytest.raises(Rejected) as exc:
        core.send(
            requester_uuid=world["orch"]["uuid"], queried_partner_title="notebook-source",
            message="go do X", behavior="[RESEARCH]",
        )
    assert exc.value.code == "research_not_accepted", f"expected research_not_accepted, got {exc.value.code!r}"
    after = queue_count(core.db, world["nlm_partner"]["id"])
    assert after == before, "a refused [RESEARCH] must not be queued"


def test_send_research_cannot_flow_upward_but_query_can(core, world):
    """[RESEARCH] may only travel to a same-or-lower position in the hierarchy
    (agent_layers: bridge-scientist=1, project-orchestrator=2 for science_).
    The project-orchestrator sits BELOW the bridge-scientist (layer 2 > layer
    1), so [RESEARCH] flowing orchestrator -> bridge is upward and must be
    refused -- but the same pair must still be able to exchange [QUERY],
    which is not subject to the hierarchy rule at all. The handshake is
    created in this exact direction first, so a [RESEARCH] refusal cannot be
    `no_handshake` in disguise, and the [QUERY] success below proves the rule
    is scoped to [RESEARCH] rather than to the pair.
    """
    core.handshake(requester_uuid=world["orch"]["uuid"], partner_title="bridge")
    before = queue_count(core.db, world["bridge"]["id"])

    with pytest.raises(Rejected) as exc:
        core.send(
            requester_uuid=world["orch"]["uuid"], queried_partner_title="bridge",
            message="delegate this", behavior="[RESEARCH]",
        )
    assert exc.value.code == "research_cannot_flow_upward", (
        f"expected research_cannot_flow_upward, got {exc.value.code!r}"
    )
    after = queue_count(core.db, world["bridge"]["id"])
    assert after == before, f"a refused [RESEARCH] must not be queued: before={before}, after={after}"

    set_ext(core, "science_")
    result = core.send(
        requester_uuid=world["orch"]["uuid"], queried_partner_title="bridge",
        message="just asking", behavior="[QUERY]",
    )
    assert result["behavior"] == "[QUERY]", f"expected the [QUERY] send to succeed, got {result!r}"


def test_send_rejects_no_handshake(core, world):
    before = queue_count(core.db, world["worker"]["id"])
    with pytest.raises(Rejected) as exc:
        core.send(
            requester_uuid=world["orch"]["uuid"], queried_partner_title="lit-review",
            message="hi", behavior="[QUERY]",
        )
    assert exc.value.code == "no_handshake", f"expected no_handshake, got {exc.value.code!r}"
    after = queue_count(core.db, world["worker"]["id"])
    assert after == before, "a refused send must not be queued"


def test_send_over_queue_admits_boundary_and_refuses_third(core, world):
    """[RESEARCH]'s cap (label_caps.max_outstanding=2) is keyed
    (partner_id, caller_id, behavior) and counts the WORKING slot. One in the
    working slot plus one queued is exactly 2: the second send must be
    admitted, the third must not.

    Note: `send`'s returned `queue_depth` is a snapshot of the DB row count
    taken at admission time, BEFORE advance() promotes the row out of the
    table into the in-memory working slot -- it is not "total outstanding
    including the working slot". That total is checked directly below via
    `working_task` + `queue_count` instead of trusting that field's number.
    """
    core.handshake(requester_uuid=world["orch"]["uuid"], partner_title="lit-review")
    set_ext(core, "science_")

    core.send(
        requester_uuid=world["orch"]["uuid"], queried_partner_title="lit-review",
        message="one", behavior="[RESEARCH]",
    )
    working = core.working_task(partner_id=world["worker"]["id"])
    assert working is not None and working["behavior"] == "[RESEARCH]" and working["body"] == "one", (
        f"expected the first [RESEARCH] to be promoted into the empty working slot, got {working!r}"
    )

    core.send(
        requester_uuid=world["orch"]["uuid"], queried_partner_title="lit-review",
        message="two", behavior="[RESEARCH]",
    )
    # 1 working + 1 queued == cap of 2: admitted, but does not displace the
    # first (same priority as itself -- equal priority never displaces).
    queued_after_second = queue_count(
        core.db, world["worker"]["id"], caller_id=world["orch"]["id"], behavior="[RESEARCH]"
    )
    assert queued_after_second == 1, (
        f"expected the second [RESEARCH] admitted and left queued, got {queued_after_second} queued rows"
    )
    still_working = core.working_task(partner_id=world["worker"]["id"])
    assert still_working is not None and still_working["body"] == "one", (
        f"the first [RESEARCH] must still hold the working slot, got {still_working!r}"
    )

    before_third = queue_count(
        core.db, world["worker"]["id"], caller_id=world["orch"]["id"], behavior="[RESEARCH]"
    )
    with pytest.raises(Rejected) as exc:
        core.send(
            requester_uuid=world["orch"]["uuid"], queried_partner_title="lit-review",
            message="three", behavior="[RESEARCH]",
        )
    assert exc.value.code == "over_queue", f"expected over_queue, got {exc.value.code!r}"
    after_third = queue_count(
        core.db, world["worker"]["id"], caller_id=world["orch"]["id"], behavior="[RESEARCH]"
    )
    assert after_third == before_third, (
        f"a refused third [RESEARCH] must not be queued: before={before_third}, after={after_third}"
    )


def test_send_over_queue_is_per_caller(core, world):
    """A DIFFERENT caller's [RESEARCH] cap is untouched by the first caller's.

    The second handshake row is written directly rather than through
    handshake() -- the science_-science_ handshake legality maze (covered
    exhaustively in the handshake tests) restricts which roles may reach a
    plain, roleless target at all, and none of that is what this test is
    about. The second caller is 'bridge' rather than 'gem_orch': a
    gemini-orchestrator sits at layer 3, BELOW the plain worker's default
    layer 2, so a [RESEARCH] from gem_orch to the worker would itself be
    refused as upward (a different rule, covered elsewhere); bridge sits at
    layer 1, safely above/level, so its [RESEARCH] can legally flow down.
    """
    core.handshake(requester_uuid=world["orch"]["uuid"], partner_title="lit-review")
    core.db.write(
        lambda conn: conn.execute(
            "INSERT INTO handshakes (from_partner, to_partner) VALUES (?, ?)",
            (world["bridge"]["id"], world["worker"]["id"]),
        )
    )
    set_ext(core, "science_")

    core.send(requester_uuid=world["orch"]["uuid"], queried_partner_title="lit-review",
              message="a", behavior="[RESEARCH]")
    core.send(requester_uuid=world["orch"]["uuid"], queried_partner_title="lit-review",
              message="b", behavior="[RESEARCH]")
    # orch is now at its cap of 2 (1 working + 1 queued). bridge must be unaffected.
    result = core.send(requester_uuid=world["bridge"]["uuid"], queried_partner_title="lit-review",
                        message="c", behavior="[RESEARCH]")
    assert result["behavior"] == "[RESEARCH]", (
        f"a different caller's [RESEARCH] must be admitted regardless of orch's cap, got {result!r}"
    )
    count = queue_count(core.db, world["worker"]["id"], caller_id=world["bridge"]["id"], behavior="[RESEARCH]")
    assert count == 1, f"expected bridge's own [RESEARCH] queue count to be 1, got {count}"


def test_send_uncapped_label_never_refuses(core, world):
    """[ERROR] has max_outstanding=NULL in label_caps: it must never refuse,
    no matter how many are outstanding."""
    core.handshake(requester_uuid=world["orch"]["uuid"], partner_title="lit-review")
    set_ext(core, "science_")
    for i in range(10):
        result = core.send(
            requester_uuid=world["orch"]["uuid"], queried_partner_title="lit-review",
            message=f"err-{i}", behavior="[ERROR]",
        )
        assert result["behavior"] == "[ERROR]", f"send #{i} of an uncapped label must not refuse, got {result!r}"
    total = queue_count(core.db, world["worker"]["id"], caller_id=world["orch"]["id"], behavior="[ERROR]")
    working = core.working_task(partner_id=world["worker"]["id"])
    working_is_error = working is not None and working["behavior"] == "[ERROR]"
    assert total + (1 if working_is_error else 0) == 10, (
        f"expected 10 [ERROR] tasks in flight total (queued + working), got {total} queued and "
        f"working={working['behavior'] if working else None}"
    )


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def test_read_success_paginates_newest_first(core, world):
    core.handshake(requester_uuid=world["orch"]["uuid"], partner_title="lit-review")
    set_ext(core, "science_")
    for i in range(3):
        core.send(requester_uuid=world["orch"]["uuid"], queried_partner_title="lit-review",
                   message=f"m{i}", behavior="[QUERY]")
    result = core.read(requester_uuid=world["orch"]["uuid"], partner_title="lit-review", page=1, page_size=2)
    assert result["total"] == 3, f"expected total 3, got {result!r}"
    bodies = [m["body"] for m in result["messages"]]
    assert bodies == ["m2", "m1"], f"expected newest-first pagination ['m2','m1'], got {bodies!r}"


def test_read_empty_is_not_an_error(core, world):
    result = core.read(requester_uuid=world["orch"]["uuid"], partner_title="lit-review")
    assert result["messages"] == [], f"expected no messages, got {result!r}"
    assert result["total"] == 0, f"expected total 0, got {result!r}"


def test_read_rejects_invalid_pagination(core, world):
    with pytest.raises(Rejected) as exc:
        core.read(requester_uuid=world["orch"]["uuid"], partner_title="lit-review", page=0)
    assert exc.value.code == "invalid_pagination", f"expected invalid_pagination, got {exc.value.code!r}"


# ---------------------------------------------------------------------------
# extend_project
# ---------------------------------------------------------------------------


def test_extend_project_success_lower_id_first(core, world):
    pid_b = mk_project(core, title="Beta", source_prefix="science_", system_id="s-ext1")
    result = core.extend_project(requester_uuid=world["orch"]["uuid"], project_title="Beta")
    assert result["created"] is True, f"expected created=True on first extend, got {result!r}"
    assert result["already_linked"] is False, f"expected already_linked=False on first extend, got {result!r}"
    lo, hi = sorted((world["project_id"], pid_b))
    assert (result["project_a"], result["project_b"]) == (lo, hi), (
        f"expected the pair stored lower-id-first as ({lo}, {hi}), got "
        f"({result['project_a']}, {result['project_b']})"
    )
    row = core.db.read_one(
        "SELECT * FROM project_extension WHERE project_a = ? AND project_b = ?", (lo, hi)
    )
    assert row is not None, "expected a project_extension row to have been written"


def test_extend_project_idempotent_regardless_of_which_side_asks(core, world):
    pid_b = mk_project(core, title="Beta", source_prefix="science_", system_id="s-ext2")
    project_b = {"id": pid_b, "source_prefix": "science_"}
    orch_b = mk_partner(core, project_b, title="beta-orchestrator", remote_id="r-beta-o2")
    core.claim_orchestrator(requester_uuid=orch_b["uuid"], project_id=pid_b, orchestrator_type="project-orchestrator")

    first = core.extend_project(requester_uuid=world["orch"]["uuid"], project_title="Beta")
    assert first["created"] is True, f"expected the first call to create the link, got {first!r}"

    second = core.extend_project(requester_uuid=world["orch"]["uuid"], project_title="Beta")
    assert second["already_linked"] is True, f"expected already_linked=True on the second call, got {second!r}"

    # Asked from the OTHER side, the answer must be identical.
    third = core.extend_project(requester_uuid=orch_b["uuid"], project_title="Alpha")
    assert third["already_linked"] is True, (
        f"expected already_linked=True when the other project asks too, got {third!r}"
    )
    assert (third["project_a"], third["project_b"]) == (first["project_a"], first["project_b"]), (
        f"the link must answer identically regardless of which project asks: first={first!r} third={third!r}"
    )
    count = core.db.read_one("SELECT COUNT(*) AS n FROM project_extension")["n"]
    assert count == 1, f"expected exactly one project_extension row total, got {count}"


def test_extend_project_requires_project_orchestrator(core, world):
    mk_project(core, title="Beta", source_prefix="science_", system_id="s-ext3")
    with pytest.raises(Rejected) as exc:
        core.extend_project(requester_uuid=world["worker"]["uuid"], project_title="Beta")
    assert exc.value.code == "requires_project_orchestrator", (
        f"expected requires_project_orchestrator, got {exc.value.code!r}"
    )
    count = core.db.read_one("SELECT COUNT(*) AS n FROM project_extension")["n"]
    assert count == 0, f"a refused extend must not create a link, got {count} rows"


def test_extend_project_rejects_self(core, world):
    with pytest.raises(Rejected) as exc:
        core.extend_project(requester_uuid=world["orch"]["uuid"], project_title="Alpha")
    assert exc.value.code == "self_extension", f"expected self_extension, got {exc.value.code!r}"


def test_extend_project_rejects_cross_source(core, world):
    with pytest.raises(Rejected) as exc:
        core.extend_project(requester_uuid=world["orch"]["uuid"], project_title="Repo")
    assert exc.value.code == "cross_source_extension", f"expected cross_source_extension, got {exc.value.code!r}"
    count = core.db.read_one("SELECT COUNT(*) AS n FROM project_extension")["n"]
    assert count == 0, f"a refused extend must not create a link, got {count} rows"


# ---------------------------------------------------------------------------
# interrupt_partner
# ---------------------------------------------------------------------------


def test_interrupt_partner_displaces_working_task_and_stops_remote(core, world):
    core.handshake(requester_uuid=world["orch"]["uuid"], partner_title="lit-review")
    stub = set_ext(core, "science_")
    core.send(
        requester_uuid=world["orch"]["uuid"], queried_partner_title="lit-review",
        message="do work", behavior="[QUERY]",
    )
    working_before = core.working_task(partner_id=world["worker"]["id"])
    assert working_before is not None and working_before["behavior"] == "[QUERY]", (
        f"precondition failed: expected a working [QUERY] task, got {working_before!r}"
    )

    result = core.interrupt_partner(
        requester_uuid=world["orch"]["uuid"], partner_title="lit-review", reason="stop now"
    )
    assert result["displaced"] == "[QUERY]", f"expected displaced == '[QUERY]', got {result!r}"

    stop_calls = [c for c in stub.calls if c[0] == "stop_remote_execution"]
    assert len(stop_calls) == 1, f"expected exactly one stop_remote_execution call, got {stub.calls!r}"
    assert stop_calls[0][1]["partner_id_in_remote"] == "r-worker", (
        f"expected stop_remote_execution for 'r-worker', got {stop_calls[0][1]!r}"
    )

    displaced_row = core.db.read_one(
        "SELECT * FROM message_queue WHERE partner_id = ? AND behavior = '[QUERY]'",
        (world["worker"]["id"],),
    )
    assert displaced_row is not None, "the displaced [QUERY] must still be in the queue"
    assert displaced_row["in_process"] == 1, (
        f"the displaced task must be marked in_process=1, got {displaced_row['in_process']}"
    )
    assert displaced_row["body"] == "do work", (
        f"the displaced task must keep its original body, got {displaced_row['body']!r}"
    )

    working_after = core.working_task(partner_id=world["worker"]["id"])
    assert working_after is not None and working_after["behavior"] == "[IDLE]", (
        f"expected [IDLE] to hold the working slot, got {working_after!r}"
    )

    # A later arrival of ANY label displaces the [IDLE] hold, and the [IDLE]
    # is discarded rather than requeued. [TRUTHFUL-REPORT] (priority 1) is
    # used here rather than [ERROR]: [ERROR] shares [QUERY]'s priority (2),
    # and the paused [QUERY] sitting in the queue from the displacement above
    # would then tie with it on priority, which is a separate tie-break
    # question this test is not about. [TRUTHFUL-REPORT]'s priority (1) is
    # strictly higher than the paused [QUERY]'s (2), so it is unambiguously
    # the head either way -- and it could NEVER win against an actual working
    # [IDLE] (priority 0) under the normal "strictly higher priority"
    # comparison, so its delivery here is squarely evidence of the holding
    # bypass, not of ordinary priority ordering.
    core.send(
        requester_uuid=world["orch"]["uuid"], queried_partner_title="lit-review",
        message="a report", behavior="[TRUTHFUL-REPORT]",
    )
    working_final = core.working_task(partner_id=world["worker"]["id"])
    assert working_final is not None and working_final["behavior"] == "[TRUTHFUL-REPORT]", (
        f"expected [TRUTHFUL-REPORT] to displace the [IDLE] hold, got {working_final!r}"
    )
    idle_row = core.db.read_one(
        "SELECT * FROM message_queue WHERE partner_id = ? AND behavior = '[IDLE]'",
        (world["worker"]["id"],),
    )
    assert idle_row is None, f"a displaced [IDLE] must never be requeued, found {idle_row!r}"

    query_still_queued = core.db.read_one(
        "SELECT * FROM message_queue WHERE partner_id = ? AND behavior = '[QUERY]'",
        (world["worker"]["id"],),
    )
    assert query_still_queued is not None and query_still_queued["in_process"] == 1, (
        f"the original displaced [QUERY] must remain queued and paused, got {query_still_queued!r}"
    )


def test_interrupt_partner_rejects_different_project(core, world):
    before = queue_count(core.db, world["worker"]["id"])
    with pytest.raises(Rejected) as exc:
        core.interrupt_partner(
            requester_uuid=world["code_partner"]["uuid"], partner_title="lit-review", reason="x"
        )
    assert exc.value.code == "different_project", f"expected different_project, got {exc.value.code!r}"
    after = queue_count(core.db, world["worker"]["id"])
    assert after == before, "a refused interrupt must not queue anything"


def test_interrupt_partner_rejects_not_executable(core):
    pid = mk_project(core, title="Notebook", source_prefix="nlm_", system_id="n-int1")
    project = {"id": pid, "source_prefix": "nlm_"}
    caller = mk_partner(core, project, title="notebook-caller", remote_id="r-caller")
    target = mk_partner(core, project, title="notebook-source", remote_id="r-source")
    before = queue_count(core.db, target["id"])
    with pytest.raises(Rejected) as exc:
        core.interrupt_partner(requester_uuid=caller["uuid"], partner_title="notebook-source", reason="stop")
    assert exc.value.code == "not_executable", f"expected not_executable, got {exc.value.code!r}"
    after = queue_count(core.db, target["id"])
    assert after == before, "a refused interrupt must not queue anything"


# ---------------------------------------------------------------------------
# advance / release / working_task / report_back / begin_summary_phase /
# reply_behavior -- the swap rules, tested directly
# ---------------------------------------------------------------------------


@pytest.fixture
def pair(core):
    pid = mk_project(core, title="SwapProj", source_prefix="science_", system_id="swap-1")
    project = {"id": pid, "source_prefix": "science_"}
    target = mk_partner(core, project, title="swap-target", remote_id="r-swap-target")
    caller1 = mk_partner(core, project, title="swap-caller1", remote_id="r-swap-c1")
    caller2 = mk_partner(core, project, title="swap-caller2", remote_id="r-swap-c2")
    return {"project": project, "target": target, "caller1": caller1, "caller2": caller2}


def test_advance_promotes_into_empty_slot(core, pair):
    push_raw(core.db, partner_id=pair["target"]["id"], caller_id=pair["caller1"]["id"],
             behavior="[QUERY]", body="hello")
    set_ext(core, "science_")
    result = core.advance(partner_id=pair["target"]["id"])
    assert result is not None and result["delivered"] == "[QUERY]", (
        f"expected the head to be promoted into the empty slot, got {result!r}"
    )
    assert result["resumed"] is False, f"a fresh task must not be reported as resumed, got {result!r}"
    assert result["displaced"] is None, f"an empty slot displaces nothing, got {result!r}"
    working = core.working_task(partner_id=pair["target"]["id"])
    assert working is not None and working["behavior"] == "[QUERY]", (
        f"expected the working slot to hold [QUERY], got {working!r}"
    )
    remaining = queue_count(core.db, pair["target"]["id"])
    assert remaining == 0, f"the promoted row must be removed from message_queue, found {remaining} left"


def test_advance_strictly_higher_priority_displaces(core, pair):
    push_raw(core.db, partner_id=pair["target"]["id"], caller_id=pair["caller1"]["id"],
             behavior="[QUERY]", body="first")
    set_ext(core, "science_")
    core.advance(partner_id=pair["target"]["id"])

    push_raw(core.db, partner_id=pair["target"]["id"], caller_id=pair["caller2"]["id"],
             behavior="[TRUTHFUL-REPORT]", body="second")
    result = core.advance(partner_id=pair["target"]["id"])
    assert result is not None and result["delivered"] == "[TRUTHFUL-REPORT]", (
        f"a strictly higher-priority arrival must displace, got {result!r}"
    )
    assert result["displaced"] == "[QUERY]", f"expected displaced == '[QUERY]', got {result!r}"

    requeued = core.db.read_one(
        "SELECT * FROM message_queue WHERE partner_id = ? AND behavior = '[QUERY]'",
        (pair["target"]["id"],),
    )
    assert requeued is not None and requeued["in_process"] == 1, (
        f"the displaced task must be back in the queue, marked in_process, got {requeued!r}"
    )
    assert requeued["body"] == "first", f"the requeued task must keep its original body, got {requeued['body']!r}"


def test_advance_equal_priority_does_not_displace(core, pair):
    """The single most important swap-rule test: an arriving [QUERY] must not
    displace a [QUERY] already being answered."""
    push_raw(core.db, partner_id=pair["target"]["id"], caller_id=pair["caller1"]["id"],
             behavior="[QUERY]", body="being answered")
    set_ext(core, "science_")
    core.advance(partner_id=pair["target"]["id"])

    push_raw(core.db, partner_id=pair["target"]["id"], caller_id=pair["caller2"]["id"],
             behavior="[QUERY]", body="new arrival")
    result = core.advance(partner_id=pair["target"]["id"])
    assert result is None, f"equal priority must not displace; expected None, got {result!r}"

    working = core.working_task(partner_id=pair["target"]["id"])
    assert working is not None and working["body"] == "being answered", (
        f"the original working task must be untouched, got {working!r}"
    )
    queued = core.db.read_one(
        "SELECT * FROM message_queue WHERE partner_id = ? AND behavior = '[QUERY]'",
        (pair["target"]["id"],),
    )
    assert queued is not None and queued["in_process"] == 0 and queued["body"] == "new arrival", (
        f"the arriving [QUERY] must simply wait, untouched, got {queued!r}"
    )


def test_advance_lower_priority_does_not_displace(core, pair):
    push_raw(core.db, partner_id=pair["target"]["id"], caller_id=pair["caller1"]["id"],
             behavior="[QUERY]", body="working")
    set_ext(core, "science_")
    core.advance(partner_id=pair["target"]["id"])

    push_raw(core.db, partner_id=pair["target"]["id"], caller_id=pair["caller2"]["id"],
             behavior="[RESEARCH]", body="lower priority arrival")
    result = core.advance(partner_id=pair["target"]["id"])
    assert result is None, f"a lower-priority arrival must not displace; expected None, got {result!r}"
    working = core.working_task(partner_id=pair["target"]["id"])
    assert working["behavior"] == "[QUERY]", f"the working task must be unchanged, got {working!r}"


def test_paused_task_outranks_a_fresh_task_of_the_same_label(core, pair):
    tid = pair["target"]["id"]
    set_ext(core, "science_")
    push_raw(core.db, partner_id=tid, caller_id=pair["caller1"]["id"], behavior="[QUERY]", body="original request")
    core.advance(partner_id=tid)  # QUERY promoted, working slot filled

    push_raw(core.db, partner_id=tid, caller_id=pair["caller2"]["id"], behavior="[TRUTHFUL-REPORT]", body="report")
    core.advance(partner_id=tid)  # displaces QUERY -> queued, in_process=1

    # A fresh [QUERY] from a different caller arrives while the paused one waits.
    push_raw(core.db, partner_id=tid, caller_id=pair["caller2"]["id"], behavior="[QUERY]", body="fresh request")

    core.release(partner_id=tid)  # the [TRUTHFUL-REPORT] finishes
    result = core.advance(partner_id=tid)
    assert result is not None and result["delivered"] == "[QUERY]", (
        f"expected a [QUERY] to be promoted, got {result!r}"
    )
    assert result["resumed"] is True, (
        f"the PAUSED [QUERY] must be chosen over the fresh one of the same label, got {result!r}"
    )
    working = core.working_task(partner_id=tid)
    assert working["body"] == "original request", (
        f"the resumed task's stored body must still be the ORIGINAL request, got {working['body']!r}"
    )
    assert working["prompt"] == templates.resume_displaced(behavior="[QUERY]"), (
        f"a resumed task must be delivered with the one-line resume prompt, got {working['prompt']!r}"
    )
    still_queued = core.db.read_one(
        "SELECT * FROM message_queue WHERE partner_id = ? AND behavior = '[QUERY]'", (tid,)
    )
    assert still_queued is not None and still_queued["body"] == "fresh request", (
        f"the fresh [QUERY] must still be waiting, untouched, got {still_queued!r}"
    )


def test_paused_task_does_not_outrank_a_higher_priority_label(core, pair):
    tid = pair["target"]["id"]
    set_ext(core, "science_")
    push_raw(core.db, partner_id=tid, caller_id=pair["caller1"]["id"], behavior="[QUERY]", body="original request")
    core.advance(partner_id=tid)
    push_raw(core.db, partner_id=tid, caller_id=pair["caller2"]["id"], behavior="[TRUTHFUL-REPORT]", body="report")
    core.advance(partner_id=tid)  # QUERY now paused in queue

    # A fresh, higher-priority [TRUTHFUL-REPORT] also arrives.
    push_raw(core.db, partner_id=tid, caller_id=pair["caller2"]["id"], behavior="[TRUTHFUL-REPORT]",
             body="second report")

    core.release(partner_id=tid)
    result = core.advance(partner_id=tid)
    assert result is not None and result["delivered"] == "[TRUTHFUL-REPORT]", (
        f"a higher-priority label must win even over a paused lower-priority label, got {result!r}"
    )
    working = core.working_task(partner_id=tid)
    assert working["body"] == "second report", f"expected the fresh [TRUTHFUL-REPORT] to be delivered, got {working!r}"


def test_advance_requeues_on_delivery_failure_and_propagates(core, pair):
    tid = pair["target"]["id"]
    stub = set_ext(core, "science_")

    def _boom(**kwargs):
        raise RuntimeError("remote is down")

    stub.deliver_message = _boom
    push_raw(core.db, partner_id=tid, caller_id=pair["caller1"]["id"], behavior="[QUERY]", body="will fail")

    with pytest.raises(RuntimeError, match="remote is down"):
        core.advance(partner_id=tid)

    working = core.working_task(partner_id=tid)
    assert working is None, f"a failed delivery must leave the slot empty, got {working!r}"
    row = core.db.read_one(
        "SELECT * FROM message_queue WHERE partner_id = ? AND behavior = '[QUERY]'", (tid,)
    )
    assert row is not None, "a task whose delivery failed must go back into the queue rather than vanish"
    assert row["in_process"] == 1, f"expected the requeued task to be marked in_process, got {row['in_process']}"
    assert row["body"] == "will fail", f"the requeued task must keep its original body, got {row['body']!r}"


def test_advance_discards_queued_work_for_an_archived_partner(core, pair):
    tid = pair["target"]["id"]
    push_raw(core.db, partner_id=tid, caller_id=pair["caller1"]["id"], behavior="[QUERY]", body="never delivered")
    core.db.write(
        lambda conn: conn.execute(
            "UPDATE partners SET archived_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (tid,)
        )
    )
    stub = set_ext(core, "science_")
    result = core.advance(partner_id=tid)
    assert result is None, f"advance() on an archived partner must return None, got {result!r}"
    remaining = queue_count(core.db, tid)
    assert remaining == 0, f"queued work for an archived partner must be discarded, {remaining} rows remain"
    deliver_calls = [c for c in stub.calls if c[0] == "deliver_message"]
    assert deliver_calls == [], f"an archived partner's queued work must never be delivered, got {deliver_calls!r}"


def test_release_clears_the_working_slot_and_is_idempotent(core, pair):
    tid = pair["target"]["id"]
    set_ext(core, "science_")
    push_raw(core.db, partner_id=tid, caller_id=pair["caller1"]["id"], behavior="[QUERY]", body="q")
    core.advance(partner_id=tid)
    released = core.release(partner_id=tid)
    assert released is not None and released["behavior"] == "[QUERY]", (
        f"release() must return what the slot held, got {released!r}"
    )
    assert core.working_task(partner_id=tid) is None, "the slot must be empty after release()"
    again = core.release(partner_id=tid)
    assert again is None, f"calling release() twice must be harmless, got {again!r}"


def test_begin_summary_phase_only_fires_on_research(core, pair):
    tid = pair["target"]["id"]
    assert core.begin_summary_phase(partner_id=tid) is None, "an empty slot must not begin a summary phase"
    set_ext(core, "science_")
    push_raw(core.db, partner_id=tid, caller_id=pair["caller1"]["id"], behavior="[QUERY]", body="q")
    core.advance(partner_id=tid)
    assert core.begin_summary_phase(partner_id=tid) is None, (
        "a working [QUERY] task must not begin a summary phase"
    )
    working = core.working_task(partner_id=tid)
    assert working["behavior"] == "[QUERY]", f"a non-RESEARCH working task must be untouched, got {working!r}"


def test_begin_summary_phase_raises_priority_and_keeps_original_body(core, pair):
    tid = pair["target"]["id"]
    set_ext(core, "science_")
    push_raw(core.db, partner_id=tid, caller_id=pair["caller1"]["id"], behavior="[RESEARCH]", body="go research X")
    core.advance(partner_id=tid)

    caller_title = pair["caller1"]["title"]
    prompt = core.begin_summary_phase(partner_id=tid)
    expected_prompt = templates.truthful_report_request(caller_title=caller_title, original_request="go research X")
    assert prompt == expected_prompt, f"expected the truthful-report prompt, got {prompt!r}"

    working = core.working_task(partner_id=tid)
    assert working["behavior"] == "[TRUTHFUL-REPORT]", (
        f"expected the working task's behavior to become [TRUTHFUL-REPORT], got {working!r}"
    )
    expected_priority = priority_of(core.db, "[TRUTHFUL-REPORT]")
    assert working["priority"] == expected_priority, (
        f"expected the effective priority to be raised to {expected_priority}, got {working['priority']}"
    )
    assert working["body"] == "go research X", (
        f"begin_summary_phase must leave body as the ORIGINAL request, got {working['body']!r}"
    )

    # Nothing but a forced interruption can displace it now: a [QUERY]
    # (priority 2) must not, since the effective priority is now 1.
    push_raw(core.db, partner_id=tid, caller_id=pair["caller2"]["id"], behavior="[QUERY]", body="interrupting query")
    result = core.advance(partner_id=tid)
    assert result is None, (
        f"a [QUERY] must not displace a task whose effective priority was raised to "
        f"[TRUTHFUL-REPORT]'s, got {result!r}"
    )


def test_reply_behavior_matches_label_caps(core):
    cases = {
        "[RESEARCH]": "[TRUTHFUL-REPORT]",
        "[QUERY]": "[MESSAGE-RESPONSE]",
        "[IDLE]": None,
        "[TRUTHFUL-REPORT]": None,
        "[ERROR]": None,
        "[MESSAGE-RESPONSE]": None,
    }
    for behavior, expected in cases.items():
        actual = core.reply_behavior(behavior)
        assert actual == expected, f"reply_behavior({behavior!r}): expected {expected!r}, got {actual!r}"


def test_report_back_pushes_without_auto_delivering(core, pair):
    tid = pair["caller1"]["id"]
    result = core.report_back(
        to_partner_id=tid, from_partner_id=pair["target"]["id"],
        behavior="[MESSAGE-RESPONSE]", body="the answer",
    )
    assert result["queue_depth"] == 1, f"expected queue_depth 1, got {result!r}"
    assert core.working_task(partner_id=tid) is None, "report_back only enqueues; it must not deliver on its own"
    stored = core.db.read_one("SELECT * FROM messages WHERE id = ?", (result["message_id"],))
    assert stored is not None and stored["behavior"] == "[MESSAGE-RESPONSE]", (
        f"a stored label must be written to messages, got {stored!r}"
    )

    set_ext(core, "science_")
    delivered = core.advance(partner_id=tid)
    assert delivered is not None and delivered["delivered"] == "[MESSAGE-RESPONSE]", (
        f"a later advance() must deliver what report_back queued, got {delivered!r}"
    )


def test_report_back_has_no_handshake_requirement(core, pair):
    """report_back is not `send`: no requester uuid, no handshake row needed --
    the handshake was already established in the direction that made the
    original exchange possible."""
    handshake_count = core.db.read_one(
        "SELECT COUNT(*) AS n FROM handshakes WHERE from_partner = ? AND to_partner = ?",
        (pair["target"]["id"], pair["caller1"]["id"]),
    )["n"]
    assert handshake_count == 0, "precondition: no handshake should exist between these two"
    result = core.report_back(
        to_partner_id=pair["caller1"]["id"], from_partner_id=pair["target"]["id"],
        behavior="[MESSAGE-RESPONSE]", body="it broke",
    )
    assert result["behavior"] == "[MESSAGE-RESPONSE]", f"report_back must succeed with no handshake, got {result!r}"


def test_report_back_refuses_delegation_and_holds_but_carries_every_report(core, world):
    """`report_back` carries reports; it never carries delegation or a hold.

    `[RESEARCH]` is delegated work, and admitting it here would bypass the
    hierarchy rule `send` enforces -- anything holding a `MessagingCore` could
    land research in a superior's queue through the back door. `[IDLE]` is a
    hold, which has no meaning in a Caller's queue.

    The other four are all reachable in practice, and two of them are cases the
    Partner cannot report itself: an `[ERROR]` when it stops on a permission it
    does not hold, and a `[QUERY]` when it needs context only the Caller has.
    An agent stopped on a prompt is not running.
    """
    worker, caller = world["worker"], world["orch"]

    for behavior in ("[RESEARCH]", "[IDLE]"):
        with pytest.raises(Rejected) as exc:
            core.report_back(to_partner_id=caller["id"], from_partner_id=worker["id"],
                             behavior=behavior, body="x")
        assert exc.value.code == "not_reportable", (
            f"{behavior} must not be reportable, got {exc.value.code!r}"
        )
        depth = core.db.read_one(
            "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id = ?", (caller["id"],)
        )["n"]
        assert depth == 0, f"{behavior} was refused but still queued ({depth} rows)"

    for behavior in ("[QUERY]", "[ERROR]", "[MESSAGE-RESPONSE]", "[TRUTHFUL-REPORT]"):
        result = core.report_back(to_partner_id=caller["id"], from_partner_id=worker["id"],
                                  behavior=behavior, body=f"a {behavior}")
        assert result["behavior"] == behavior, (
            f"{behavior} should be reportable; got {result!r}"
        )


def test_permissions_reject_non_gemini_partner(core, world):
    core.extension = None
    calls = (
        lambda: core.get_permissions(requester_uuid=world["orch"]["uuid"], partner_title="lit-review"),
        lambda: core.add_permissions(requester_uuid=world["orch"]["uuid"], partner_title="lit-review",
                                      read_paths=["/x"]),
        lambda: core.delete_permissions(requester_uuid=world["orch"]["uuid"], partner_title="lit-review",
                                         paths=["/x"]),
    )
    for call in calls:
        with pytest.raises(Rejected) as exc:
            call()
        assert exc.value.code == "not_path_configurable", (
            f"expected not_path_configurable for a non-gemini_ partner, got {exc.value.code!r}"
        )


def test_add_permissions_rejects_empty_paths(core, world):
    set_ext(core, "gemini_")
    with pytest.raises(Rejected) as exc:
        core.add_permissions(requester_uuid=world["orch"]["uuid"], partner_title="gemini-worker")
    assert exc.value.code == "no_paths", f"expected no_paths, got {exc.value.code!r}"


def test_delete_permissions_rejects_empty_paths(core, world):
    set_ext(core, "gemini_")
    with pytest.raises(Rejected) as exc:
        core.delete_permissions(requester_uuid=world["orch"]["uuid"], partner_title="gemini-worker", paths=[])
    assert exc.value.code == "no_paths", f"expected no_paths, got {exc.value.code!r}"


def test_add_permissions_records_and_pushes_new_only(core, world):
    stub = set_ext(core, "gemini_")
    target = world["gemini_partner"]

    result = core.add_permissions(
        requester_uuid=world["orch"]["uuid"], partner_title=target["title"],
        read_paths=["/a"], write_paths=["/b"],
    )
    assert result["granted"] == ["read_file(/a)", "write_file(/b)"], f"unexpected granted list: {result!r}"
    assert result["unchanged"] == [], f"expected nothing unchanged on the first grant, got {result!r}"

    recorded = core.db.read("SELECT kind, path FROM partner_paths WHERE partner_id = ?", (target["id"],))
    assert {(r["kind"], r["path"]) for r in recorded} == {("read", "/a"), ("write", "/b")}, (
        f"expected partner_paths to record both grants, got {[dict(r) for r in recorded]!r}"
    )
    add_calls = [c for c in stub.calls if c[0] == "add_permissions"]
    assert len(add_calls) == 1, f"expected exactly one remote add_permissions call, got {stub.calls!r}"
    assert set(add_calls[0][1]["rules"]) == {"read_file(/a)", "write_file(/b)"}, (
        f"expected both new rules pushed to the remote, got {add_calls[0][1]!r}"
    )

    # Re-adding an already-held rule must be reported unchanged, not granted twice.
    result2 = core.add_permissions(
        requester_uuid=world["orch"]["uuid"], partner_title=target["title"], read_paths=["/a"]
    )
    assert result2["granted"] == [], f"expected nothing granted the second time, got {result2!r}"
    assert result2["unchanged"] == ["read_file(/a)"], f"expected ['read_file(/a)'] unchanged, got {result2!r}"
    add_calls_after = [c for c in stub.calls if c[0] == "add_permissions"]
    assert len(add_calls_after) == 1, (
        f"a rule already held must not trigger a second remote add_permissions call, got {stub.calls!r}"
    )
    recorded_after = core.db.read(
        "SELECT * FROM partner_paths WHERE partner_id = ? AND kind = 'read' AND path = '/a'", (target["id"],)
    )
    assert len(recorded_after) == 1, (
        f"re-adding an already-recorded path must not duplicate the row, found {len(recorded_after)}"
    )


def test_add_permissions_raises_when_remote_silently_refuses(core, world):
    """The single most important test in this group: the stub accepts the
    write call but quietly does not apply it. add_permissions must catch this
    via its verify-after-write and raise -- AND must not have recorded
    anything locally, since the grant never actually took effect."""
    stub = set_ext(core, "gemini_")
    target = world["gemini_partner"]
    stub.permissions_refuse = {"write_file(/blocked)"}

    with pytest.raises(Rejected) as exc:
        core.add_permissions(
            requester_uuid=world["orch"]["uuid"], partner_title=target["title"], write_paths=["/blocked"]
        )
    assert exc.value.code == "permission_not_applied", f"expected permission_not_applied, got {exc.value.code!r}"

    rows = core.db.read(
        "SELECT * FROM partner_paths WHERE partner_id = ? AND path = '/blocked'", (target["id"],)
    )
    assert rows == [], (
        f"a permission that failed to apply on the remote must NOT be recorded locally, found {[dict(r) for r in rows]!r}"
    )


def test_delete_permissions_revokes_both_kinds(core, world):
    stub = set_ext(core, "gemini_")
    target = world["gemini_partner"]
    core.add_permissions(
        requester_uuid=world["orch"]["uuid"], partner_title=target["title"],
        read_paths=["/dual"], write_paths=["/dual"],
    )

    result = core.delete_permissions(
        requester_uuid=world["orch"]["uuid"], partner_title=target["title"], paths=["/dual"]
    )
    assert set(result["revoked"]) == {"read_file(/dual)", "write_file(/dual)"}, (
        f"expected both kinds revoked, got {result!r}"
    )
    rows = core.db.read("SELECT kind FROM partner_paths WHERE partner_id = ? AND path = '/dual'", (target["id"],))
    assert rows == [], f"both kinds must be removed from partner_paths, found {[dict(r) for r in rows]!r}"
    remaining = stub.get_permissions(partner_id_in_remote=target["partner_id_in_remote"])
    assert remaining == [], f"expected the remote to hold nothing for /dual, got {remaining!r}"


def test_get_permissions_reports_drift(core, world):
    stub = set_ext(core, "gemini_")
    target = world["gemini_partner"]
    core.add_permissions(requester_uuid=world["orch"]["uuid"], partner_title=target["title"], read_paths=["/x"])

    # Simulate the remote losing a recorded grant out of band.
    stub.permissions[target["partner_id_in_remote"]].remove("read_file(/x)")
    # Simulate the remote holding a grant nobody ever recorded.
    stub.permissions.setdefault(target["partner_id_in_remote"], []).append("write_file(/unrecorded)")

    result = core.get_permissions(requester_uuid=world["orch"]["uuid"], partner_title=target["title"])
    assert result["missing"] == ["read_file(/x)"], f"expected missing == ['read_file(/x)'], got {result!r}"
    assert result["unrecorded"] == ["write_file(/unrecorded)"], (
        f"expected unrecorded == ['write_file(/unrecorded)'], got {result!r}"
    )
    assert result["recorded"] == {"read": ["/x"], "write": []}, (
        f"expected recorded to reflect what was written via add_permissions, got {result['recorded']!r}"
    )
    assert result["allowed"] == ["write_file(/unrecorded)"], (
        f"expected allowed to reflect exactly what the stub currently holds, got {result['allowed']!r}"
    )
