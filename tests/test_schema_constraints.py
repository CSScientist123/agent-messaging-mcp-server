"""Asserts the messaging schema's constraints actually bite -- not merely that the DDL parses.

    python3 tests/test_schema_constraints.py [schema.sql]

Every assertion corresponds to a rule stated in the design. If a rule is added there, add an
assertion here; if an assertion fails, one of the two is wrong.
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

SCHEMA = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "schema.sql")

#: Admission in one statement, so being over the cap yields rowcount 0 rather than needing a
#: read-then-write. `:working` carries the working slot's contribution, which lives in the
#: Polling Server's memory and so cannot be counted by SQL alone.
ADMIT = """
INSERT INTO message_queue (partner_id, caller_id, behavior, body)
SELECT :pid, :cid, :behavior, :body
 WHERE (SELECT max_outstanding FROM label_caps WHERE behavior = :behavior) IS NULL
    OR (SELECT COUNT(*) FROM message_queue
         WHERE partner_id = :pid AND caller_id = :cid AND behavior = :behavior) + :working
     < (SELECT max_outstanding FROM label_caps WHERE behavior = :behavior)
"""

passed = failed = 0


def ok(cond: bool, msg: str) -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS: {msg}")
    else:
        failed += 1
        print(f"FAIL: {msg}")


def rejects(db: sqlite3.Connection, sql: str, *args) -> bool:
    """True when the database refuses the statement -- which is the point of a constraint."""
    try:
        db.execute(sql, *args)
        return False
    except sqlite3.IntegrityError:
        return True


def partner(db, title, project, uuid, role=None, remote=None):
    db.execute(
        "INSERT INTO partners(uuid,project_id,title,partner_id_in_remote,descr,orchestrator_type)"
        " VALUES (?,?,?,?,'d',?)",
        (uuid, project, title, remote or f"r-{uuid}", role),
    )


def main() -> int:
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA.read_text(encoding="utf-8"))
    db.execute("PRAGMA foreign_keys = ON")

    db.execute("INSERT INTO projects(source_prefix,project_system_id,title) VALUES ('science_','proj_a','Alpha')")
    db.execute("INSERT INTO projects(source_prefix,project_system_id,title) VALUES ('gemini_','g1','Gem')")
    partner(db, "worker-1", 1, "u1")
    partner(db, "agy-1", 2, "u2")

    # ---- identity -------------------------------------------------------------------
    ok(rejects(db, "INSERT INTO partners(uuid,project_id,title,partner_id_in_remote,descr)"
                   " VALUES ('u9',2,'worker-1','r9','d')"),
       "duplicate partner title rejected across DIFFERENT projects (server-wide uniqueness)")
    ok(rejects(db, "INSERT INTO projects(source_prefix,project_system_id,title) VALUES ('code_','p9','Alpha')"),
       "duplicate project title rejected")
    ok(rejects(db, "INSERT INTO partners(uuid,project_id,title,partner_id_in_remote,descr)"
                   " VALUES ('u8',1,'other','r-u1','d')"),
       "same partner_id_in_remote twice in one project rejected")
    db.execute("INSERT INTO partners(uuid,project_id,title,partner_id_in_remote,descr)"
               " VALUES ('u7',2,'other','r-u1','d')")
    ok(True, "the same remote id in a DIFFERENT project is allowed -- it names a different remote")

    # ---- handshakes -----------------------------------------------------------------
    ok(rejects(db, "INSERT INTO handshakes(from_partner,to_partner) VALUES (1,1)"),
       "self-handshake rejected")
    db.execute("INSERT INTO handshakes(from_partner,to_partner) VALUES (1,2)")
    ok(rejects(db, "INSERT INTO handshakes(from_partner,to_partner) VALUES (1,2)"),
       "duplicate one-way handshake rejected")

    # ---- roles ----------------------------------------------------------------------
    partner(db, "orch", 1, "u3", "project-orchestrator")
    ok(rejects(db, "INSERT INTO partners(uuid,project_id,title,partner_id_in_remote,descr,orchestrator_type)"
                   " VALUES ('u4',1,'orch2','r4','d','project-orchestrator')"),
       "second project-orchestrator in one project rejected")
    partner(db, "gorch", 1, "u5", "gemini-orchestrator")
    ok(True, "a different role in the same project is allowed")
    partner(db, "bridge", 1, "u6", "bridge-scientist")
    ok(True, "bridge-scientist is a claimable role")
    ok(rejects(db, "INSERT INTO partners(uuid,project_id,title,partner_id_in_remote,descr,orchestrator_type)"
                   " VALUES ('uX',1,'bad','rX','d','scientist-scientist')"),
       "the old role name scientist-scientist is rejected")

    # ---- label priority table -------------------------------------------------------
    order = [r[0] for r in db.execute("SELECT behavior FROM label_caps ORDER BY priority, behavior")]
    ok("[IDLE]" not in order,
       "there is no hold label -- the question an agent asked IS the hold")
    ok(order[0] == "[TRUTHFUL-REPORT]",
       "[TRUTHFUL-REPORT] outranks everything, so a summary is not contaminated")
    q_pri = db.execute("SELECT priority FROM label_caps WHERE behavior='[QUERY]'").fetchone()[0]
    tr_pri = db.execute(
        "SELECT priority FROM label_caps WHERE behavior='[TRUTHFUL-REPORT]'").fetchone()[0]
    ok(tr_pri < q_pri,
       "only a summary outranks an agent waiting on its own [QUERY] or [ERROR]")
    ok(order[-1] == "[RESEARCH]", "[RESEARCH] is lowest -- new work never displaces an issue")
    q, e = (db.execute("SELECT priority FROM label_caps WHERE behavior=?", (b,)).fetchone()[0]
            for b in ("[QUERY]", "[ERROR]"))
    ok(q == e, "[QUERY] and [ERROR] share a rank -- both stop work, neither is more urgent")
    ok(rejects(db, "INSERT INTO message_queue(partner_id,caller_id,behavior,body)"
                   " VALUES (2,1,'[NOPE]','b')"),
       "an unknown label cannot be queued")

    # ---- admission caps -------------------------------------------------------------
    def admit(pid, cid, behavior, working=0):
        return db.execute(ADMIT, {"pid": pid, "cid": cid, "behavior": behavior,
                                  "body": "b", "working": working}).rowcount

    ok(admit(2, 1, "[RESEARCH]") == 1 and admit(2, 1, "[RESEARCH]") == 1,
       "two [RESEARCH] from one caller admitted")
    ok(admit(2, 1, "[RESEARCH]") == 0, "a third [RESEARCH] from the same caller is refused")
    ok(admit(2, 3, "[RESEARCH]") == 1,
       "the cap is per CALLER -- a different caller still has its own two")
    ok(all(admit(2, 1, "[QUERY]") == 1 for _ in range(3)), "three [QUERY] from one caller admitted")
    ok(admit(2, 1, "[QUERY]") == 0, "a fourth [QUERY] from the same caller is refused")
    db.execute("DELETE FROM message_queue WHERE behavior='[QUERY]'")
    ok(admit(2, 1, "[QUERY]", working=3) == 0,
       "the working slot counts toward the cap -- it limits work in flight, not work waiting")
    ok(all(admit(2, 1, "[ERROR]") == 1 for _ in range(5)),
       "[ERROR] is uncapped -- an agent saying it is blocked must never be refused")
    db.execute("DELETE FROM message_queue")

    # ---- storage --------------------------------------------------------------------
    ok(rejects(db, "INSERT INTO messages(from_partner,to_partner,behavior,body) VALUES (1,2,'[RESEARCH]','b')"),
       "[RESEARCH] cannot be stored in messages -- it is transport only")
    ok(rejects(db, "INSERT INTO messages(from_partner,to_partner,behavior,body) VALUES (1,2,'[ERROR]','b')"),
       "[ERROR] cannot be stored in messages -- resolving it is all we keep")
    ok(rejects(db, "INSERT INTO message_queue(partner_id,caller_id,behavior,body) "
                   "VALUES (2,1,'[IDLE]','b')"),
       "[IDLE] is not a label any more and cannot be queued")
    db.execute("INSERT INTO messages(from_partner,to_partner,behavior,body) VALUES (1,2,'[QUERY]','b')")
    ok(True, "[QUERY] is stored and therefore readable")

    # ---- budget grants --------------------------------------------------------------
    # ids: 1 worker-1, 2 agy-1, 3 other, 4 orch(project-orch), 5 gorch(gemini-orch), 6 bridge
    ok(rejects(db, "INSERT INTO budget_grants(grantee_partner,granted_by,budget_count) VALUES (5,1,2)"),
       "a budget granted by a non-orchestrator is rejected")
    # granted_by is the real project-orchestrator here, so this can only fail on the
    # grantee's role -- otherwise it would pass for the wrong reason and the grantee rule
    # would never actually be exercised.
    ok(rejects(db, "INSERT INTO budget_grants(grantee_partner,granted_by,budget_count) VALUES (1,4,2)"),
       "a budget granted to a partner that is not the gemini-orchestrator is rejected")
    db.execute("INSERT INTO budget_grants(grantee_partner,granted_by,budget_count) VALUES (5,4,2)")
    ok(True, "project-orchestrator -> gemini-orchestrator is the one legal grant")
    ok(rejects(db, "INSERT INTO budget_grants(grantee_partner,granted_by,budget_count) VALUES (6,4,4)"),
       "gemini budget above 3 rejected")

    # ---- path grants ----------------------------------------------------------------
    ok(rejects(db, "INSERT INTO partner_paths(partner_id,kind,path) VALUES (1,'write','/x')"),
       "a path grant on a non-gemini partner is rejected -- nothing would ever apply it")
    db.execute("INSERT INTO partner_paths(partner_id,kind,path) VALUES (2,'write','/mnt/c/Data/tet-dit')")
    ok(True, "a path grant on a gemini_ partner is allowed")

    # ---- project extension ----------------------------------------------------------
    db.execute("INSERT INTO project_extension(project_a,project_b) VALUES (1,2)")
    ok(rejects(db, "INSERT INTO project_extension(project_a,project_b) VALUES (2,1)"),
       "an extension pair is stored one way only, so it cannot disagree with itself")
    ok(rejects(db, "INSERT INTO project_extension(project_a,project_b) VALUES (1,1)"),
       "a project cannot be an extension of itself")

    # ---- live-partner ceiling -------------------------------------------------------
    db.execute("INSERT INTO projects(source_prefix,project_system_id,title) VALUES ('code_','p3','Ceiling')")
    for i in range(10):
        partner(db, f"c-{i}", 3, f"cu{i}")
    ok(rejects(db, "INSERT INTO partners(uuid,project_id,title,partner_id_in_remote,descr)"
                   " VALUES ('cu10',3,'c-10','r','d')"),
       "11th live partner in a project rejected -- the ten-limit is enforced, not prose")
    db.execute("UPDATE partners SET archived_at='2026-01-01T00:00:00.000Z' WHERE title='c-0'")
    partner(db, "c-10", 3, "cu10")
    ok(True, "archiving frees a live-partner slot")
    ok(rejects(db, "UPDATE partners SET title='recycled' WHERE title='c-0'"),
       "an archived partner cannot be renamed -- a spent title cannot be laundered back")

    # ---- label_caps is the single authority for per-label facts ---------------------
    #
    # Every one of these is read by SQL somewhere in messaging_core/core.py. A row that
    # disagrees with the design is a behaviour change with no code change, which is the
    # point of keeping them as data -- and the reason they need asserting here.
    caps = dict(
        (b, (p_, m, st, rb))
        for b, p_, m, st, rb in db.execute(
            "SELECT behavior, priority, max_outstanding, stored, reply_behavior FROM label_caps"
        )
    )
    ok(caps["[TRUTHFUL-REPORT]"][0] < caps["[QUERY]"][0] < caps["[MESSAGE-RESPONSE]"][0]
       < caps["[RESEARCH]"][0],
       "[TRUTHFUL-REPORT] outranks [QUERY] outranks [MESSAGE-RESPONSE] outranks [RESEARCH]")
    ok(caps["[QUERY]"][0] == caps["[ERROR]"][0],
       "[QUERY] and [ERROR] share a rank -- both stop work, neither is more urgent")
    ok(caps["[MESSAGE-RESPONSE]"][0] < caps["[RESEARCH]"][0],
       "an answer outranks delegated work -- otherwise a partner never gets unblocked")
    ok(caps["[QUERY]"][1] == 3 and caps["[RESEARCH]"][1] == 2,
       "the two capped labels are capped at 3 and 2")
    ok(all(caps[b][1] is None for b in ("[ERROR]", "[TRUTHFUL-REPORT]",
                                        "[MESSAGE-RESPONSE]")),
       "every other label is uncapped -- an answer must never be refused for being the fourth")
    ok([b for b, v in caps.items() if v[2]] and
       set(b for b, v in caps.items() if v[2]) == {"[QUERY]", "[TRUTHFUL-REPORT]",
                                                   "[MESSAGE-RESPONSE]"},
       "exactly the three answerable labels are stored; transport labels are not")
    ok(caps["[QUERY]"][3] == "[MESSAGE-RESPONSE]"
       and caps["[ERROR]"][3] == "[MESSAGE-RESPONSE]"
       and caps["[RESEARCH]"][3] == "[TRUTHFUL-REPORT]",
       "the three labels that ask for something name what comes back")
    ok(all(caps[b][3] is None for b in ("[TRUTHFUL-REPORT]",
                                        "[MESSAGE-RESPONSE]")),
       "every other label replies with nothing -- that NULL is what terminates an exchange")
    ok(caps[caps["[ERROR]"][3]][3] is None,
       "an [ERROR]'s answer itself replies with nothing, so the correction ends in one hop")
    ok(rejects(db, "UPDATE label_caps SET reply_behavior='[QUERY]' WHERE behavior='[QUERY]'"),
       "a label cannot reply with itself -- that is an exchange with no end")

    # ---- messages stores only what label_caps says is stored ------------------------
    #
    # Enforced by a trigger rather than a CHECK listing the labels, so there is exactly one
    # authority. These two assertions are what prove the trigger reads label_caps and is not
    # a second hardcoded list that happens to agree today.
    db.execute("INSERT INTO projects(source_prefix,project_system_id,title)"
               " VALUES ('science_','p-store','Storing')")
    sp = db.execute("SELECT id FROM projects WHERE title='Storing'").fetchone()[0]
    partner(db, "st-a", sp, "st-a-u")
    partner(db, "st-b", sp, "st-b-u")
    a, b = (db.execute("SELECT id FROM partners WHERE title=?", (t,)).fetchone()[0]
            for t in ("st-a", "st-b"))
    for beh in ("[QUERY]", "[TRUTHFUL-REPORT]", "[MESSAGE-RESPONSE]"):
        db.execute("INSERT INTO messages(from_partner,to_partner,behavior,body) VALUES (?,?,?,'x')",
                   (a, b, beh))
    ok(db.execute("SELECT COUNT(*) FROM messages WHERE to_partner=?", (b,)).fetchone()[0] == 3,
       "the three stored labels are accepted by messages")
    for beh in ("[RESEARCH]", "[ERROR]"):
        ok(rejects(db, "INSERT INTO messages(from_partner,to_partner,behavior,body)"
                       " VALUES (?,?,?,'x')", (a, b, beh)),
           f"{beh} is transport-only and is refused by messages")
    db.execute("UPDATE label_caps SET stored=1 WHERE behavior='[ERROR]'")
    db.execute("INSERT INTO messages(from_partner,to_partner,behavior,body) VALUES (?,?,'[ERROR]','x')",
               (a, b))
    ok(True, "flipping label_caps.stored changes what messages accepts -- one authority, not two")
    db.execute("UPDATE label_caps SET stored=0 WHERE behavior='[ERROR]'")
    db.execute("DELETE FROM messages WHERE behavior='[ERROR]' AND to_partner=?", (b,))

    # ---- the delegation hierarchy is data --------------------------------------------
    def layer(src, role):
        row = db.execute(
            "SELECT layer FROM agent_layers WHERE source_prefix=? AND orchestrator_type IN (?, '*')"
            " ORDER BY CASE orchestrator_type WHEN '*' THEN 1 ELSE 0 END LIMIT 1", (src, role)
        ).fetchone()
        return None if row is None else row[0]

    ok(layer("nlm_", None) == layer("code_", None) == 0,
       "NotebookLM and Claude code sit at the top of the hierarchy")
    ok(layer("science_", "bridge-scientist") < layer("science_", "project-orchestrator")
       < layer("science_", "gemini-orchestrator") < layer("gemini_", None),
       "bridge > project-orchestrator > gemini-orchestrator > Antigravity, in that order")
    ok(layer("science_", "bridge-scientist") != layer("science_", None),
       "a specific role wins over its source's '*' default")
    ok(layer("science_", None) == layer("science_", "project-orchestrator"),
       "a science_ partner holding no role sits at the project-orchestrator's layer")
    ok(all(layer(s, None) is not None for s in ("nlm_", "code_", "science_", "gemini_")),
       "every source has a default layer -- an unplaced agent is never a lookup miss")

    # ---- the two NotebookLM facts are columns, not code ------------------------------
    nlm = db.execute("SELECT can_send, accepts_research FROM source_caps"
                     " WHERE source_prefix='nlm_'").fetchone()
    ok(nlm == (0, 0),
       "nlm_ neither sends nor accepts delegated work -- both are rows, so a new source is a row")
    others = db.execute("SELECT COUNT(*) FROM source_caps"
                        " WHERE source_prefix<>'nlm_' AND (can_send=0 OR accepts_research=0)"
                        ).fetchone()[0]
    ok(others == 0, "every other source both sends and accepts work")

    # ---- no column without a writer ---------------------------------------------------
    #
    # message_queue rows are DELETED on promotion, so a `dequeued_at` could only ever be
    # written to a row about to disappear. It was removed for that reason; this assertion
    # is what stops it coming back.
    cols = [r[1] for r in db.execute("PRAGMA table_info(message_queue)")]
    ok("dequeued_at" not in cols,
       "message_queue has no dequeued_at -- a promoted row is deleted, so nothing could write it")
    ok("enqueued_at" in cols and "in_process" in cols,
       "message_queue keeps the two fields the pop order actually reads")

    # ---- cascades -------------------------------------------------------------------
    db.execute("DELETE FROM projects WHERE id=2")
    ok(db.execute("SELECT COUNT(*) FROM partners WHERE project_id=2").fetchone()[0] == 0,
       "deleting a project cascades to its partners")
    ok(db.execute("SELECT COUNT(*) FROM message_queue").fetchone()[0] == 0,
       "cascade reaches the message queue")
    ok(db.execute("SELECT COUNT(*) FROM partner_paths").fetchone()[0] == 0,
       "cascade reaches path grants")
    ok(db.execute("SELECT COUNT(*) FROM project_extension").fetchone()[0] == 0,
       "cascade reaches project extensions")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
