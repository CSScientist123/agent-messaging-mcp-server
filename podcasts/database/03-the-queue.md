# Three: The queue — `message_queue`, `messages`, and the slot that is not a table

**Have open:** `visualizations/03-schema-er.png`. `MESSAGE_QUEUE` is the largest
box on the page, lower right. **Its eleven column glosses are this note's section
list** — read them top to bottom and you have the outline. Then find its four
inbound edges: two from `PARTNERS` ("waits for (partner_id)" and "pushed by
(caller_id)"), two from `LABEL_CAPS` ("prioritises and caps (behavior)" and
"admitted under (origin_behavior)"), and one from `MESSAGES` ("persisted copy
(message_id)").

Secondary: `04-priority-queue.png` for the head read and the swap, and
`05-working-slot.png` for the thing the ER diagram *cannot* draw.

**The claim this note argues:** this table is where the system actually lives, and
three of its columns exist only because something that is not a row — the working
slot — has to be reconciled with rows.

---

## 1. One queue, and what a row is

```sql
CREATE TABLE message_queue (
    id           INTEGER PRIMARY KEY,
    partner_id   INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    caller_id    INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    behavior     TEXT NOT NULL REFERENCES label_caps(behavior),
    body         TEXT NOT NULL,
    in_process   INTEGER NOT NULL DEFAULT 0 CHECK (in_process IN (0, 1)),
    message_id   INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    summary_phase   INTEGER NOT NULL DEFAULT 0 CHECK (summary_phase IN (0, 1)),
    origin_behavior TEXT REFERENCES label_caps(behavior),
    awaiting_resolution INTEGER NOT NULL DEFAULT 0 CHECK (awaiting_resolution IN (0, 1)),
    enqueued_at  TEXT NOT NULL
                 DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (caller_id <> partner_id)
);
```

**One queue per partner, ordered by priority. Every message is a push.** There is
no separate reply channel, which means there is no routing decision to get wrong.
An earlier design had two queues chosen by causal role — a capped forward queue
for messages that opened a chain, an uncapped backward one for messages
continuing an admitted chain. It solved the deadlock it was built for, but it
solved it by asking the caller to classify its own message, and a caller that
classifies wrongly gets a deadlock back. The priority queue solves the same
problem structurally: an answer outranks the work waiting for it, so it cannot be
stuck behind it.

**A row here is a task that is *waiting*.** Not one that is running. That
distinction runs through everything below.

### `partner_id` and `caller_id` — and the `CHECK` that shapes real decisions

*Trace: the `caller_id` gloss reads "CHECK caller_id <> partner_id" — the diagram
puts the constraint on the column.*

```sql
CHECK (caller_id <> partner_id)
```

A queue row may not name its own recipient as its sender.

This sounds like hygiene and is not. It forces a genuine design decision
elsewhere. When a partner is archived, every caller with work queued for it must
be told, and that notice is an `[ERROR]` row written into each caller's queue. Who
is the sender?

There are only two candidates: the partner that vanished, or the agent that
archived it. Attributing it to the requester fails — within a project, only the
project-orchestrator may direct a plain worker, so the requester is *normally the
same partner* as the one caller with work queued. The row would name its own
recipient as its sender, and this `CHECK` refuses it.

So the notice is attributed to the **vanishing partner**, and that works precisely
because archiving is a soft delete: the row survives, so it remains a valid,
permanent sender. `_report_lost_work` also has to repeat the check by hand
(`core.py:807-808`), skipping any caller that *is* the partner — because the
working slot is not a table and the constraint cannot reach it.

Both columns are `ON DELETE CASCADE`, and that pair of cascades is the fact
`delete_partner` is built around. Note 4 takes it.

### `behavior` and `body`

`behavior` is a foreign key into `label_caps` — an unknown label cannot be queued,
not because a validator rejects it but because the database will not store it.

`body` is the caller's raw text. It lives here rather than in `messages` because
only the three labels marked `stored` are ever written there; the queue must be
able to carry a `[RESEARCH]` or an `[ERROR]` that has no `messages` row at all.

---

## 2. `in_process` — paused, and the bug that shaped the whole pop order

*Trace: the gloss reads "1 = paused; tie-break WITHIN a label". That last word is
the one that cost a real bug.*

```sql
in_process INTEGER NOT NULL DEFAULT 0 CHECK (in_process IN (0, 1))
```

A task displaced from the working slot by a higher-priority arrival. It is
**paused, not new** — and it outranks other queued tasks carrying **the same
label only**.

### Why the scoping is load-bearing

`[QUERY]` and `[ERROR]` deliberately share priority 2 (note 1). Now suppose
`in_process` were a *global* tie-break — paused beats fresh, regardless of label.

A partner is interrupted mid-`[QUERY]`. Its caller, realising what went wrong,
sends an `[ERROR]` explaining it. Both labels sit at priority 2. The paused
`[QUERY]` wins the global tie-break, and the partner is handed **"resume your
previous `[QUERY]`"** instead of the correction it was just sent.

**The correction is never delivered.** Not dropped, not errored — just permanently
outranked by the thing it was sent to fix. Every individual step behaves exactly
as specified.

That is why reading the head takes **two statements**. One `ORDER BY` cannot say
"within a label".

### The surprise: `in_process` is never `UPDATE`d

There is no `UPDATE message_queue SET in_process = 1` anywhere in the codebase.
Pausing a task is a **DELETE followed by a fresh INSERT** with `in_process = 1`
(`core.py:2357-2394`).

Two consequences that will bite anyone reasoning about this table from the schema
alone:

**A resumed row has a new rowid.** So `_HEAD_ROW_SQL`'s final `q.id ASC` tie-break
means "most recently paused sorts last", not "oldest task first".

**`enqueued_at` on a paused row is the *re*-queue time**, not the original
arrival. A task displaced three times has an `enqueued_at` from the third
displacement. Any latency measurement built naively on that column would measure
the wrong interval.

---

## 3. `message_id` — and the schema's only `SET NULL`

*Trace: the edge `MESSAGES |o--o{ MESSAGE_QUEUE : "persisted copy (message_id)"`.
The `|o` on the `MESSAGES` side is the nullability: a queue row may have no
message at all.*

```sql
message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL
```

Set only for stored labels — NULL for `[RESEARCH]` and `[ERROR]`, which have no
`messages` row.

**This is the only `ON DELETE SET NULL` in the entire schema.** Twelve foreign
keys cascade, seven take no action, and this one nulls out.

And here is the honest observation: **it is unreachable in practice.**
`message_id` is carried through the swap and lands on the working slot, but
nothing ever dereferences it — `read` goes straight to `messages`. More to the
point, `messages` rows are only ever removed by a partner cascade, and that same
cascade removes the queue row too. There is no path that deletes a message while
leaving a queue row pointing at it.

So the clause is defensive rather than live machinery. Worth saying plainly, since
a reader could reasonably assume it fires.

---

## 4. `summary_phase` and `origin_behavior` — a label that changes mid-flight

*Trace: two glosses — "1 = a displaced [RESEARCH] summary; still owes its Caller"
and "label admitted under, when it differs from behavior". And the newly-drawn
edge "admitted under (origin_behavior)".*

A `[RESEARCH]` task is two exchanges against one remote: do the work, then report
on it. When the work finishes, `begin_summary_phase` **relabels the task in
place** — same working slot, `behavior` becomes `[TRUTHFUL-REPORT]`, effective
priority rises from 4 to 1, and the body stays the original request.

That relabelling breaks two things unless something carries the truth forward.

### `summary_phase` — who is still owed a report

```sql
summary_phase INTEGER NOT NULL DEFAULT 0 CHECK (summary_phase IN (0, 1))
```

> The label alone cannot say it — a `[TRUTHFUL-REPORT]` can equally be one an
> agent sent directly, and that one owes nothing back, because it already IS the
> report.

Without the marker, a requeued summary phase is indistinguishable from a delivered
report. On resume the system looks up what a `[TRUTHFUL-REPORT]` replies with,
finds NULL, releases the slot, and pushes nothing. **The research is done and its
answer is silently discarded** — no error, no log, and the caller simply never
hears back.

The Polling Server reads this marker rather than inferring the reply from the
label (`polling/server.py:678-689`), and the comment there names the bug that
made it necessary: inferring it meant a directly-sent `[TRUTHFUL-REPORT]` replied
with another one, each hop spawning a fresh drain thread.

### `origin_behavior` — which cap this still counts against

```sql
origin_behavior TEXT REFERENCES label_caps(behavior)
```

NULL in the ordinary case; set only when the admitted label differs from the
running one.

> A summary phase runs at `[TRUTHFUL-REPORT]`'s priority but still counts against
> its Caller's `[RESEARCH]` cap, because it is the same delegated work under a
> second instruction.

Without it, the moment a task becomes a `[TRUTHFUL-REPORT]` it stops counting
against `[RESEARCH]` — and the same caller can put one more `[RESEARCH]` in flight
than the cap allows, for exactly as long as the summary takes. Which is exactly
when the partner is least able to take more.

**Priority comes from `behavior`; cap accounting comes from `origin_behavior`.**
One task, two labels, two different questions answered by two different columns.

---

## 5. `awaiting_resolution` — a question is not work

*Trace: "1 = an agent's own displaced, unanswered question".*

```sql
awaiting_resolution INTEGER NOT NULL DEFAULT 0 CHECK (awaiting_resolution IN (0, 1))
```

When an agent sends a `[QUERY]` or an `[ERROR]`, that act **stops the sender**.
Its remote is stopped, whatever it was working on is pushed back paused, and the
question itself takes its working slot. The question is the hold; there is no
separate hold label.

That slot task is synthetic — it has no queue row and ordinarily never gets one.
So when does this column ever get set?

Only a `[TRUTHFUL-REPORT]` outranks a waiting agent (priority 1 versus 2). So a
summary can displace a wait, and at that moment the question *becomes* a queue
row. The marker earns its place twice:

**It makes the row outrank everything else in that agent's queue** — it is read
*first*, ahead of priority, in both head statements. So when the summary finishes,
what resumes is the wait, not some job the agent still cannot do.

**It makes the resumed row re-enter the wait rather than be delivered.** Without
it the question comes back looking like an ordinary `[QUERY]` a caller had sent —
and the agent is handed, as work, the question it asked.

It also quietly qualifies a property stated elsewhere: *at most one paused row per
label*. That remains true of **work** rows. A wait carries a label too and can
share one with a paused task — it never collides, because a wait is never
rendered, and so is never what a resume prompt names.

---

## 6. `enqueued_at`, and the column that deliberately does not exist

*Trace: "start time is on the in-memory slot, not here".*

```sql
enqueued_at TEXT NOT NULL
            DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
```

When the message entered the queue. The other half of a latency measurement — when
it actually *started* — is deliberately **not** a column:

> a promoted row is DELETED, so a `dequeued_at` would only ever be written to a
> row about to disappear. … **A column with no surviving writer is worse than no
> column, because it reads like a measurement someone is taking.**

That sentence is the sharpest principle in the schema, and note 4 closes on a
whole table of columns dropped for exactly this reason.

The start time lives on the in-memory working slot beside the task it describes,
and `status` reports the difference. Which is why `_now()` in Python
(`core.py:210-219`) matches SQLite's `strftime` format exactly — one value comes
from the database and one from Python, and they get subtracted.

---

## 7. Admission — one statement, because two would race

*Trace: `04-priority-queue.png`, the yellow `ADMIT` subgraph: "Admission — ONE
statement, so a race cannot pass it twice".*

```sql
INSERT INTO message_queue (partner_id, caller_id, behavior, body, message_id)
SELECT :pid, :cid, :behavior, :body, :mid
 WHERE (SELECT max_outstanding FROM label_caps WHERE behavior = :behavior) IS NULL
    OR (SELECT COUNT(*) FROM message_queue
         WHERE partner_id = :pid AND caller_id = :cid
           AND COALESCE(origin_behavior, behavior) = :behavior) + :working
     < (SELECT max_outstanding FROM label_caps WHERE behavior = :behavior)
```

That is `_ADMIT_SQL` (`core.py:112-120`). **The admission test is the `WHERE`
clause of the insert itself.** If the predicate is false, the `SELECT` produces no
rows and nothing is inserted; the refusal surfaces as `rowcount == 0`
(`core.py:1919`) — never as an exception, never as a prior read.

The comment states the alternative it refuses:

> two concurrent callers can both pass a read-then-write check and both insert,
> which is precisely how a cap of three admits four.

This is the same doctrine as `create_partner`'s insert-and-catch (note 2). Make
the constraint do the checking, in one statement, then interpret the outcome.

### The cap is two cooperating terms, and neither is sufficient

Look at `+ :working`. The SQL counts *queued rows*. But the cap is about work **in
flight**, which includes the task currently in the working slot — and that task is
not a row, because promotion deleted it.

So `:working` is supplied by `WorkingSlots.outstanding()` (`slots.py:87-113`),
which returns 0 or 1 by looking in memory. Half the count is SQL; half is a Python
dict.

Note the symmetry between the two halves. The SQL uses
`COALESCE(origin_behavior, behavior) = :behavior`. The memory side asks whether
`task["behavior"] == behavior or task.get("origin_behavior") == behavior`. Same
rule about summary phases, expressed on both sides of the sum — because
`begin_summary_phase` relabels the task **in place, in that same slot, without it
ever leaving**.

**What makes the two halves atomic is a lock, not a transaction.** The caller
holds the partner's slot lock across both the `outstanding()` read and the
`db.write` (`core.py:2072` in `send`, `core.py:2711` in `report_back`). A swap
landing between them would count against a slot that no longer exists.

### Storage is decided in the same transaction

`_admit` inserts the `messages` row **before** running `_ADMIT_SQL`
(`core.py:1899-1905`). So an over-cap refusal rolls the message row back too —
*"a message that was never queued must not be readable as though it had been
delivered."* That only works because `db.write` wraps the whole closure in one
`BEGIN IMMEDIATE`.

---

## 8. The pop order — two statements, six keys

*Trace: the `Q` cylinder on `04-priority-queue.png`: "reading the head is TWO
questions, and awaiting_resolution leads BOTH of them".*

### Step one — which label runs next

```sql
SELECT q.behavior AS behavior
  FROM message_queue q JOIN label_caps c ON c.behavior = q.behavior
 WHERE q.partner_id = :pid
 GROUP BY q.behavior
 ORDER BY MAX(q.awaiting_resolution) DESC,
          MIN(c.priority) ASC, MIN(q.in_process) ASC,
          MIN(CASE WHEN q.in_process = 0 THEN q.enqueued_at END) ASC,
          MIN(q.enqueued_at) ASC
 LIMIT 1
```

Five keys, each earning its place:

**1. `MAX(awaiting_resolution) DESC`** — a label holding a displaced unanswered
question wins outright, *above priority*. `MAX` because the question is "does any
row of this label carry the marker".

**2. `MIN(c.priority) ASC`** — the label ranking. Lower wins.

**3. `MIN(in_process) ASC`** — 0 when the label has any fresh row, 1 when every row
of it is paused. At equal priority, a label with something new to say beats one
that only wants resuming. **This is the key that fixes the `[QUERY]`/`[ERROR]`
bug.**

**4. `MIN(CASE WHEN in_process = 0 THEN enqueued_at END) ASC`** — the earliest
arrival among *only the label's fresh rows*, and it exists because of a
second-order version of the same failure. Once a label holds both a paused row and
a fresh one, key 3 is 0 and ties with a wholly-fresh label. A plain
`MIN(enqueued_at)` would then fall back to the *oldest* row in the label — which
is precisely the paused one, since pausing happens to whatever has waited longest.
The same bug walks back in through the tie-break. The `CASE` maps a paused row to
NULL so it cannot supply the label's timestamp.

The code explicitly forbids adding `NULLS LAST` here: key 3 already separates "has
a fresh row" from "has none", so this key is only ever compared between two labels
that are both non-NULL or both NULL.

**5. `MIN(enqueued_at) ASC`** — the final tie-break.

### Step two — which row of that label

```sql
 ORDER BY q.awaiting_resolution DESC, q.in_process DESC, q.enqueued_at ASC, q.id ASC
```

**Here `in_process` is `DESC`** — paused *does* win. A partner finishes what it
started before starting anything else *of the same kind*. Scoping that to within
one label is the entire reason for the split.

And that within-label uniqueness is what lets the resume prompt be a single line:
at most one work row per label is ever paused, so *"resume your previous
`[RESEARCH]`"* has exactly one referent.

`q.id ASC` is the deterministic final tie-break — `enqueued_at` has millisecond
resolution, so two rows genuinely can share one.

### The answer is looked up by label, not taken from the head

One call to `_HEAD_ROW_SQL` deliberately bypasses step one entirely
(`core.py:2237-2238`): when the slot holds a wait, `advance` looks up
`[MESSAGE-RESPONSE]` **by label**.

The reason is a deadlock. `[MESSAGE-RESPONSE]` sits at priority 3 — below
`[QUERY]`/`[ERROR]` at 2 and `[TRUTHFUL-REPORT]` at 1. An agent whose paused work
carries one of those labels has a queue whose head is *not* the answer, however
long the answer has been sitting there. Reading the head would find that work,
refuse to displace an equal-or-better slot, and return `None` on every pass
forever: the agent waits for an answer that has already arrived.

> Priority orders WORK. It has nothing to say about the one message that ends a
> wait.

### The index, and the one thing it cannot contain

```sql
CREATE INDEX message_queue_order
    ON message_queue(partner_id, behavior, in_process DESC, enqueued_at);
```

Only `in_process` is `DESC`; `enqueued_at` is ascending. Non-unique, not partial.

**Priority is not in this index, and cannot be** — it lives in `label_caps` and
reaches the query through a join. Grouping by partner and behavior, with
`in_process` and `enqueued_at` ordered within, is exactly what both statements
scan.

Note also what the index does *not* cover: `awaiting_resolution`, now the leading
sort key in both statements. At the scale this runs at — one partner's queue,
rarely more than a handful of rows — that costs nothing. It is worth knowing it is
a deliberate non-match rather than an oversight.

---

## 9. Five writers, in three kinds

This is the structural fact that makes the table comprehensible.

| Writer | Via `_admit`? | Cap tested | `in_process` | Markers carried |
|---|---|---|---|---|
| `send` | yes | yes | 0 | — |
| `report_back` | yes | yes | 0 | — |
| `advance._swap` | **no** | no | 1 | all three |
| `_requeue` | **no** | no | 1 | all three |
| `_report_lost_work` | **no** | n/a | 0 | — |

**Admissions.** `send` and `report_back` both funnel through `_admit`, so both get
the `messages` row, the atomic cap test, the `over_queue` refusal, and the depth
count. `report_back` is not `send` — no requester uuid, no handshake check — but
it keeps the cap and the storage rule, *"because those are about the Caller's
queue rather than about who is allowed to talk to whom."*

**Re-entries.** `_swap` and `_requeue` bypass `_admit` entirely, and that is
correct: a displaced task was already counted on admission. Re-testing would let
the cap refuse a task re-entering the queue it is already in. They pay for the
bypass by carrying `summary_phase`, `origin_behavior` and `awaiting_resolution`
forward **by hand** — three columns copied explicitly off the slot, each one a
fact nothing downstream could reconstruct.

**The notice.** `_report_lost_work` is a third kind: a raw `INSERT` with no cap
check and no `messages` row (`core.py:822-826`). Justified precisely — `[ERROR]`
is uncapped and `stored = 0`, so there is no cap to check and no message to
create. And note the **direction reversal**: the vanishing partner becomes the
`caller_id`, and each waiting caller becomes the `partner_id`.

---

## 10. `messages` — what is kept

*Trace: the `MESSAGES` entity, and the edge `LABEL_CAPS ||--o{ MESSAGES : "decides
storage (behavior)"`.*

```sql
CREATE TABLE messages (
    id            INTEGER PRIMARY KEY,
    from_partner  INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    to_partner    INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    behavior      TEXT NOT NULL REFERENCES label_caps(behavior),
    body          TEXT NOT NULL,
    created_at    TEXT NOT NULL
                  DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX messages_readable ON messages(to_partner, id DESC);
```

Durable history for the three labels with `stored = 1`. `_admit` is the **only**
writer; there is no `UPDATE` and no `DELETE` — messages are immutable and vanish
only by cascade.

The index is `(to_partner, id DESC)` — **`id`, not `created_at`.** Since `id` is
the rowid and monotonically increasing, a newest-first read of one partner's inbox
comes straight out of the index's natural order with no sort step. Using
`created_at` would have given the same order at millisecond resolution and needed
a tie-break; the rowid is exact and free.

The table has **zero `CHECK` constraints**, deliberately. The storage rule is a
trigger reading `label_caps` instead — because *"a list here is a second copy of
`label_caps.stored` and two copies of one fact eventually disagree."*

---

## 11. The slot that is not on the diagram

*Trace: look for the working slot on the ER page. **It is not there**, and its
absence is the design.*

```
The task actually being worked -- the "working slot" -- is held in memory by the
Polling Server and is deliberately NOT in this table: it is process state, it
changes on every swap, and persisting it would invite a reader to believe it
survives a restart when it does not.
```

Three dicts guarded by one lock, keyed by `partners.id` (`slots.py:53-56`). A slot
holds the same shape a queue row is popped into, plus fields no row ever has:
`priority` (joined in by `_HEAD_ROW_SQL`), `prompt`, `remote_call_id`,
`started_at`, and `awaiting_resolution` for a wait that was never a row at all.

The argument against persisting it is worth stating fully, because the temptation
is real — a `working` table would make `status` a single query:

> Persisting it would put a row in the database that a reader would reasonably
> take for durable truth, and that reader would be wrong in exactly the situation
> where being wrong is expensive: after a crash, when the row says a partner is
> mid-task and no thread is driving it.

The remote's own turn does not survive a restart either. So a persisted slot would
describe a situation that cannot exist.

**The locks are per-partner and re-entrant**, not global — because a swap holds
its lock across remote I/O, a network round trip to Antigravity or Claude Science.
Under a single lock every drain thread in the process would queue behind whichever
partner's remote was slowest, which is the opposite of why drain threads exist.
Re-entrancy matters because `send` calls `advance`, and `advance` calls `_render`,
all on the same partner.

### The consistency question this raises

Head selection reads through a **reader connection**; the swap is a separate
`db.write`. Two different connections, two different transactions.

What makes that safe is the per-partner lock — which is **process-local**. The
deployment assumption underneath is that exactly one process serves each partner's
source, enforced by the Polling Server declining outright to drain a partner whose
source it holds no extension for.

That assumption is not visible in the schema. It is the thing a new engineer most
needs told, and it lives in `polling/server.py`.

---

## 12. Questions a new engineer asks

**"Why is there no `state` column?"** Because there is no third place a task can
be. It is queued, or it holds the working slot, or it is neither. A state column
could disagree with where the task actually is; the queue and the slot each answer
that question in exactly one place. An earlier design had a five-state
`polling_tasks` table and it could say *delivering* a minute after delivery
finished.

**"Why delete the row on promotion instead of flagging it?"** Because the queue
holds what is *waiting*. A busy partner would otherwise hold its own cap slot
twice — once as a row, once in the slot — and a cap of two would admit one.

**"Can I `UPDATE` a queue row's `in_process` to pause it?"** Nothing does, and you
should not add it. Pausing is DELETE-then-INSERT, which is what gives the resumed
row a fresh `enqueued_at` and a new rowid. Half the ordering logic assumes that.

**"What if two callers hit the cap simultaneously?"** They cannot both pass. The
test is inside the insert, and every write is serialised through one writer thread
holding `BEGIN IMMEDIATE`. That is the whole reason the cap is shaped this way.

**"Why does `report_back` need the cap at all?"** Because it writes into a
caller's queue, and a caller's queue is exactly what the cap protects. What it
does *not* need is the handshake check — it is the queue machinery replying, not
an agent deciding to talk.

---

*Next: where each rule actually lives — the five triggers by name, the three
places a rule can hide, the write path that makes all of this safe, and a table of
columns that were deliberately never carried over.*
