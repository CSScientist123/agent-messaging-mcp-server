# Invariants, and what the system cannot currently do

**Audience.** An engineer about to change this system, who needs to know which rules are
load-bearing before touching them.

**Scope.** The invariants the design rests on: what each one is, why it exists, what breaks
without it, and — importantly — **where it is enforced**. Then a closing section on
capabilities the system offers that no remote fully implements.

**Non-scope.** How to operate or debug a running system, which is
`docs/04-operating-and-debugging.md`. Parameter detail, which is `docs/02-reference.md`.

**Assumed prior knowledge.** Python, SQLite, and `docs/03-message-lifecycle.md`.

A note on enforcement, because it is the most useful column here. A rule enforced by a
`CHECK`, a `UNIQUE` constraint, or a trigger cannot be bypassed by a new code path. A rule
enforced only in application code can be, and will be, by the next person who writes a second
route to the same table. Where that is the situation, this document says so plainly rather
than implying more safety than exists.

## 1. All writes go through one writer thread

**The rule.** Every `INSERT`, `UPDATE`, and `DELETE` runs on a single dedicated writer
thread, inside an explicit `BEGIN IMMEDIATE`, committing on success and rolling back on any
exception. Reader connections are opened read-only.

**Why.** Concurrent writers on one SQLite file contend for the write lock and produce
`database is locked` under load. Funnelling writes through one thread removes the contention
entirely instead of tuning around it.

**What breaks without it.** Intermittent lock errors under concurrency, and writes that
escape the transaction discipline — so a failure part-way leaves half a change behind.

**Where enforced.** `messaging_core/db.py`. Reader connections open with
`sqlite3.connect(f"file:{path}?mode=ro", uri=True)`, which makes read-only a property of the
connection rather than a setting inside it. That distinction matters: a `PRAGMA query_only`
can be switched off through the very API it guards — one `read("PRAGMA query_only = OFF")`
followed by a `read("INSERT ...")` and the invariant is gone for that thread's connection.
A `mode=ro` connection refuses the write regardless of any pragma.

Covered by `tests/test_foundation.py::test_read_cannot_write` and the `reader-is-writable`
mutant.

## 2. Priority decides the working slot, and only a strict win displaces

**The rule.** There is one queue per Partner. `advance` promotes the queue head into the
working slot only when the slot is empty or the head's `label_caps.priority` **strictly**
beats the working task's.

**Why strictly.** An arriving `[QUERY]` must not displace a `[QUERY]` already being answered.
If equal priority displaced, two Callers could ping-pong a Partner between their questions
and neither would ever get an answer.

**What breaks without it.** Livelock under two equal-priority Callers, and a Partner that
never completes anything.

**Where enforced.** Application code — `MessagingCore.advance`, which is the only
implementation of the swap. `send`, `interrupt_partner`, and the Polling Server's drain
thread all call it rather than repeating it. Covered by the `displace-on-equal` and
`priority-inverted` mutants.

## 3. `in_process` outranks only within its own label

**The rule.** A displaced task is marked `in_process = 1` and pushed back. It is picked
before other queued tasks carrying **the same label**, and never before a task carrying a
different one.

**Why it takes two statements.** A single `ORDER BY priority, in_process DESC, enqueued_at`
says "paused first at equal priority", which is not the rule — it is the rule with the label
scoping dropped. `[QUERY]` and `[ERROR]` share priority 2 deliberately, so the difference is
reachable: while the tie-break was global, a Partner interrupted mid-`[QUERY]` was handed
"resume your previous `[QUERY]`" instead of the `[ERROR]` its Caller had just sent explaining
what went wrong, and the correction was never delivered. That is the exact flow the approval
doctrine in rule 20 depends on.

So the head is read in two steps. `_HEAD_LABEL_SQL` picks the label: lowest priority, then a
label with any unpaused work over one whose rows are all paused, then arrival.
`_HEAD_ROW_SQL` picks the row within it: paused first, then arrival.

**The consequence that makes the resume prompt possible.** Among the tasks carrying one
label, at most one is paused and it is always picked first — so "resume your previous
`[RESEARCH]`" refers to exactly one thing, and the prompt can be a single line.

**Where enforced.** `_HEAD_LABEL_SQL` and `_HEAD_ROW_SQL` in `messaging_core/core.py`.
Covered by three mutants: `in-process-ignored` (drops the paused-first tie-break within a
label), `in-process-crosses-labels` (restores the global tie-break that caused the bug above),
and `displaced-not-paused` (requeues a displaced task without the flag at all).

## 4. Draining deletes

**The rule.** A `message_queue` row is removed when its task is promoted into the working
slot, so the queue holds what is waiting rather than what is running.

**What breaks without it.** A busy Partner would hold its own cap slot twice — once in the
queue and once in the working slot — so a cap of two would admit one.

**Where enforced.** Application code, in `MessagingCore.advance`. There is exactly one
implementation now; an earlier version had two, in the core and the polling server, and
`docs/05` used to warn about keeping them in step. That warning is obsolete because the
duplication is gone.

## 5. A cap counts work in flight, keyed `(partner, caller, label)`

**The rule.** `label_caps.max_outstanding` limits one Caller's outstanding tasks of one label
against one Partner, counting the working slot as well as queued rows.

**Why the working slot counts.** Otherwise the next task is admitted the moment the previous
one starts, and a cap of three means four.

**Why one statement.** Admission is `INSERT ... SELECT ... WHERE`, so being over cap is
`rowcount == 0`. A read-then-write can be passed by two concurrent callers simultaneously,
which is exactly how a cap of three admits four.

**Where enforced.** `_ADMIT_SQL` in `messaging_core/core.py`, with the working-slot term
supplied by `WorkingSlots.outstanding` under the Partner's slot lock. Covered by the
`cap-ignores-working` mutant.

## 6. Only labels marked `stored` reach `messages`

**The rule.** `messages` accepts `[QUERY]`, `[TRUTHFUL-REPORT]` and `[MESSAGE-RESPONSE]` —
exactly the labels with `label_caps.stored = 1`. `[RESEARCH]`, `[ERROR]` and `[IDLE]` are
transport.

**Why a trigger reading `label_caps` rather than a `CHECK` listing labels.** A list in the
`CHECK` would be a second copy of `label_caps.stored`, and two copies of one fact eventually
disagree. With the trigger, flipping `stored` on a row changes what `messages` accepts.

**What breaks without it.** `read` would replay delegated work and resolved failures into an
agent's context on every call, growing without bound.

**Where enforced.** The `messages_stored_labels_only` trigger in `schema/schema.sql`, plus
`messages.behavior REFERENCES label_caps(behavior)`. Covered by the `store-everything`
mutant, and by an assertion in `tests/test_schema_constraints.py` that flips `stored` and
confirms the trigger follows.

## 7. An exchange terminates because some labels reply with nothing

**The rule.** A finished task pushes `label_caps.reply_behavior` back to its Caller.
`[QUERY]` replies with `[MESSAGE-RESPONSE]`, `[RESEARCH]` with `[TRUTHFUL-REPORT]`, and the
other four with NULL — nothing.

**What breaks without it.** Every completed task would produce a message that produced a task
that produced a message. Two agents would talk to each other until one was archived.

**Where enforced.** The column, read by `MessagingCore.reply_behavior` and acted on in
`PollingServer._complete`. A `CHECK` additionally forbids a label replying with itself,
which is the same infinite exchange written more compactly.

## 8. `[IDLE]` enters the slot by priority and leaves it by anything

**The rule.** `[IDLE]` holds the highest priority, so pushing one takes the working slot by
construction — that is all a forced interruption is. But an `[IDLE]` *in* the slot is a hold,
not a task: any arrival displaces it regardless of priority, and it is discarded rather than
requeued.

**Why.** Comparing priorities on the way out would make the interruption permanent, since
nothing outranks `[IDLE]`. And requeuing it would stop the Partner again the moment it
resumed.

**Where enforced.** Application code, in `MessagingCore.advance` (`holding`). `send` refuses
`[IDLE]` with `idle_not_sendable`, so `interrupt_partner` is the only producer. Covered by the
`idle-requeued` mutant.

## 9. Delegated work never travels upward

**The rule.** `[RESEARCH]` may only reach a Partner at the same or a lower position in
`agent_layers`. Every other label travels freely in both directions.

**Why only `[RESEARCH]`.** It asks its recipient to go and do something. A lower agent handing
it upward would be reassigning its own director's work. An answer, an error or a report has to
be able to come back, which is why the rule is scoped to one label rather than applied to the
pair.

**Where enforced.** Two places, and the second is what makes the first a rule rather than a
convention. `MessagingCore.send` reads `agent_layers` — not literals, so a deployment that adds
a tier adds a row. And `MessagingCore.report_back` refuses any `behavior` that is not some
label's `reply_behavior`, which is what stops it being a second route into a Partner's queue
that skips the check entirely. `report_back` has no tool and no reachable caller that would
pass `[RESEARCH]` today; "not currently reachable" is not an invariant, and the gate costs one
query.

## 10. Titles address, UUIDs identify

**The rule.** A Caller names a target by title at the boundary; the title is resolved
immediately and nothing stored or forwarded carries one. A Caller identifies itself by UUID
only, never additionally by title.

**Why.** A stale title then fails loudly at the boundary against the current registry,
instead of propagating into stored state where a later reader resolves it to whatever holds
that name now.

**Where enforced.** Structurally — every foreign key in the schema references an id. No tool
takes a `requester_title`, and `tests/test_mcp_surface.py` asserts no tool schema contains
that parameter.

## 11. A Partner takes its Project's source

**The rule.** `partners` has no source column. A Partner's source is `projects.source_prefix`,
and a Project has exactly one.

**The consequence.** Two Partners in one Project always share a source, so a **cross-source
handshake is cross-Project by construction**. The same-Project requirement in the handshake
rules therefore applies only within a source.

**Where enforced.** Structurally: `projects.source_prefix` is a foreign key to `source_caps`,
and `partners.project_id` a foreign key to `projects`.

## 12. Titles are unique server-wide and permanent

**The rule.** A Partner title is unique across the whole table, archived rows included.
There is no rename capability at all — a title is fixed for the life of a Partner.

**Why.** Scoping uniqueness to live Partners would free a name for reuse, after which a stale
address held in a runbook, a transcript, or another agent's memory would silently resolve to a
**different** agent. A loud "title taken" is better than a quiet misdelivery.

**The cost, stated honestly.** A semantic title is spent after one use. Under a naming
convention like `literature-reviewer`, archiving burns that name permanently. Plan for a
generation suffix.

**Where enforced.** `UNIQUE (title)` on `partners`, plus the `partners_no_rename_archived`
trigger, which refuses a direct `UPDATE` of an archived Partner's title — so the rule survives
a code path that bypasses the tools.

## 13. `partner_id_in_remote` is unique per Project

**The rule.** One remote object maps to one Partner within a Project.

**Why per Project rather than globally.** A remote id is only meaningful inside the remote app
that issued it, and the Project identifies that app. Two Projects could coincidentally use the
same id string for genuinely different objects.

**Where enforced.** `UNIQUE (project_id, partner_id_in_remote)`. The application pre-check
remains only as a fast path — the constraint is what makes it race-free, since a check-then-
insert can be passed by two concurrent callers.

## 14. A role is claimed once

**The rule.** An orchestrator role is claimed once and never reassigned, with one holder per
Project and role among non-archived Partners.

**Where enforced.** The `one_orchestrator_per_project_role` partial unique index, so a
concurrent claim race has exactly one winner at the database level rather than in a
check-then-act. Covered by the `role-reclaimable` mutant.

**A consequence worth naming.** Because one Project holds at most one Partner per role, the
same-role rule on cross-project handshakes (invariant 16) can never fire *within* a Project —
its two project ids would have to be equal, and they are not.

## 15. Ten live Partners per Project

**The rule.** A Project holds at most `source_caps.max_live_partners` non-archived Partners.

**Where enforced.** The `partners_live_limit` trigger. A `CHECK` cannot count rows, so a
trigger is the only way to keep this in the database rather than in whichever caller
remembers. The limit is data, so a different ceiling is a row rather than a code change.

## 16. A Project extension branches sideways, never downward

**The rule.** Two Projects declared extensions of one another (`project_extension`) allow
their Partners to handshake across the boundary — but only between two Partners holding the
**same** orchestrator role.

**Why.** The live-Partner ceiling is deliberate, so research at scale needs more Projects
rather than a larger ceiling. What it needs is *width*, not a second chain of command.
Requiring identical roles is what stops a `gemini-orchestrator` in one Project from taking
direction from a `project-orchestrator` in another — inheriting a superior it was never given.

**Where enforced.** Application code in `MessagingCore.handshake`, against a
`project_extension` row. The table itself is symmetric by construction: `CHECK (project_a <
project_b)` means the pair has exactly one row and cannot answer differently depending on
which way it is asked.

## 17. Claude Code has exactly one counterpart

**The rule.** A `code_` Partner may handshake only a `science_` Partner holding
`bridge-scientist`, and that bridge may hold at most one `code_` Partner.

**Why.** The bridge is the seam between a human's Claude Code session and the research
project. A bridge holding two `code_` Partners would make "the Caller" ambiguous for every
message reaching it.

**The exemption this forces.** A `code_` requester cannot hold an orchestrator role — all
three roles are Claude Science roles — so it is exempt from the "only an orchestrator may
initiate a handshake" rule, and `handshake` checks the `code_` case first, before the check it
could not pass.

**Where enforced.** Application code in `MessagingCore.handshake`
(`code_handshakes_bridge_only`, `bridge_single_code_partner`).

## 18. Authorization precedes existence disclosure

**The rule.** A capability taking another Partner's identifier checks the requester's
authorization **before** any check that depends on the target existing.

**Why.** Otherwise the distinct rejection codes form an enumeration oracle: any live Partner
can probe whether a guessed UUID exists, and what role it holds, without the UUID ever
appearing in a response. A UUID that "must never be leaked" is then confirmable.

**Where enforced.** Application code in `grant_gemini_budget`, the only capability taking
another Partner's UUID. Every other capability addresses its target by title, which
`search_partner` already discloses.

## 19. A permission is recorded only after the remote is seen to hold it

**The rule.** `add_permissions` writes to the remote, reads the remote back, and records the
grant in `partner_paths` **only** if the read confirms it. A mismatch raises
`permission_not_applied` and records nothing.

**Why that order.** The opposite order leaves `partner_paths` claiming a grant that does not
exist, which is the one direction of drift a Caller cannot recover from by reading. And a
Caller that believes a permission landed when it did not will send work that stops on a
prompt — the exact failure the approval doctrine exists to prevent.

**Why the two are allowed to differ at all.** `partner_paths` is the intended set;
`get_permissions` reports the actual one. The difference is precisely what a Caller acts on,
so collapsing them into one number would remove the only signal worth having.

**Where enforced.** `MessagingCore._apply_and_verify`. The `partner_paths_gemini_only`
trigger additionally keeps rows off any non-Antigravity Partner, since a grant nothing will
ever apply is indistinguishable, to a reader, from one that is being enforced.

## 20. No tool anywhere answers a permission prompt

**The rule.** An approval request is an error, not a question. No capability answers one, in
any adapter.

**Why.** Answering leaves the permission set that caused the prompt unchanged, so the next
turn blocks identically.

**The route back.** `interrupt_partner`, an `[ERROR]` reply naming what was missing,
`get_permissions` to see what the conversation actually allows, `add_permissions` /
`delete_permissions` to correct it, and then a fresh `send`. There is no resume capability
and no resume extension method: correcting the grant and sending again *is* the resumption.

**Where enforced.** By absence — there is no such capability to call. Antigravity's
`poll_completion` raises `approval_is_an_error` if it sees a prompt rather than responding to
it. Enforcement by absence is the strongest form available: a rule with no method cannot be
circumvented by an agent that finds circumventing easier.

## 21. A drain thread that retires removes its own row

**The rule.** `drain_threads` holds one row per Partner with a live drain thread. A thread
that finds nothing queued and nothing working retires and DELETES its row.

**What breaks without it.** The row is a claim that a thread is running when none is. It is
also what `start()` reads to bring threads back after a restart, so a stale row means a
thread is respawned for a Partner with no work — harmless, since it retires immediately, but
the row would never be cleaned up by anything else.

**The deliberate exception.** `stop()` does NOT delete rows. It signals threads for a process
that is going away with work possibly still queued, and the row is exactly what lets `start()`
resume that Partner. Deleting at shutdown would strand the work the row exists to protect.

**Where enforced.** `PollingServer._deregister`, called from the drain loop's natural-exit
path only. Covered by the `drain-row-survives` mutant.

## 22. A delivery is not finished until the remote has visibly started

**The rule.** `deliver_message` must not return while the remote still looks
idle. For a TUI-driven remote that means waiting, bounded, for the pane to show
its busy footer before handing control back.

**Why.** The drain loop's contract is `deliver, then poll until finished`. It is
only sound if the first poll cannot observe the state that existed *before* the
delivery. A TUI repaints asynchronously, so it can and does.

**What breaks without it, observed live rather than reasoned about.** An
Antigravity `[QUERY]` round trip reported **complete in 0 seconds** with a
placeholder body. The message had been delivered and the agent went on to answer
it correctly — into a pane nobody was watching any more, because `poll_completion`
had read the stale idle footer, `_complete` had fired, and the task was closed
before the agent began. **Nothing raised.** The caller got a well-formed receipt
and a `[MESSAGE-RESPONSE]` containing the "reported through its own channel"
placeholder, which is exactly what a legitimate Claude Science reply looks like.

**Why a timeout is not an error.** A turn short enough to finish inside the
window never shows a busy footer at all. Returning is correct there — the answer
is already on the pane — so the wait reports nothing and simply ends.

**Where enforced.** `AntigravityExtension._await_busy`, called at the end of
`deliver_message`. Covered by
`test_antigravity_deliver_message_waits_for_the_turn_to_actually_start` and its
too-fast-to-observe companion.

**The generalisation, since this is the third time it has bitten.** Any remote
whose state is read by *observing* rather than by *asking* can report the state
from before your action. The permissions walk hit it (a pane captured mid-repaint
showed half a typed rule), the manual verification hit it, and delivery hit it.
Wherever this design reads a pane, it must wait for the pane it expects rather
than trust the first one it gets.

## Capabilities no remote fully implements

This section describes a real limit on what the system can promise today.

**Antigravity path permissions are real, and verified against a live session.** Not inferred:
the sequence below was driven end to end, a grant was made and read back, and it was revoked
again.

`get_permissions` reads `~/.gemini/config/projects/<project-id>.json` at
**`permissionGrants.permissionGrants.allow`**, with the project id from
`~/.gemini/antigravity-cli/cache/default_project_id.txt`. `add_permissions` and
`delete_permissions` write through the `/permissions` view over tmux, and the caller verifies
against that same file.

**The verification method is the part worth carrying forward.** An earlier version of this
adapter read `projectResources`, and was "confirmed" by observing that it was `{}` while the
TUI read `allowlist (0)`. Empty matched empty. That is not evidence of a mapping, and the
moment a real rule existed the TUI read `allowlist (1)` while the adapter still returned `[]`.
**A mapping is confirmed only when a non-empty value appears on both sides.** The same mistake
is available anywhere a check compares two things that are both, at that moment, nothing.

Two honest caveats remain, and one that has been removed.

**Antigravity permissions are project-scoped, not conversation-scoped** — every conversation
under one project sees one list, so `partner_id_in_remote` is accepted for interface symmetry
and is not used by `get_permissions`. This also means there is no such thing as a "scratch"
grant for testing: a test write lands in the list every conversation under that project reads.

**A session left in the permissions editor swallows the next message.** Closing takes more than
one Escape — the editor is two screens deep — and if the chat prompt cannot be reached the
adapter raises `antigravity_session_stuck_in_editor` rather than returning. Reported because
the alternative is silent: everything sent afterwards is read as editor input, and no error
surfaces anywhere.

**Removed: the claim that revocation is impossible.** The editor's footer advertises
`d/⌫ Delete rule`, and `d` deletes the selected rule immediately. `delete_permissions` now
works, by re-reading the list before every removal, moving the cursor onto the rule by name,
and confirming it is there before pressing a key that deletes without asking.

**Claude Science has no per-frame path concept at all**, so all three permission methods stay
at the base class's `not_path_configurable` refusal there rather than being implemented
against an invented endpoint.

**`stop_remote_execution` for Claude Science needs an execution id its API never surfaces.**
The interrupt route exists but requires an `execId` that no other route returns. Claude
Science's own MCP server never calls it and instructs its caller to cancel by hand in the UI,
which an agent cannot do. The adapter raises `no_remote_cancel`.

**Antigravity's interrupt sends `Escape`, and this is now verified rather than inferred.** A
busy turn went idle 2 seconds after it, with the pane reading `Interrupted - What should
Antigravity CLI do instead?`. The absence of a better mechanism is also confirmed: `index.js`
sends `Escape` only from `escapePicker` and `agy_dismiss`, neither of which cancels a turn.

**Nothing on this list is now an unverified keystroke.** Both the `/permissions` sequence and
the interrupt were driven against a live session and are pinned by tests. What remains here is
a genuine capability gap -- Claude Science cannot be cancelled at all -- rather than an
assumption waiting to be checked.
