# Feature report — 2026-08-27

## What this round was

Two hand-written audits (`Development/Human audits/`) that **reversed decisions the system was
built on**, rather than requesting repairs. This is a v2 of the messaging model, not a patch
set, and the pieces it touched were the ones with the most tests and documentation behind them.

## What was reversed, deliberately

**Forward/backward routing by causal role is gone.** One priority queue per Partner replaces
two directional ones. `CausalRole`, `queue_for`, and `send`'s `role` parameter no longer exist.

The old model was not wrong about the problem — capping a continuation really can deadlock a
pair, and its uncapped backward queue really did prevent that. It solved it by asking the
caller to classify its own message, and a caller that classifies wrongly gets a deadlock back.
The priority queue solves it structurally: an answer outranks the work waiting for it, so it
cannot be stuck behind it.

**`polling_tasks` and `open_issues` are gone**, and the five-state machine with them. The
concept of a poll task survives — a task is queued, or it holds the working slot — but there is
no longer a state column that can disagree with where the task actually is.

**`resume_partner` is gone**, and with it `resume_remote_execution`, so the extension interface
has **four** abstract methods rather than five. Once permissions are configured in advance,
correcting a grant and sending again *is* the resumption; a resume action was a state with
nothing left to do.

**`code_` may now hold a handshake**, where the rule was a blanket ban.

## Schema

12 tables (was 12, but four are new and three are gone), 5 triggers (was 2).

New: `label_caps`, `agent_layers`, `project_extension`, `message_queue`.
Gone: `forward_queue`, `backward_queue`, `polling_tasks`, `open_issues`, `notify_targets`,
`cursors`.

New columns: `source_caps.can_send`, `source_caps.accepts_research`. Removed:
`source_caps.max_queue` — a queue limit is not a property of the source.

New triggers: `messages_stored_labels_only`, `budget_grants_roles_insert`,
`partner_paths_gemini_only`.

Two decisions in the schema are worth naming because both are about **not** adding something.

`messages.behavior` is a foreign key to `label_caps` plus a trigger reading
`label_caps.stored`, rather than a `CHECK` listing the three stored labels. A list would be a
second copy of one fact, and two copies eventually disagree. A schema assertion flips `stored`
and confirms the trigger follows.

`message_queue` has **no `dequeued_at`**. A promoted row is deleted, so such a column could
only ever be written to a row about to disappear. The start time lives on the in-memory working
slot and is reported by `status`. This was caught by the same audit pass that once let
`polling_tasks.phase` ship with zero writers.

## New modules

`messaging_core/slots.py` — the working slot, per Partner, in memory and never in SQLite.
Locking is per Partner rather than global, because a swap holds its lock across a remote round
trip; under one lock every drain thread would queue behind whichever remote was slowest.

`messaging_core/templates.py` — the five prompt shapes that actually reach a remote. Each
mirrors one in the project note "Prompt templates".

## The queue

`MessagingCore.advance` is the **single** implementation of "compare the head against the
working slot and act". `send`, `interrupt_partner`, and the drain thread all call it. The
previous design had two copies, in the core and the polling server, which is a defect the
invariants document used to warn about; the warning is now obsolete because the duplication is
gone.

Rules that carry weight, each with the failure it prevents:

- **Strict win to displace.** An arriving `[QUERY]` must not displace a `[QUERY]` being
  answered, or two Callers ping-pong a Partner and neither gets an answer.
- **Stop the remote before touching the queue.** A stop that fails then leaves everything as it
  was. The other order leaves one task in two places.
- **`in_process` outranks only within its label.** A paused `[RESEARCH]` resumes before a fresh
  one, and still waits behind a `[QUERY]`, so an interrupted Partner answers the question that
  interrupted it.
- **`[IDLE]` enters by priority and leaves by anything.** Comparing priorities on the way out
  would make an interruption permanent, since nothing outranks `[IDLE]`. It is discarded rather
  than requeued, since requeuing would stop the Partner again the moment it resumed.
- **Four labels reply with nothing.** `label_caps.reply_behavior` is NULL for them, and that
  NULL is what terminates an exchange. Without it, every completed task produces a message that
  produces a task.

## Permissions

Three standalone capabilities — `get_permissions`, `add_permissions`, `delete_permissions` —
and `send` is deliberately **not** one of them. A permission prompt means the grant was missing
*before* the work started, so configuring paths as a side effect of sending work is one step
too late by construction.

The Antigravity store was located and confirmed: `~/.gemini/config/projects/<project-id>.json`,
`projectResources`, with the id from
`~/.gemini/antigravity-cli/cache/default_project_id.txt`. Confirmed by reading the file against
what the TUI displayed — `{}` beside `allowlist (0)`.

Two candidate stores were investigated and **rejected on evidence**:
`~/.gemini/antigravity-cli/settings.json` is global, so writing it grants every conversation;
and the conversation's own `.db` still held `write_file(/)` while the TUI read `allowlist (0)`,
so it is not the list the UI reads.

`get_permissions` reads the file. `add_permissions` / `delete_permissions` write through the
`/permissions` view over tmux and the caller verifies against the same file. The asymmetry is
deliberate: reading a file is exact, typing into a TUI is not, so the typed half is the half
that has to prove it worked. `_apply_and_verify` raises `permission_not_applied` and records
nothing when the read-back disagrees.

**Then it was driven against a live `agy` session, and that found four more bugs.** The read
half had been "verified" by observing `projectResources: {}` beside a TUI reading
`allowlist (0)`. Empty matched empty, which is evidence of nothing — and the moment a real rule
existed, the TUI read `allowlist (1)` and `get_permissions` still returned `[]`.

1. **The store key was wrong.** The allowlist is at `permissionGrants.permissionGrants.allow`,
   not `projectResources`, which stays `{}` forever.
2. **`/permissions` is three screens, not one.** `/permissions` + Enter only picks the command
   from the palette and lands on a scope selector; a *second* Enter reaches the rule list. The
   adapter sent one, so it would have sat on the scope selector believing it was on the list.
3. **Closing needed more than one Escape.** The editor is two deep, so the session was left
   *inside* it — where the next `deliver_message` is read as editor input and swallowed with
   nothing reported anywhere.
4. **Delete reused a stale pane.** After one deletion the list renumbers; the second computed
   an index into rows that no longer existed. It refused rather than deleting the wrong rule —
   the guard held — but it now re-reads before every removal.

Also removed: `no_remote_permission_removal`. The editor's footer advertises `d/⌫ Delete rule`
and `d` deletes immediately, so revocation works — by finding the rule by name, moving the
cursor onto it, and confirming it is there before pressing a key that deletes without asking.

**The lesson, stated once because it generalises:** a mapping is confirmed only when a
**non-empty** value appears on both sides. Two things that are both nothing agree trivially.

**One honest limit remains.** Antigravity permissions are project-scoped, not
conversation-scoped, so `partner_id_in_remote` is accepted for symmetry and unused by
`get_permissions` — and there is no such thing as a scratch grant for testing.

**Verified end to end through `MessagingCore`**, not merely the adapter: `get_permissions`
reported real drift, `add_permissions` granted only what was new and reported the already-held
rule as unchanged, the verify-after-write passed, `partner_paths` recorded the intent, and the
follow-up drift check came back clean. Every test rule was then revoked and the project config
left byte-equivalent to the baseline captured before the run.

## Handshake and hierarchy

`code_` ↔ `bridge-scientist`, at most one each way. The `code_` branch is checked **first**,
before the "must hold an orchestrator role" rule, because all three roles are Claude Science
roles and a `code_` Partner can never hold one.

A `bridge-scientist` may otherwise reach only the `project-orchestrator`.

`project_extension` allows cross-Project handshakes, but only between the **same** orchestrator
role — an extension branches an effort sideways, it is not a second chain of command. It grants
nothing within a single Project, and cannot: the two ids would have to be equal.

`agent_layers` places every agent, and governs exactly one rule: `[RESEARCH]` never travels
upward. Every other label travels freely, which is what lets an answer come back.

## Bugs found and fixed during this round

**`_render` dropped an interruption's reason on retry.** An `[IDLE]` whose delivery failed was
requeued with `in_process = 1` and then re-rendered as "Resume your previous `[IDLE]`" — the
stop reason silently removed from the retry while it still sat in the row's `body`. Found by
the test agent that owned `mutation_run.py`, reproduced, and fixed by checking the label before
the paused flag. An `[IDLE]` is a hold and is never resumed.

**A malformed Antigravity config crashed instead of refusing.** Valid JSON that is not an
object (a top-level array, or a `projectResources` that is a list) raised `AttributeError`
four frames from the file that caused it — and would have propagated out of
`add_permissions`' verify step, where it would read as "the grant did not land" rather than
"the config is wrong". Found by the adapter test agent, reproduced, fixed to raise
`antigravity_project_unreadable`.

**`stop()` could crash on an unstarted thread.** `_spawn_drain_thread` registers a thread
before starting it, so a `start()` that failed left an unstarted thread in the map — and
`stop()`, documented as always safe to call, would be the thing that raised. Guarded.

**The pop order was wrong, and the mistake is worth keeping.** `in_process` was documented as a
tie-break *within* a label and implemented as a global one — a single `ORDER BY priority,
in_process DESC, enqueued_at`. `[QUERY]` and `[ERROR]` share priority 2 deliberately, so the
difference was reachable: a Partner interrupted mid-`[QUERY]` was handed "resume your previous
`[QUERY]`" instead of the `[ERROR]` its Caller had just sent explaining what went wrong. The
correction was never delivered — and that is precisely the route by which a blocked Partner
gets unblocked under the approval doctrine.

Reading the head is now two statements, because one `ORDER BY` cannot say "within a label":
`_HEAD_LABEL_SQL` picks the label, `_HEAD_ROW_SQL` picks the row within it. Found by the test
agent that owned `test_core_capabilities.py`, which noticed the code contradicted the module's
own stated invariant and worked around it in its test rather than asserting the wrong behavior
as correct.

**A directly-sent `[TRUTHFUL-REPORT]` replied with another one, forever.** `PollingServer._complete`
inferred "this is a research summary" from the label, and a `[TRUTHFUL-REPORT]` in the working
slot can equally be one an agent sent directly — which owes nothing back, because it already
*is* the report. So a directly-sent report was answered with a report, whose completion was
answered again, each hop spawning a fresh drain thread: the unbounded exchange
`label_caps.reply_behavior IS NULL` exists to prevent, reintroduced by a special case.
`begin_summary_phase` now sets an explicit `summary_phase` marker on the slot and `_complete`
checks that instead of the label.

Worth recording how this was nearly missed. My own round-trip probe drained the Caller's side
four times and printed the resulting worker queue — which contained the bounced
`[TRUTHFUL-REPORT]`. I read that output as evidence of termination and wrote "termination
verified" into this report. It was not; the probe had printed the bug and I had misread it. The
test agent that owned `test_polling_working_slot.py` found it independently and wrote a
strict-xfail with a full reproducer. **A probe whose output you interpret is not a test.** The
same file's other xfail, by contrast, was an artifact of the corrupted tree below — the agent
measured `MIN(c.priority) DESC` because the `priority-inverted` mutant was applied at the time.

**A drain thread retiring in the same window as a push stranded the message.** Between
`drain_once` reporting "nothing left" and the thread actually exiting, a push would find the
thread still alive, report `[nothing new]`, and spawn nothing — and the thread would then exit
and delete its own row, leaving work queued with no thread and no `drain_threads` row. That is
the liveness failure the row exists to prevent, arriving by the other door. Retirement now
re-checks for work while holding the same lock `notify_partner_push` decides under, which makes
the two mutually exclusive.

The provenance is worth recording. The polling agent suspected something here, could not
reproduce it reliably, noticed the symptom had the same signature as the mutation interference
below, and **retracted** the claim rather than assert it — while explicitly saying it had not
re-confirmed against a clean tree. That was the right call on its evidence, and the bug was
real. It was confirmed here by forcing the window open with an injected pause rather than
hoping to hit it. A retraction is not a refutation.

**Two mutations were left applied to the working tree**, silently, and both left the suite
green — which is what a surviving mutant means. `cap-ignores-working` was applied for long
enough that the cap stopped counting the working slot; `priority-inverted` was applied for long
enough to invert the whole queue. Root cause: two mutation passes running at once, each
restoring to its own snapshot and overwriting the other's restore. That was an orchestration
error on my part — I told an agent to run the pass while I was running it.

It happened a third time before the guards were complete: a real bug fix — the retirement race
above — was written while a pass was running in the background, and the pass's restore silently
reverted it. The suite stayed green for another twenty minutes, because a reverted fix and a
surviving mutant look identical from outside.

`tests/mutation_run.py` now refuses to start when `.mutation_running` exists, snapshots to
`.mutation_backup/` before touching anything, restores on SIGINT/SIGTERM (which a `finally`
does not cover), self-heals at the start of the next run if it was killed anyway, and —
the guard that closes the third case — restores a file **only if it still contains exactly what
the pass wrote into it**. If it does not, something else changed it, and the pass says so and
keeps its hands off rather than reverting work it did not make.

## Verification performed

- 63 schema assertions pass; the shipped schema is byte-identical to the vault copy.
- A dead-column audit over all 12 tables: no table and no column is without a reader, writer,
  trigger, or index.
- The ER diagram is checked programmatically against the live schema, table by table and
  column by column.
- The rejection-code index in `docs/02` is checked programmatically against `core.py`: no code
  documented that does not exist, none raised that is not documented.
- A priority probe covering promotion, displacement, the cap counting the working slot,
  interruption, hold release, and the upward-`[RESEARCH]` refusal — the last set up with the
  handshake in place, so the refusal can only come from the hierarchy check.
- A round-trip probe covering the two-phase `[RESEARCH]` close-out, drain-row deletion on
  retirement, and drain-row survival across `stop()`. Termination is covered by a *test*
  (`test_termination_delivered_truthful_report_produces_nothing_back`) rather than by this
  probe, for the reason recorded above: the probe printed the bounce and I read it as its
  absence.
- `get_permissions` verified against the real Antigravity install: returns `[]`, matching the
  `allowlist (0)` the TUI reports.

## A third audit, read late

`vault_check` flagged a new orphan, which is how the third audit was found at all — it had
been added while this work was in flight. Three items:

**All three orchestrator roles are Claude Science roles**, not just `gemini-orchestrator`.
This overruled a question I had escalated rather than decided: I had found that an Antigravity
or NotebookLM partner could claim `project-orchestrator` or `bridge-scientist`, checked every
path it could be exploited through, found each independently guarded, and left it. That is the
"currently unreachable" argument, and it is not a rule. `claim_orchestrator` now checks the
source for every role, with one rejection code (`orchestrator_requires_science_project`)
replacing the role-specific one.

It had a consequence I had not anticipated. A `gemini_` partner can now hold **no** role, so it
can never clear `handshake`'s orchestrator gate — which makes `no_handshake_between_gemini` and
`gemini_to_science_illegal` **unreachable through the tool surface**. Both checks are kept as
defence in depth, since they still fire against a role written straight into the database, and
both are now marked unreachable in the reference. Five tests were rewritten: four to assert the
rule an agent actually meets, one to force a role in and prove the deeper check is not dead
code.

**The Escape keystroke** — verified live, above.

**"Investigate Note 04"** — ambiguous between the runbook and the priority-queue diagram, so
both were checked mechanically. All 13 SQL blocks in the runbook execute against the real
schema; no tool or environment variable it names is absent from the code; the diagram's
priority table and both cap values match `label_caps`, and its "two questions" claim matches
the two head statements. The finding was what was *missing*: today's delivery bug is a failure
mode with no runbook entry, and the placeholder body it produces is legitimate for two of the
three remotes — so the **timing** is the tell, not the body. That entry now exists.

## Not done

The **write** half of the Antigravity permission path has not been driven live. It needs an
`agy` session under tmux, and there is none — the two running `agy` processes are on the user's
own terminals, not under tmux. More importantly, the plan assumed a "scratch conversation"
would isolate the test; the investigation established permissions are **project-scoped**, so
there is no such thing as a scratch grant here. A test write would land in the same
`projectResources` the user's live sessions read. That is a decision to put to the user rather
than take.
