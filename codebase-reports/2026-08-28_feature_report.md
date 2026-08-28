# Feature report — 2026-08-28

## `[IDLE]` removed; a blocking question is the hold

`[IDLE]` and `interrupt_partner` are gone. Nothing stops another agent any more.
An agent is stopped in exactly two ways, both of them ordinary queue mechanics:
by a message that strictly outranks what it is doing, or by the `[QUERY]` or
`[ERROR]` it sends itself.

### What changed

**`send` stops its own caller for a blocking label.** `labels.BLOCKING_BEHAVIORS`
is `("[QUERY]", "[ERROR]")`. Sending one calls the new
`MessagingCore._await_answer`: it stops the sender's remote, pushes whatever the
sender was working on back into the sender's own queue marked `in_process`, and
puts the question itself into the sender's working slot. One condition, not
three — direction does not matter and neither does whether the sender held work.

**The question is the hold, at its own natural priority.** `[QUERY]` and
`[ERROR]` stay at 2 and are never raised. That alone makes an unanswered
question a blocker, because only `[TRUTHFUL-REPORT]` at 1 is strictly lower. A
dedicated hold at priority 0 would have needed a second, contradicting rule to
ever leave the slot.

**Nothing is rendered or delivered for a wait.** `templates.idle_interruption`
is deleted. The drain thread does not poll a waiting agent, does not report for
it, and `PollingServer.scan_once` no longer arms a thread for one — its remote is
stopped, so there is nothing to harvest, and what ends a wait is a queued row
that arms the partner through the queue branch anyway.

**A second question is refused** with `already_awaiting_an_answer`
(`MessagingCore._already_waiting`).

**The answer is folded into what comes next.** When a `[MESSAGE-RESPONSE]`
reaches a waiting agent, `advance` consumes its row without promoting it,
discards the question (never requeued), re-reads the head, and renders one
prompt via the new `templates.resolution` — three shapes: a new job quoted in
full, the agent's own paused work named by label, or the response alone when the
queue is empty. `templates.awaiting_resolution` renders the synthetic slot task.

**`[ERROR]` now replies with `[MESSAGE-RESPONSE]`** (audit item Q4), so a caller
that corrects a blocked partner learns the correction landed.

### The new column

`message_queue.awaiting_resolution` (`INTEGER NOT NULL DEFAULT 0`, `CHECK IN
(0,1)`, in `db._ADDITIVE_COLUMNS`). A `[TRUTHFUL-REPORT]` outranks a waiting
agent, so a question really can be displaced into the queue. The flag is read
**first** in both `_HEAD_LABEL_SQL` and `_HEAD_ROW_SQL`, so the displaced
question outranks everything else in that agent's queue and re-enters the wait
rather than coming back as work the agent already asked.

It also scopes the "at most one paused row per label" property to *work* rows: a
wait carries a label too and can share one with a paused task, but is never
rendered and so is never what a resume prompt names.

### Two defects found while doing it

**The answer could deadlock behind lower-numbered work.** `[MESSAGE-RESPONSE]`
sits at priority 3, below `[QUERY]`/`[ERROR]` at 2 and `[TRUTHFUL-REPORT]` at 1.
An agent whose paused work carried one of those labels had a queue head that was
not the answer, however long the answer had been sitting there — `advance`
returned `None` on every pass, forever. Found by a test written for the
tie-break, not for this. `advance` now looks the answer up **by label** while the
slot is awaiting, because priority orders work and the message that ends a wait
is not work. Mutant: `the-answer-is-ordered-by-priority`.

**A summary phase cannot be displaced at all any more.** It runs at
`[TRUTHFUL-REPORT]`'s own priority of 1 and `advance` displaces only on a
strictly lower number, so `_swap`'s `summary_phase` carry is now defensive. The
reachable route back into the queue is a failed delivery through `_requeue`, and
the mutant and its test were retargeted there.

### Removed

- `MessagingCore.interrupt_partner` and its MCP tool registration
- `MessagingCore._require_executable` (orphaned with it; `not_executable`
  survives as an extension-raised code in `_UNCANCELLABLE`)
- `templates.idle_interruption`
- `labels.INTERRUPT_BEHAVIOR`, `core._RAISES_UPWARD`, the `holding` variable
- rejection codes `idle_not_sendable` and the capability's `different_project` /
  `not_executable` / `no_such_partner` entries

Seventeen capabilities remain, six of which need an extension.

### Documentation

`docs/01`–`docs/05`, `README.md`, five `.mmd` diagrams, three of the six podcast
notes, and the Obsidian vault (`Index`, `Message types`, `Prompt templates`,
`Queuing system for messages`, `Schema notes`, `Extension layer`, `Antigravity
state handling`, `Response body standard`, `Messaging Server`, `Vault
conventions`, two tool notes, `vault_check.py`, `schema_test.py`, and the shipped
schema copy). `docs/05` gained invariant 8a for the displaced wait.

`tests/doc_consistency.py`: 146 checks, no inconsistency. `vault_check.py`: the
four remaining failures are pre-existing and in files this change did not touch
(a human audit's `[[interrupt]]` link, two orphan notes, one H1 heading).
