"""Mutation pass: break the code on purpose, and check a NAMED test notices.

    python3 tests/mutation_run.py

A green suite is not evidence that an invariant holds -- only an assertion aimed at that
invariant is. This answers the only question that settles it: *would this suite notice if
the code were wrong?*

Each mutant targets one load-bearing rule of the v2 messaging model (one priority queue per
partner, `label_caps` as the single authority for priority/caps/storage/reply, the in-memory
working slot, the drain thread). Every mutation is reverted in a `finally`, so an interrupted
run cannot leave the tree broken.

**How a catch is decided.** This repo's test files are not all owned by this pass, and several
are being rewritten independently of it against the same v2 model -- at any given moment some
of them may not even collect (an old import, an old keyword argument). Comparing the mutated
run's return code to 0 would therefore say nothing about the mutation: the suite can easily be
red before a single byte is changed. So instead this compares two SETS of failing test node
ids -- one from an unmutated baseline run, one from each mutated run -- and only tests that
are newly failing (present in the mutated set, absent from the baseline set) count as having
caught the mutant. A test that was already broken before the mutation proves nothing about the
mutation, and must not be credited with catching it.

A mutant with no newly-failing test is a **missing assertion** and is reported as a finding,
not hidden and not fixed by editing someone else's test file from here.

**Run this alone, and do not edit the source while it runs.** Both hazards are the same
hazard: this pass holds a snapshot of every file it will touch, and anything that writes those
files meanwhile is either overwritten by a restore or overwrites one. During a single session
that silently applied `cap-ignores-working` to the tree, then `priority-inverted`, then
silently reverted a bug fix written while a pass was running -- three times, each time leaving
a green suite and a wrong system.

Two guards, because a rule nobody can check is not a guard. `.mutation_running` makes a second
pass refuse to start. And every restore is *conditional*: a file is put back only if it still
contains exactly what this pass wrote into it. If it does not, somebody else changed it, and
this pass says so loudly and keeps its hands off rather than reverting work it did not make.

**Why there is a backup directory.** A `finally` restores the file after each mutant, which is
enough for an exception and not enough for a signal. A run killed by `timeout`, Ctrl-C, or an
OOM leaves the tree MUTATED, and the next reader has no way to tell -- the code looks
deliberate, the tests still pass (that is what a surviving mutant means), and the report says
"pattern absent" for the one mutant that would have caught it. That happened, and the cap
silently stopped counting the working slot for as long as it took to notice.

So originals are written to `.mutation_backup/` before anything is touched, restored from there
on SIGINT/SIGTERM, and restored automatically at the start of the NEXT run if the directory is
still present. The directory existing at all means the previous run did not finish.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import signal
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
BACKUP = REPO / ".mutation_backup"
LOCK = REPO / ".mutation_running"

#: (label, file, find, replace, the invariant it breaks)
MUTANTS: list[tuple[str, str, str, str, str]] = [
    (
        "claude-science-cannot-be-cancelled", "adapters/claude_science/adapter.py",
        '        path = f"/api/frames/{partner_id_in_remote}/cancel"',
        '        raise Rejected("no_remote_cancel", "no cancel") or (\n'
        '            f"/api/frames/{partner_id_in_remote}/cancel")',
        "Claude Science goes back to refusing every cancel, so a displaced frame keeps "
        "running while the new instruction is delivered and the agent sees both",
    ),
    (
        "a-failed-cancel-is-swallowed", "adapters/claude_science/adapter.py",
        '        self._require_ok("POST", path, status, payload, ok=(200, 201, 202, 204))',
        "        pass",
        "a cancel that failed on the remote reports success, so a second instruction is "
        "delivered to an agent the caller believes has stopped",
    ),
    (
        "inheritance-unreachable-by-agents", "mcp_server/server.py",
        '                other_project_title=other_project_title or None,',
        "",
        "the extend_project tool drops the two-project form, so the project_extension "
        "row inheritance depends on can only be created from the library and no agent "
        "can ever link two gemini_ projects",
    ),
    (
        "a-failed-remote-invites-a-double-send", "messaging_core/core.py",
        "            exc.already_committed = True\n"
        "            raise\n"
        "        except (Rejected, NeedsRemote) as exc:",
        "            raise\n"
        "        except (Rejected, NeedsRemote) as exc:",
        "a RemoteFailure after admission escapes unmarked, so send renders it with "
        "'send the work again' while the task is already back in the queue",
    ),
    (
        "an-error-ends-the-exchange", "schema/schema.sql",
        "    ('[ERROR]',            2, NULL, 0, '[MESSAGE-RESPONSE]'),",
        "    ('[ERROR]',            2, NULL, 0, NULL),",
        "an [ERROR] is delivered, acted on, and the slot frees with nothing sent back, so "
        "a Caller that corrects a blocked Partner never learns the correction landed",
    ),
    (
        "a-hold-is-typed-at-the-agent", "messaging_core/core.py",
        '            if task["behavior"] == INTERRUPT_BEHAVIOR:\n'
        '                task["remote_call_id"] = None',
        '            if False and task["behavior"] == INTERRUPT_BEHAVIOR:\n'
        '                task["remote_call_id"] = None',
        "an [IDLE] is rendered and delivered, so a deliberately stopped agent is handed a "
        "paragraph to act on",
    ),
    (
        "a-partner-does-not-park-itself", "messaging_core/core.py",
        "            behavior in _RAISES_UPWARD",
        "            False and behavior in _RAISES_UPWARD",
        "a Partner that raises a question upward keeps working, so the next queued message "
        "reaches an agent blocked on an unanswered question and the two interleave",
    ),
    (
        "a-caller-parks-itself-dispatching-work", "messaging_core/core.py",
        "            and travelling_up\n",
        "",
        "the direction test is dropped, so an orchestrator stops itself every time it asks "
        "a worker anything and halts everything it drives",
    ),
    (
        "a-lineage-may-fork", "messaging_core/core.py",
        '                    "gemini_already_inherited",',
        '                    "duplicate_handshake",',
        "a second conversation may claim the same predecessor, so 'which conversation "
        "succeeds this one' has more than one answer",
    ),
    (
        "inheritance-costs-the-orchestrator-its-reach", "messaging_core/core.py",
        '                "  AND pr.source_prefix = \'science_\'",',
        '                "  AND pr.source_prefix != \'\'",',
        "gemini_single_science_source counts every inbound handshake again, so an inherited "
        "conversation becomes unreachable by the orchestrator that pays budget for it",
    ),
    (
        "a-notebook-is-asked-like-an-agent", "messaging_core/core.py",
        '        if task["behavior"] == "[QUERY]" and self._partner_type(partner) == "nlm_":',
        '        if False and self._partner_type(partner) == "nlm_":',
        "a notebook receives the agent relay, including an identity block inviting it to "
        "call send -- which it cannot, having can_send = 0",
    ),
    (
        "deleting-a-project-drops-work-in-flight", "messaging_core/core.py",
        "            if in_flight is not None:",
        "            if False and in_flight is not None:",
        "deleting a Project cascades away queued work with no way to tell whoever is "
        "waiting, and no notice can survive the DELETE that would warn them",
    ),
    (
        "delivers-into-an-unready-tui", "adapters/antigravity/adapter.py",
        "        if not self._await_idle(session):",
        "        if False and not self._await_idle(session):",
        "deliver_message types into a session that has not reached an input prompt -- "
        "into agy's trust dialog, where the message goes nowhere and every check "
        "afterwards reads as success",
    ),
    (
        "unstarted-turn-called-finished", "adapters/antigravity/adapter.py",
        "        if not self._saw_busy.get(session) and (",
        "        if False and not self._saw_busy.get(session) and (",
        "poll_completion reads an absent busy footer as FINISHED when the turn has "
        "merely not started, so the caller gets an empty body and the working slot is "
        "released while the agent is still about to answer",
    ),
    (
        "echo-skipped-by-one-line-only", "adapters/antigravity/adapter.py",
        "            kept = lines[self._past_echo(lines, last_body, echo_index) :]",
        "            kept = lines[echo_index + 1 :]",
        "read_remote_result slices one line into a multi-line prompt, so the caller "
        "receives its own instructions back with the answer buried at the end",
    ),
    (
        "search-has-no-relevance-floor", "messaging_core/core.py",
        "    if len(query) >= _MIN_SUBSTRING_LEN and query in (candidate_title or \"\").lower():\n"
        "        return True\n"
        "    return score >= _RELEVANCE_FLOOR",
        "    return True",
        "search returns the top N candidates whatever they scored, so a query matching "
        "nothing still hands an agent three confident titles and it addresses one",
    ),
    (
        "search-drops-a-deliberate-substring", "messaging_core/core.py",
        "    if len(query) >= _MIN_SUBSTRING_LEN and query in (candidate_title or \"\").lower():\n"
        "        return True\n",
        "",
        "an exact substring of a title is judged on its fuzzy ratio alone, so searching "
        "'worker' for 'research-worker' (0.571) finds nothing",
    ),
    (
        "archiving-tells-nobody", "messaging_core/core.py",
        '                self._report_lost_work(conn, row["id"])',
        "                pass",
        "archiving a partner drops every caller's in-flight work with no notice, so each "
        "one waits forever for a reply that was already discarded",
    ),
    (
        "delete-does-not-check-work-in-flight", "messaging_core/core.py",
        '                    "partner_has_work_in_flight",',
        '                    "not_authorized",',
        "the refusal that protects in-flight work from an irreversible delete is reported "
        "as an authorization failure, so the caller fixes the wrong thing",
    ),
    (
        "reverse-handshake-closed", "messaging_core/core.py",
        "                reverse_row = self.db.read_one(\n"
        '                    "SELECT id FROM handshakes WHERE from_partner = ? AND to_partner = ?",',
        "                reverse_row = None and self.db.read_one(\n"
        '                    "SELECT id FROM handshakes WHERE from_partner = ? AND to_partner = ?",',
        "a Partner can no longer answer the Caller that handshook it -- the reply "
        "direction closes and send refuses no_handshake, which is the state that made "
        "the research dispatch's own instruction to message back impossible to follow",
    ),
    (
        "research-travels-the-reverse-handshake", "messaging_core/core.py",
        '                        "research_needs_a_forward_handshake",',
        '                        "no_handshake",',
        "a worker delegating [RESEARCH] back to its own director is refused under the "
        "wrong code, so the caller is told to handshake rather than told the direction "
        "is the problem",
    ),
    (
        "notebook-id-never-recovered", "adapters/notebooklm/adapter.py",
        "                notebook_id = self._resolve_project_system_id(partner_id_in_remote)",
        "                notebook_id = None",
        "a NotebookLM adapter that did not itself register the partner can never find "
        "its notebook again, so every delivery after a restart fails and the caller is "
        "never told",
    ),
    (
        "project-id-never-recovered", "adapters/claude_science/adapter.py",
        "                project_id = self._resolve_project_system_id(frame_id)",
        "                project_id = None",
        "same for Claude Science: a frame's project cannot be recovered from the "
        "database, so a restarted process cannot deliver to any existing partner",
    ),
    (
        "resolver-crosses-sources", "mcp_server/config.py",
        "     WHERE p.partner_id_in_remote = ? AND pr.source_prefix = ?",
        "     WHERE p.partner_id_in_remote = ? AND ? IS NOT NULL",
        "the id resolver stops filtering by source, so a partner_id_in_remote that "
        "collides across two projects resolves to the wrong container -- and a "
        "NotebookLM adapter addresses a query at a Claude Science project id",
    ),
    (
        "label-order-ignores-fresh-arrival", "messaging_core/core.py",
        "          MIN(CASE WHEN q.in_process = 0 THEN q.enqueued_at END) ASC,",
        "",
        "_HEAD_LABEL_SQL ranks a label by its OLDEST row rather than its earliest "
        "FRESH one, so a [QUERY] holding a paused row plus a fresh one beats a fresh "
        "[ERROR] of equal priority and the correction is never delivered",
    ),
    (
        "idle-hold-spins", "polling/server.py",
        "                    stop_event.wait(self.hold_interval)",
        "                    stop_event.wait(max(self.poll_interval / 4, 0.0))",
        "an [IDLE] hold polls at four times the poll rate forever, for a partner "
        "deliberately stopped with nothing to poll",
    ),
    (
        "send-claims-nothing-changed-after-committing", "mcp_server/server.py",
        "            if getattr(exc, \"already_committed\", False):",
        "            if False and getattr(exc, \"already_committed\", False):",
        "send renders a post-admission failure as 'Nothing was changed.', so the agent "
        "retries and double-sends work the system already accepted",
    ),
    (
        "no-column-reconciliation", "messaging_core/db.py",
        "_ADDITIVE_COLUMNS: tuple[tuple[str, str], ...] = (",
        "_ADDITIVE_COLUMNS: tuple[tuple[str, str], ...] = () and (",
        "a database created before a column was added never gains it, so every read "
        "of that column fails with `no such column` against an existing deployment",
    ),
    (
        "drains-any-source", "polling/server.py",
        "        if source_prefix not in self.extensions:",
        "        if False and source_prefix not in self.extensions:",
        "a process spawns a drain thread for a Partner whose source it holds no "
        "extension for -- the thread raises no_extension on every pass, never retires, "
        "and its drain_threads row re-arms it after every restart",
    ),
    (
        "start-resumes-every-source", "polling/server.py",
        'f"WHERE pr.source_prefix IN ({placeholders})",',
        'f"WHERE 1=1 OR pr.source_prefix IN ({placeholders})",',
        "start() resumes drain threads for Partners this process cannot serve, "
        "recreating the doomed threads on every restart",
    ),
    (
        "supervisor-ignores-the-working-slot", "polling/server.py",
        "        for partner_id in self.core.slots.occupied():",
        "        for partner_id in []:",
        "the supervisor scans only the queue, so a task promoted into the working slot "
        "(which DELETES its queue row) is left with a remote mid-turn and no thread "
        "watching it",
    ),
    (
        "send-does-not-arm", "mcp_server/server.py",
        '                polling.ensure_partner_thread(partner_id=result["partner_id"])',
        "                pass",
        "send delivers work to a remote and arms nothing, so poll_completion is never "
        "called and the answer never reaches the Caller",
    ),
    (
        "summary-phase-not-carried", "messaging_core/core.py",
        '                            int(bool(working.get("summary_phase"))),',
        "                            0,",
        "a displaced summary phase is requeued without its marker, so on resume it is "
        "an ordinary [TRUTHFUL-REPORT] that owes nothing back and the research result "
        "is silently dropped",
    ),
    (
        "summary-resumed-as-a-plain-pause", "messaging_core/core.py",
        '        if task.get("summary_phase"):\n'
        "            return templates.truthful_report_request(",
        '        if False and task.get("summary_phase"):\n'
        "            return templates.truthful_report_request(",
        "a resumed summary phase falls through to the one-line resume prompt, which "
        "names nothing the agent is holding, instead of re-asking for the summary",
    ),
    (
        "cap-ignores-the-origin-label", "messaging_core/slots.py",
        '            if task["behavior"] == behavior or task.get("origin_behavior") == behavior:',
        '            if task["behavior"] == behavior:',
        "the working slot stops counting against the [RESEARCH] cap the moment "
        "begin_summary_phase relabels it, so a caller gets one more [RESEARCH] in flight "
        "than the cap allows for as long as the summary takes",
    ),
    (
        "admit-ignores-the-origin-label", "messaging_core/core.py",
        "           AND COALESCE(origin_behavior, behavior) = :behavior) + :working",
        "           AND behavior = :behavior) + :working",
        "_ADMIT_SQL stops counting a displaced summary row against the [RESEARCH] cap it "
        "was admitted under, so the cap leaks through the queue instead of the slot",
    ),
    (
        "reader-is-writable", "messaging_core/db.py",
        'uri = f"file:{quoted}?mode=ro"',
        'uri = f"file:{quoted}?mode=rw"',
        "the single-writer invariant: a reader connection can write again",
    ),
    (
        "priority-inverted", "messaging_core/core.py",
        " ORDER BY MIN(c.priority) ASC, MIN(q.in_process) ASC,",
        " ORDER BY MIN(c.priority) DESC, MIN(q.in_process) ASC,",
        "_HEAD_LABEL_SQL picks the LOWEST-priority label last instead of first, so a "
        "[RESEARCH] can win the working slot over a [QUERY] that stops work",
    ),
    (
        "in-process-ignored", "messaging_core/core.py",
        " ORDER BY q.in_process DESC, q.enqueued_at ASC, q.id ASC",
        " ORDER BY q.enqueued_at ASC, q.id ASC",
        "_HEAD_ROW_SQL drops the paused-first tie-break, so a paused task loses "
        "to a fresh arrival carrying the same label and a partner never resumes "
        "what it was already doing",
    ),
    (
        "displace-on-equal", "messaging_core/core.py",
        'head_priority >= working["priority"]:',
        'head_priority > working["priority"]:',
        "an arriving task of EQUAL priority wrongly displaces the working task, "
        "so two same-priority callers can ping-pong a partner forever",
    ),
    (
        "cap-ignores-working", "messaging_core/core.py",
        "           AND COALESCE(origin_behavior, behavior) = :behavior) + :working\n"
        "     < (SELECT max_outstanding FROM label_caps WHERE behavior = :behavior)",
        "           AND COALESCE(origin_behavior, behavior) = :behavior)\n"
        "     < (SELECT max_outstanding FROM label_caps WHERE behavior = :behavior)",
        "_ADMIT_SQL's cap counts only queued rows, not the one already being "
        "worked, so a caller capped at N ends up with N+1 in flight",
    ),
    (
        "displaced-not-paused", "messaging_core/core.py",
        '                        "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",\n'
        "                        (\n"
        "                            partner_id,\n"
        '                            working["caller_id"],',
        '                        "VALUES (?, ?, ?, ?, 0, ?, ?, ?)",\n'
        "                        (\n"
        "                            partner_id,\n"
        '                            working["caller_id"],',
        "advance()'s _swap requeues a displaced task as fresh (in_process=0) "
        "instead of paused, so it loses its resume tie-break and its resume "
        "prompt is wrong",
    ),
    (
        "idle-requeued", "messaging_core/core.py",
        "                # Requeuing it would stop the partner again the moment it\n"
        "                # resumed.\n"
        "                if working is not None and not holding:\n"
        "                    conn.execute(",
        "                # Requeuing it would stop the partner again the moment it\n"
        "                # resumed.\n"
        "                if working is not None:\n"
        "                    conn.execute(",
        "advance()'s _swap requeues a displaced [IDLE] hold instead of "
        "discarding it, so a cleared interruption re-interrupts the partner "
        "the moment it comes back around",
    ),
    (
        "in-process-crosses-labels", "messaging_core/core.py",
        " ORDER BY MIN(c.priority) ASC, MIN(q.in_process) ASC,\n"
        "          MIN(CASE WHEN q.in_process = 0 THEN q.enqueued_at END) ASC,",
        " ORDER BY MIN(c.priority) ASC, MIN(q.in_process) DESC,\n"
        "          MIN(CASE WHEN q.in_process = 0 THEN q.enqueued_at END) DESC,",
        "the paused-vs-fresh tie-break stops being scoped to one label, so a "
        "paused [QUERY] beats a fresh [ERROR] at the same priority and a Caller's "
        "correction is never delivered to the Partner it is correcting",
    ),
    (
        "retire-without-recheck", "polling/server.py",
        "                    with self._lock:\n"
        "                        if not self._has_work(partner_id):",
        "                    if True:\n"
        "                        if True:",
        "a drain thread retires without re-checking for work under the push "
        "lock, so a message admitted in the window between deciding to retire "
        "and exiting is queued with no thread and no drain_threads row -- and "
        "nothing ever picks it up",
    ),
    (
        "drain-row-survives", "polling/server.py",
        '"DELETE FROM drain_threads WHERE partner_id = ?", (partner_id,)',
        '"SELECT partner_id FROM drain_threads WHERE partner_id = ?", (partner_id,)',
        "PollingServer._deregister leaves the drain_threads row behind when its "
        "thread retires, so the next push believes a thread is already running "
        "and spawns none -- the queued message is never picked up",
    ),
    (
        "store-everything", "messaging_core/core.py",
        'return bool(row["stored"]) if row is not None else False',
        "return True",
        "MessagingCore._stored always answers True, so [RESEARCH] and [ERROR] "
        "-- transport-only per label_caps.stored -- are written to `messages`",
    ),
    (
        "orchestrator-role-open-to-any-source", "messaging_core/core.py",
        '        if project is None or project["source_prefix"] != "science_":',
        '        if project is None or orchestrator_type == "never-happens":',
        "claim_orchestrator stops restricting orchestrator roles to Claude "
        "Science, so an Antigravity or NotebookLM partner can claim "
        "project-orchestrator or bridge-scientist",
    ),
    (
        "deliver-does-not-wait-for-the-turn", "adapters/antigravity/adapter.py",
        "        saw_busy = self._await_busy(session)",
        "        saw_busy = True",
        "deliver_message returns before the remote has visibly started, so the "
        "next poll_completion reads a stale idle pane and the drain thread "
        "closes a task the agent has not begun",
    ),
    (
        "role-reclaimable", "schema/schema.sql",
        "CREATE UNIQUE INDEX one_orchestrator_per_project_role",
        "CREATE INDEX one_orchestrator_per_project_role",
        "one_orchestrator_per_project_role is no longer UNIQUE, so a project "
        "can end up with two live partners holding the same orchestrator role",
    ),
]


def _snapshot(paths: list[pathlib.Path]) -> None:
    """Copy every file this run will touch into BACKUP, flat, keyed by a safe name."""
    BACKUP.mkdir(exist_ok=True)
    for path in paths:
        rel = path.relative_to(REPO)
        (BACKUP / str(rel).replace("/", "__")).write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8"
        )


def _restore_all() -> list[str]:
    """Put every backed-up file back. Returns the ones that had actually drifted."""
    if not BACKUP.is_dir():
        return []
    changed = []
    for saved in sorted(BACKUP.iterdir()):
        target = REPO / saved.name.replace("__", "/")
        good = saved.read_text(encoding="utf-8")
        if not target.exists() or target.read_text(encoding="utf-8") != good:
            target.write_text(good, encoding="utf-8")
            changed.append(str(target.relative_to(REPO)))
    return changed


def _clear_backup() -> None:
    shutil.rmtree(BACKUP, ignore_errors=True)


def _install_signal_restore() -> None:
    """Restore and exit on a signal. A `finally` does not run for SIGTERM."""

    def handler(signum, _frame):
        changed = _restore_all()
        _clear_backup()
        LOCK.unlink(missing_ok=True)
        print(f"\n[signal {signum}] restored {len(changed)} mutated file(s) before exiting.")
        sys.exit(130)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)


def _run_pytest() -> str:
    """One pytest pass over the whole repo. Returns captured stdout.

    `--continue-on-collection-errors` matters here specifically because not
    every test file in this repo is finished being rewritten against the v2
    model; a collection error in one of them must not prevent every other
    test from running and being counted.
    """
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q", "--no-header", "-rf", "--tb=no",
            "-p", "no:cacheprovider", "--continue-on-collection-errors",
        ],
        cwd=REPO, capture_output=True, text=True, timeout=900,
    )
    return proc.stdout


def _failing_tests() -> set[str]:
    return set(re.findall(r"^FAILED (\S+)", _run_pytest(), re.M))


def main() -> int:
    print("=" * 92)
    print("MUTATION PASS -- a surviving mutant is a missing assertion")
    print("=" * 92)

    # Refuse to start if another pass is already mutating this tree. Two runs
    # will each restore to their own snapshot and clobber each other.
    if LOCK.exists():
        print(
            f"REFUSING TO START: {LOCK.name} exists, so another mutation pass is already "
            "running against this tree. Two at once corrupt each other's restores. Wait for "
            "it, or delete the lock if you are certain it is stale."
        )
        return 2
    LOCK.write_text("running\n", encoding="utf-8")

    # A backup directory left behind means the previous run was killed rather
    # than finished. Restore before measuring anything, or the baseline is taken
    # against a mutated tree and every number after it is meaningless.
    stale = _restore_all()
    if stale:
        print(
            f"RECOVERED: the previous run did not finish and left {len(stale)} file(s) "
            "mutated. Restored before starting:"
        )
        for f in stale:
            print(f"  - {f}")
        print()
    _clear_backup()

    _install_signal_restore()
    _snapshot(sorted({REPO / m[1] for m in MUTANTS}))

    baseline_failing = _failing_tests()
    if baseline_failing:
        print(
            f"baseline: {len(baseline_failing)} pre-existing failing test(s), excluded from "
            "catch credit below (see module docstring -- some test files in this repo are "
            "mid-rewrite against the v2 model, independent of this pass):"
        )
        for t in sorted(baseline_failing):
            print(f"  - {t}")
    else:
        print("baseline: green (0 failing tests)")
    print()

    survivors: list[tuple[str, str]] = []
    rows: list[tuple[str, int, str]] = []
    foreign: list[str] = []

    for label, relpath, find, replace, invariant in MUTANTS:
        path = REPO / relpath
        original = path.read_text(encoding="utf-8")
        if find not in original:
            rows.append((label, -1, "NOT APPLIED -- pattern absent"))
            survivors.append((label, "pattern not found; mutant could not be applied"))
            continue
        mutated = original.replace(find, replace, 1)
        if mutated == original:
            # A replacement identical to the original changes no behaviour --
            # this suite has had exactly that bug before (see the module
            # docstring), so it is checked for explicitly rather than trusted
            # to produce a real failure downstream.
            rows.append((label, -1, "NOT APPLIED -- replacement is a no-op"))
            survivors.append((label, "replacement produced no change; mutant does not mutate"))
            continue
        try:
            path.write_text(mutated, encoding="utf-8")
            failing = _failing_tests()
            newly = sorted(failing - baseline_failing)
            if newly:
                first = newly[0].split("::")[-1]
                extra = f" (+{len(newly) - 1} more)" if len(newly) > 1 else ""
                rows.append((label, len(newly), f"caught by {first}{extra}"))
            else:
                rows.append((label, 0, "SURVIVED"))
                survivors.append((label, invariant))
        finally:
            # Conditional restore. Put the file back ONLY if it still holds
            # exactly what this pass wrote -- otherwise something else edited it
            # while the pass was running, and reverting would silently destroy
            # that work. This has happened: a real bug fix, written during a
            # background run, was reverted by the restore and the suite stayed
            # green for another twenty minutes.
            current = path.read_text(encoding="utf-8")
            if current == mutated:
                path.write_text(original, encoding="utf-8")
                if path.read_text(encoding="utf-8") != original:
                    print(f"\nFATAL: could not restore {relpath} after {label}. Stopping.")
                    _clear_backup()
                    LOCK.unlink(missing_ok=True)
                    return 2
            elif current != original:
                foreign.append(relpath)
                print(
                    f"\nWARNING: {relpath} was modified by something else while {label} was "
                    "applied. NOT restoring it -- that would destroy the change. This run's "
                    "results for this file are void."
                )

    if foreign:
        print(
            f"\nRESULTS VOID for {sorted(set(foreign))}: the file changed underneath this run. "
            "Re-run with nothing else touching the source."
        )
    _clear_backup()
    LOCK.unlink(missing_ok=True)

    print(f"{'mutant':<26}{'newly failing':>14}  caught by")
    print("-" * 92)
    for label, n, note in rows:
        shown = "-" if n < 0 else str(n)
        print(f"{label:<26}{shown:>14}  {note}")

    print()
    tree_ok = all((REPO / relpath).read_text(encoding="utf-8") for _, relpath, *_ in MUTANTS)
    restored_failing = _failing_tests()
    print(f"tree restored: {tree_ok}")
    print(f"failing set back to baseline: {restored_failing == baseline_failing}")

    print()
    if survivors:
        print(f"{len(survivors)} SURVIVING MUTANT(S) -- each is a missing assertion:")
        for label, invariant in survivors:
            print(f"  - {label}: {invariant}")
    else:
        print(f"all {len(MUTANTS)} mutants caught by a named test")
    return 0 if (restored_failing == baseline_failing and not survivors) else 1


if __name__ == "__main__":
    raise SystemExit(main())
