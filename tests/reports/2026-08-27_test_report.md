# Test report — 2026-08-27

The v2 messaging model (one priority queue, in-memory working slot, permission tools) replaced
a model with two directional queues and a five-state machine. Every test file was rewritten
against it; none was adapted. Four agents wrote them in parallel, each owning a disjoint set of
files, with the spec supplied rather than read from the docs — the docs were being rewritten at
the same time and would have taught them the old model.

## Totals

| | |
|---|---|
| Tests | **331 passed**, 0 failed, 0 skipped, 0 xfail |
| Schema assertions | **63 passed**, 0 failed |
| Mutants | **14**, all caught by a named test |
| Source bugs found | **13** fixed, plus 1 spec gap closed by audit |

## Per file

### tests/test_foundation.py — 60 tests

The database layer, the label vocabulary, the working slot, and the prompt templates.

Claims: writes go through one writer thread inside `BEGIN IMMEDIATE`; readers are `mode=ro`
and **a `PRAGMA query_only = OFF` issued through the read API does not restore write access**
(the OS-level file descriptor is the actual guarantee, not the pragma); `WorkingSlots` locks
are per partner, identical per partner, re-entrant, and `outstanding` requires both caller and
behavior to match; templates contain every path they are given and none they are not, name the
behavior they are given, and quote the original request verbatim.

The lock re-entrancy test acquires with a bounded timeout so a regression fails loudly rather
than hanging the suite.

Templates are asserted on behavior, never on exact wording — a rewording is a refactor.

### tests/test_schema_suite.py — 2 tests

Runs `test_schema_constraints.py` and checks the shipped schema is byte-identical to the vault
copy.

### tests/test_schema_constraints.py — 63 assertions

Not pytest; a standalone script asserting the constraints **bite**, not that the DDL parses.

Covers: title uniqueness across archived rows and the rename trigger; `partner_id_in_remote`
unique per project; one orchestrator per project per role; the live-partner ceiling and that
archiving frees a slot; the `(caller, label)` cap in one statement at both cap values; all six
`label_caps` rows and their relative priorities; that the three answerable labels are exactly
the stored ones; that a label cannot reply with itself; every `agent_layers` row and that a
specific role beats its source's `'*'` default; the two NotebookLM booleans; the four cascades.

Two assertions are worth naming because they test that a rule has **one** authority rather than
two agreeing copies. Flipping `label_caps.stored` for `[ERROR]` changes what `messages`
accepts — proving the trigger reads the table rather than carrying its own list. And
`message_queue` is asserted to have **no** `dequeued_at` column, because a promoted row is
deleted and such a column could only ever be written to a row about to disappear.

### tests/test_core_capabilities.py — 96 tests

Every capability. Highlights, by what would otherwise go untested:

- `create_partner`'s duplicate-remote-id path includes a **concurrency race** proving the
  database constraint settles it, not the pre-check.
- `send`'s `research_cannot_flow_upward` is paired with an assertion that the **same pair can
  send `[QUERY]` upward** — a refusal test alone does not show the rule is scoped to one label.
- `over_queue` is tested on both sides of the boundary (second admitted, third refused), across
  two different callers, and against an uncapped label.
- 16 handshake tests covering every named rejection, including `code_`'s exemption from the
  orchestrator requirement and the gemini-orchestrator-cannot-inherit case across an extension.
- 15 tests on the queue machinery: equal priority does not displace, paused outranks fresh of
  its own label but not a higher-priority one, delivery failure requeues rather than dropping,
  an archived partner's work is discarded.
- `permission_not_applied` **with nothing written locally** — the single most important test in
  the permission group, reachable only because `StubExtension.permissions_refuse` lets the fake
  remote accept a write and quietly not apply it.

### tests/test_boundaries.py — 46 tests

Attacks nine stated boundaries rather than confirming them. Every one held after the fixes
below. Negative results are recorded in the file so the next person knows what has already been
tried.

Attacked: the cap (12-thread barrier hammer, plus a push fired while a swap is deliberately
blocked mid-delivery); priority (ping-pong between equal-priority callers, paused-vs-fresh
across labels, an `[IDLE]` returning after displacement); the hierarchy (through `send`, through
`report_back`, through `interrupt_partner`); handshake legality (every combination, including
two that first had to exploit an adjacent gap to reach the check under test); identity
disclosure (title-as-uuid, archived vs never-existed producing byte-identical refusals,
`grant_gemini_budget`'s authorization gate running before any query touches the target);
archived partners; permissions against a remote that accepts and does not apply; write
discipline; and the approval doctrine, by sweeping every extension class for any
prompt-answering method.

Two of its own tests were rebuilt after they were found to be weak:

- `test_paused_task_beats_fresh_task_of_the_same_label_only` originally had chronology
  accidentally agreeing with the correct answer, which masked the `in-process-ignored` mutant.
- `test_cap_counts_the_working_slot_deterministically` was added because the concurrent
  hammering test only exposed `cap-ignores-working` by thread-scheduling luck.

Finding a suite's own tests to be luck-dependent is the mutation pass doing its job.

### tests/test_polling_working_slot.py — 17 tests

Replaces the deleted `test_polling_state_machine.py`.

Covers retirement deleting the `drain_threads` row (joining the real thread and re-reading the
table); `stop()` **preserving** the row and `start()` respawning from it; push idempotency;
both round-trip shapes; `[IDLE]` as a hold that is not polled and does not retire;
`NeedsRemote` from `poll_completion` treated as finished; a drain loop surviving an extension
exception; `stop()`/`start()` each safe twice; a missing extension losing nothing.

**Test 7 is the one that matters most**: `[ERROR]`, `[MESSAGE-RESPONSE]` and a directly-sent
`[TRUTHFUL-REPORT]` each produce nothing back. Without it, two agents reply to each other until
one is archived.

**Test 17 forces a race rather than waiting for one.** A wrapper blocks the drain thread at the
moment it decides to retire, a message is admitted into that window, and the test asserts it is
still picked up. A test that hoped to hit this by timing would pass on a fast machine and stay
silent about the bug.

### tests/test_extension_points.py — 27 tests

The four abstract methods (parametrized: missing any one and the class cannot be instantiated);
the five concrete-refusing defaults; `NonExecutingExtension`; `StubExtension` including
`permissions_refuse` in both directions.

Two tests assert **absence** and say so in a comment, because absence is the enforcement
mechanism here rather than a shape check: `resume_remote_execution` exists on no class, and no
class exposes any method beyond the documented nine — which is what "nothing answers a
permission prompt" means operationally.

### tests/test_adapters.py — 56 tests

**The 7 Antigravity permission tests in this file were rewritten twice, and the reason is the
most useful thing in this report.** The first version was written against a *guessed*
`/permissions` key sequence. It passed cleanly, with realistic-looking fakes, and described
something the CLI does not do.

Driving it against a live `agy` session corrected four things at once — the store key
(`permissionGrants.permissionGrants.allow`, not `projectResources`), the number of Enters
(three screens, not one), how many Escapes it takes to leave, and that deleting must navigate
to a rule by name rather than press a key per rule. Every pane fixture in the file is now
reproduced from that session rather than invented.

Two of the replacement tests were *also* wrong on first writing, in a way worth naming: the
delete fake handed back a pane with the cursor **already on the target**, so a navigation test
passed having sent zero keypresses. The fake now models a real list — the cursor moves on
Up/Down, `d` removes an entry — so the keypress count is a measurement rather than a
formality. **A fake that agrees with the code proves nothing about either.**

### tests/test_adapters.py — the rest

NotebookLM and Claude Science **refuse** all three permission operations, asserted with the
transport (`subprocess.run`, `urllib.request.urlopen`) patched to explode if touched at all —
so the test proves nothing reached the remote, not merely that an exception was raised.

Antigravity's 15 permission tests cover: `get_permissions` reading the real config file shape
(missing file → `[]`, unreadable/empty id file, malformed JSON, and string/list/dict
flattening, all with tmux hard-blocked to prove it never touches the pane); `add_permissions`'
exact tmux key sequence, its refusal to type anything when the editor never opens, that
`Escape` is sent even when typing raises, and that it never claims success;
`delete_permissions`' refusal when no removal key is advertised.

### tests/test_mcp_surface.py — 10 tests

Behavior, not inventory. The previous version pinned a hard-coded list of 15 tool names, which
breaks on every rename and would not notice a dropped constraint. What replaced it: no tool
takes a `requester_title`; `send` accepts no `role` and no path arguments; and **every** tool
discovered from the live `list_tools()` turns both a `Rejected` and a `NeedsRemote` into a
response body rather than propagating.

### tests/test_mcp_config.py — 3 tests

Unchanged. Asserts the server builds a real adapter by default and a stub only under
`MESSAGING_MCP_STUB=1` — the regression that once made every deployment silently talk to a fake.

## Mutation pass — 12 mutants, all caught

| Mutant | Rule it breaks |
|---|---|
| `reader-is-writable` | reader connections open `mode=rw` |
| `priority-inverted` | the label query orders priority `DESC` |
| `in-process-ignored` | the row query drops the paused-first tie-break |
| `in-process-crosses-labels` | the tie-break stops being scoped to one label |
| `displace-on-equal` | an equal-priority arrival displaces the working task |
| `cap-ignores-working` | the cap counts queued rows only |
| `displaced-not-paused` | a displaced task is requeued without `in_process` |
| `idle-requeued` | a displaced `[IDLE]` comes back |
| `retire-without-recheck` | retirement skips the re-check under the push lock |
| `drain-row-survives` | a retiring thread leaves its row behind |
| `store-everything` | every label is written to `messages` |
| `role-reclaimable` | the orchestrator index is no longer `UNIQUE` |

A catch is credited only when a test is **newly** failing against an unmutated baseline. A test
already failing before the mutation proves nothing about it and must not be credited — an
earlier version compared the whole-suite return code, which reported false catches whenever any
unrelated file was mid-rewrite.

### What the mutation pass did to the source, and what now stops it

Three times in one session, a mutation pass corrupted the working tree and left the suite green
— which is precisely what a surviving mutant looks like, so nothing said so:

1. `cap-ignores-working` left applied. The cap stopped counting the working slot.
2. `priority-inverted` left applied. The entire cross-label queue order inverted — and a test
   agent then reported the inversion as a source bug, in good faith, having measured a mutated
   tree.
3. A real bug fix, written while a pass was running in the background, was **reverted** by that
   pass's restore.

Causes: (1) and (2) were two passes running concurrently, each restoring to its own snapshot;
(3) was editing source during a run. Both are the same hazard — the pass holds a snapshot, and
anything else writing those files loses.

`tests/mutation_run.py` now: refuses to start while `.mutation_running` exists; snapshots to
`.mutation_backup/` and self-heals at the start of the next run if killed; restores on
SIGINT/SIGTERM, which a `finally` does not cover; and — the one that actually closes case (3) —
restores a file **only if it still contains exactly what the pass wrote into it**. If it does
not, something else changed it, and the pass says so loudly and keeps its hands off rather than
reverting work it did not make.

Two spot-checks catch the worst mutants by hand, both recorded in the runbook:
`_HEAD_LABEL_SQL` must read `MIN(c.priority) ASC`, and `_ADMIT_SQL` must contain `+ :working`.
Both leave the suite green while the system is wrong.

## Source bugs found by the tests, all fixed

| Bug | Found by | Why it mattered |
|---|---|---|
| `_render` dropped an interruption's reason on retry — a failed `[IDLE]` delivery came back as "Resume your previous `[IDLE]`" with the reason unused in the row | the mutation agent | the Caller's stated reason for stopping a Partner was removed from the retry |
| `_complete` inferred "research summary" from the label, so a directly-sent `[TRUTHFUL-REPORT]` was answered with another one, forever, each hop spawning a drain thread | the polling agent | the unbounded exchange `reply_behavior IS NULL` exists to prevent, reintroduced by a special case |
| `report_back` applied no hierarchy check, so `[RESEARCH]` could be routed upward through it | the boundary agent | `send` refuses it; anything holding a `MessagingCore` could use the other door |
| the paused-vs-fresh tie-break was global, not per-label, so a paused `[QUERY]` beat a fresh `[ERROR]` | the core-capabilities agent | a Partner interrupted mid-question never received the correction its Caller sent — the route the approval doctrine depends on |
| a drain thread retiring in the same window as a push stranded the message with no thread and no row | suspected by the polling agent, then **retracted**; confirmed here by forcing the window | the liveness failure `drain_threads` exists to prevent, by the other door |
| a malformed Antigravity config raised `AttributeError` instead of refusing | the adapter agent | it would surface inside `add_permissions`' verify step, reading as "the grant did not land" |
| `get_permissions` read `projectResources`; the allowlist is at `permissionGrants.permissionGrants.allow` | the live run | it returned `[]` for a fully granted conversation — indistinguishable from a fresh install |
| `/permissions` needs two Enters, not one | the live run | the adapter would sit on the scope selector believing it was on the rule list |
| closing needed more than one Escape | the live run | the session was left inside the editor, where the next `deliver_message` is swallowed with nothing reported |
| `delete_permissions` reused a pane made stale by the previous deletion | the live run | it refused rather than deleting the wrong rule — the guard held — but the index was computed against rows that no longer existed |
| an exception in the closing `finally` masked the original one | rewriting the tests | the caller was handed "stuck in the editor" instead of "could not reach the add screen" — a symptom in place of a cause, with a different fix |
| `deliver_message` returned before the remote had visibly started | **live testing, and only live testing** | a round trip reported complete in 0 seconds; the agent answered into a pane nobody was watching, and nothing raised |
| `stop()` could raise joining an unstarted thread | this pass | `stop()` is documented as always safe to call |

The retracted one is worth dwelling on. The agent found a symptom, could not reproduce it
reliably, noticed it had the same signature as the mutation interference above, and withdrew
the claim rather than assert it — explicitly flagging that it had not re-confirmed against a
clean tree. That was the right call on the evidence it had, and the finding was real. **A
retraction is not the same as a refutation**, and a retracted finding with a stated reason is
worth re-testing rather than filing away.

## One reported bug that was not a bug

An agent reported `archive_sessions` as missing an orchestrator check, citing a comment on
`delete_partner` that called the two "scoped exactly like" each other. The spec requires only
same-project membership for archiving. The defect was the **comment**; the code was right, and
the two authorities differ deliberately — archiving frees a slot and leaves the row and its
spent title in place, deletion is irreversible. The comment was corrected and the test removed,
because it encoded a requirement nobody had made.

## What live testing caught that the suite structurally could not

Every fake in this suite is synchronous: it returns the state the test sets. The delivery bug
lives entirely in the gap between a keystroke and a terminal repaint, which only a real TUI
has — so no fake could have exhibited it, and no assertion could have failed.

That gap has now produced three defects: a pane captured mid-repaint during the permissions
walk (which looked exactly like the terminal swallowing input), a rule list gone stale between
deletions, and a delivery reported finished before the agent started. The suite pins each fix,
but it could not have found any of them.

**The rule that generalises:** a remote whose state you *observe* rather than *ask* can report
the state from before your action. Wait for the state you expect; never trust the first read.

## The escalation, and how it was resolved

`claim_orchestrator` restricted only `gemini-orchestrator` to `science_` projects, leaving a
`gemini_` partner free to claim `project-orchestrator` or `bridge-scientist`. Every path it
could be exploited through turned out to be independently guarded, so no named boundary was
crossed. No test was written either way, because the spec was silent and a test would have
frozen a guess.

**The owner ruled: all three roles are Claude Science roles.** That is the right outcome, and
the reasoning that deferred it was the wrong shape — "currently unreachable" describes today's
call graph, not a rule, and the next capability to take an `orchestrator_type` would have
inherited the gap silently.

Five tests changed as a result, and the interesting ones are the two that had to be **weakened
in their setup and strengthened in their claim**. `test_two_gemini_partners_can_never_handshake`
and its `gemini_ -> science_` sibling used to *claim a role* to get past the orchestrator gate
and reach the deeper check. That route is closed, so they now assert the refusal an agent
actually meets, then force the role straight into the database to prove the deeper check is
still live rather than dead code. Two new mutants pin both new rules.
