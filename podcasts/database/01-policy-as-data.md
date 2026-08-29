# One: Policy as data — the three tables nothing ever writes

**Have open:** `visualizations/03-schema-er.png`. This note lives in the upper
portion of that page: `SOURCE_CAPS` at the top, `AGENT_LAYERS` below and left of
it, `LABEL_CAPS` over on the right. Secondary, if you want it:
`04-priority-queue.png`, the box labelled `label_caps.priority -- lower wins`.

**The claim this note argues:** every behavioural rule in this system that could
have been an `if` statement is a row in a table instead. Three tables carry that
policy. They are seeded when the database is created and **no runtime code path
ever inserts, updates or deletes a row in any of them.** A new source, a new tier
in the delegation hierarchy, a change to what a label costs — each is a row, not a
deployment.

That is a design stance rather than an accident, and the rest of the schema is
built on top of it. Get these three tables and you can predict most of the
system's behaviour without reading a line of Python.

---

## 1. What "nothing ever writes" actually means

*Trace: find `SOURCE_CAPS`, `AGENT_LAYERS` and `LABEL_CAPS` on the diagram. Notice
that every edge touching them points **outward** — nothing flows in.*

Grep the codebase for `INSERT INTO source_caps`, `UPDATE label_caps`,
`DELETE FROM agent_layers`. Outside `schema/schema.sql` itself and the test suite,
there are none. The only occurrences of these three table names in runtime code
are `SELECT`s and JOINs.

That distinguishes them sharply from the other nine tables. `partners` is written
by `create_partner`, `claim_orchestrator`, `archive_sessions` and
`delete_partner`. `message_queue` has five distinct writers. These three have
zero. They are read-only reference data — configuration that happens to live in
SQLite rather than in a YAML file or a Python constant.

Two consequences worth holding onto:

**Changing policy means changing seed data.** There is no admin capability, no
tool, no MCP surface for adjusting a cap or adding a source. You edit
`schema/schema.sql` and create a new database, or you issue SQL by hand. This is
deliberate: policy changes are rare and consequential, and making them require a
schema edit puts them in code review alongside everything else.

**Reading them is cheap and constant.** They are tiny — four rows, seven rows,
five rows — and SQLite keeps them in page cache indefinitely. The joins in the
hot path (`_ADMIT_SQL`, `_HEAD_LABEL_SQL`, `_HEAD_ROW_SQL` all join `label_caps`)
cost effectively nothing.

---

## 2. `source_caps` — capability as data

*Trace: the `SOURCE_CAPS` entity, six rows of columns. Read the four glosses that
say `0 for nlm_`.*

### The table

```sql
CREATE TABLE source_caps (
    source_prefix   TEXT PRIMARY KEY
                    CHECK (source_prefix IN ('nlm_', 'code_', 'science_', 'gemini_')),
    max_live_partners INTEGER NOT NULL DEFAULT 10 CHECK (max_live_partners > 0),
    can_execute     INTEGER NOT NULL DEFAULT 1 CHECK (can_execute IN (0, 1)),
    needs_handshake INTEGER NOT NULL DEFAULT 1 CHECK (needs_handshake IN (0, 1)),
    can_send        INTEGER NOT NULL DEFAULT 1 CHECK (can_send IN (0, 1)),
    accepts_research INTEGER NOT NULL DEFAULT 1 CHECK (accepts_research IN (0, 1))
);
```

Four rows are seeded, and only one of them is interesting:

```sql
('nlm_',     10, 0, 0, 0, 0),  -- context only: never executes, never sends, answers queries
('code_',    10, 1, 1, 1, 1),
('science_', 10, 1, 1, 1, 1),
('gemini_',  10, 1, 1, 1, 1);
```

`code_`, `science_` and `gemini_` are identical. **All the information in this
table is in the `nlm_` row.** Its four zeros are four separate refusals, and each
one is enforced somewhere different.

### The four zeros, one at a time

**`can_execute = 0` — a NotebookLM partner never runs anything.** There is no
turn to stop, so `stop_remote_execution` has nothing to cancel. The adapter
inherits `NonExecutingExtension`'s refusal (`extension/base.py:184-188`), which
raises `Rejected("not_executable", …)`. That code is one of exactly two members of
`_UNCANCELLABLE` in `messaging_core/core.py:91` — the set of refusals that mean
"this remote has no cancel", as opposed to "the cancel failed". The distinction
matters: a designed refusal is recorded and the displacement proceeds; a genuine
failure aborts the swap.

**`needs_handshake = 0` — you may message a notebook without being introduced.**
Read by `_needs_handshake` (`messaging_core/core.py:395-401`), which joins
`partners → projects → source_caps` rather than testing the prefix string. That
indirection is the point: the rule is "whatever `source_caps` says", not
"whatever `nlm_` means today". `handshake` refuses a handshake toward such a
target outright with `handshake_not_needed` (`core.py:1259-1264`) — offering one
would imply the relationship means something.

**`can_send = 0` — a notebook is reachable but never a caller.** This is the
sharpest of the four. A NotebookLM partner is a knowledge base; there is no agent
behind it that decides to speak. `send` checks it first among the source rules
(`core.py:1971-1977`) and refuses `source_cannot_send`. The schema comment states
the consequence in one line worth memorising: *"A partner that cannot send is
reachable but never a caller."*

**`accepts_research = 0` — a notebook answers, it does not act.** `[RESEARCH]`
asks its recipient to go and do something. A notebook only answers questions
about what it already holds. `send` refuses with `research_not_accepted`
(`core.py:1978-1985`) — a *different* code from the one above, because it is a
different mistake with a different fix. The first means "this thing cannot talk to
you"; the second means "use `[QUERY]` instead".

**What breaks without them:** each zero, removed, produces a distinct silent
failure. Without `can_send`, nothing prevents code from constructing a message
whose sender is a database with no agent — it would be admitted and would sit in
a queue forever with nothing to answer it. Without `accepts_research`, a
`[RESEARCH]` reaches a notebook that will answer it as though it were a question,
and the caller receives a summary of a task nobody performed.

### `max_live_partners` — a number that is data twice over

```sql
max_live_partners INTEGER NOT NULL DEFAULT 10 CHECK (max_live_partners > 0)
```

Ten live (non-archived) partners per project. This is *the* reason
`archive_sessions` exists as a capability: it is how you free a slot.

The number being a column rather than a constant is only half of it. The other
half is where it is enforced — a trigger, `partners_live_limit`, which reads this
very column:

```sql
CREATE TRIGGER partners_live_limit
BEFORE INSERT ON partners
BEGIN
    SELECT RAISE(ABORT, 'project is at its live-partner limit; archive one first')
     WHERE (SELECT COUNT(*) FROM partners
             WHERE project_id = NEW.project_id AND archived_at IS NULL)
         >= (SELECT c.max_live_partners
               FROM projects pr JOIN source_caps c ON c.source_prefix = pr.source_prefix
              WHERE pr.id = NEW.project_id);
END;
```

Note 4 covers triggers properly. What matters here is the shape: the *limit* is
data in `source_caps`, and the *enforcement* is in the database. Neither is in
application code. Raise the column to 20 and the ceiling moves, with no code
change and no possibility of a caller that forgot to check.

*Trace: on the diagram, the edge `PROJECTS ||--o{ PARTNERS` is labelled "hosts
(project_id); capped by partners_live_limit" — the diagram names the trigger on
the relationship it constrains.*

### What is deliberately **not** in this table

There used to be a `max_queue` column. It was removed, and the schema comment
records why:

> a queue limit is not a property of the source. It is a property of
> `(caller, label)` — see `label_caps` — because the thing worth limiting is how
> much of one KIND of work one caller may have outstanding against one partner,
> not how many messages a partner can hold in total.

This is worth dwelling on because it is a genuine modelling correction rather
than a tidy-up. "How many messages can this partner hold" is a question about
storage. "How many research tasks may one caller have in flight against one
partner" is a question about *work*, and it is the one that actually needs
answering. Moving the cap to `(caller, label)` changed it from a resource limit
into a coordination rule.

---

## 3. `agent_layers` — position in the hierarchy, as data

*Trace: the `AGENT_LAYERS` entity. Three columns, two of them `PK`. Read the
`layer` gloss: "0 nlm_/code_ .. 4 gemini_; RESEARCH never travels up".*

### The table

```sql
CREATE TABLE agent_layers (
    source_prefix     TEXT NOT NULL REFERENCES source_caps(source_prefix),
    orchestrator_type TEXT NOT NULL,
    layer             INTEGER NOT NULL CHECK (layer >= 0),
    PRIMARY KEY (source_prefix, orchestrator_type)
) WITHOUT ROWID;
```

This is the first of four `WITHOUT ROWID` tables in the schema. The others are
`partner_paths`, `drain_threads` and `project_extension`. In an ordinary SQLite
table, a composite `PRIMARY KEY` is implemented as a separate unique index over a
hidden 64-bit rowid, so every lookup is two B-tree descents: one into the index to
find the rowid, one into the table to find the row. `WITHOUT ROWID` clusters the
rows *in* the primary key's B-tree, so the key lookup **is** the row lookup. For a
seven-row table read on every `send`, that is not a performance decision worth
arguing about — it is simply the correct shape for a table whose entire identity
is its composite key and which has no surrogate id anyone references.

*(The schema states no reason for the `WITHOUT ROWID` choices. That reading is
inference from the shape, not something the file asserts.)*

### The seven rows

```sql
('nlm_',     '*',                    0),
('code_',    '*',                    0),
('science_', 'bridge-scientist',     1),
('science_', 'project-orchestrator', 2),
('science_', 'gemini-orchestrator',  3),
('science_', '*',                    2),
('gemini_',  '*',                    4);
```

**A lower number is higher up.** The hierarchy reads:

> NotebookLM, Claude Code > bridge-scientist > project-orchestrator >
> gemini-orchestrator > Antigravity

Two things in that list surprise people. First, NotebookLM sits at the *top*
alongside Claude Code — layer 0. That is not a claim about importance; it is a
claim about direction. Layer 0 means "delegated work never flows up to here",
which is trivially true of a notebook that accepts no research at all. Second,
`science_`'s `'*'` default is layer **2**, the same as `project-orchestrator` —
so a plain `science_` worker holding no role sits at the same layer as the
orchestrator that directs it.

### The one rule this table exists for

**`[RESEARCH]` only ever travels down or sideways.** Delegated work flows away
from whoever is directing it. A lower agent handing `[RESEARCH]` back up would be
reassigning its own director's work.

Every other label travels freely in both directions — which is exactly what makes
an answer, an error, or a report able to come back. Without that asymmetry the
system would either be unable to delegate or unable to reply.

**Sideways is deliberately allowed.** Two partners at the same layer in extended
projects are branches of one research effort, not a chain of command. This is why
the check is `>` rather than `>=`.

`send` reads it at `core.py:1991` and refuses `research_cannot_flow_upward`.

Note the *second* rule that has to exist alongside it: a plain worker and its
project-orchestrator share layer 2, so the layer check alone would let a worker
delegate back to its own director. What actually prevents that is a separate
requirement that `[RESEARCH]` travel along a handshake in the direction the
orchestrator claimed — `research_needs_a_forward_handshake`. The layer rule is
necessary and not sufficient, and note 2 picks that up.

### `'*'`, and why the `CASE` in the lookup is not cosmetic

```sql
SELECT layer FROM agent_layers
 WHERE source_prefix = :src AND orchestrator_type IN (:role, '*')
 ORDER BY CASE orchestrator_type WHEN '*' THEN 1 ELSE 0 END
 LIMIT 1
```

That is `_LAYER_SQL` (`core.py:202-207`). `'*'` is the source's default, used when
no row names the partner's actual `orchestrator_type` — **including a partner
holding no role at all.**

The `CASE` is the part worth stopping on. A more specific row must beat the
default. But `'*'` sorts *before* every real role name alphabetically, so a plain
`ORDER BY orchestrator_type` would make the default win by accident, every time.
A `bridge-scientist` (layer 1) would be read as the plain `science_` default
(layer 2) — placed *below* the project-orchestrator it is supposed to sit above,
silently, with no error anywhere.

The `CASE` maps `'*'` to 1 and everything else to 0, so specificity wins by
construction.

There is a second piece of NULL behaviour being exploited rather than special-
cased. A role-less partner binds `:role` to SQL `NULL`, and
`orchestrator_type IN (NULL, '*')` never matches on the NULL side — so it falls
through to the `'*'` row cleanly, with no `COALESCE` and no branch.

**What breaks without the fallback:** `_layer` returns `1_000_000` if no row
matches at all (`core.py:450-456`). An unplaced agent is treated as the very
bottom of the hierarchy — it may *receive* delegated work and may not delegate
upward to anyone. That is the safe direction to be wrong in.

---

## 4. `label_caps` — four independent rules sharing one table

*Trace: the `LABEL_CAPS` entity on the right of the page. Five columns. Then find
the self-loop: `LABEL_CAPS ||--o| LABEL_CAPS : "answered by (reply_behavior)"` —
a table with a foreign key to itself, drawn as an arrow that leaves and returns.*

### The table

```sql
CREATE TABLE label_caps (
    behavior        TEXT PRIMARY KEY
                    CHECK (behavior IN ('[TRUTHFUL-REPORT]', '[QUERY]', '[ERROR]',
                                        '[MESSAGE-RESPONSE]', '[RESEARCH]')),
    priority        INTEGER NOT NULL,
    max_outstanding INTEGER CHECK (max_outstanding IS NULL OR max_outstanding > 0),
    stored          INTEGER NOT NULL DEFAULT 0 CHECK (stored IN (0, 1)),
    reply_behavior  TEXT REFERENCES label_caps(behavior),
    CHECK (reply_behavior IS NULL OR reply_behavior <> behavior)
);
```

```sql
('[TRUTHFUL-REPORT]',  1, NULL, 1, NULL),
('[QUERY]',            2,    3, 1, '[MESSAGE-RESPONSE]'),
('[ERROR]',            2, NULL, 0, '[MESSAGE-RESPONSE]'),
('[MESSAGE-RESPONSE]', 3, NULL, 1, NULL),
('[RESEARCH]',         4,    2, 0, '[TRUTHFUL-REPORT]');
```

Read that as **four separate rules that happen to share a home.** They are not
facets of one concept; they are four different questions that happen to be keyed
by the same thing.

### Rule one — `priority` decides the working slot

Lower wins. `[TRUTHFUL-REPORT]` at 1, `[QUERY]` and `[ERROR]` tied at 2,
`[MESSAGE-RESPONSE]` at 3, `[RESEARCH]` at 4.

`[TRUTHFUL-REPORT]` is highest so that a summary completes without other traffic
contaminating the context it is summarising. The obvious objection — doesn't a
label at the top starve everything else? — is answered by when it occurs: a
`[TRUTHFUL-REPORT]` is only ever produced *after* a `[RESEARCH]` has already been
drained. Nothing waits behind it that was not already waiting.

**`[MESSAGE-RESPONSE]` is second, and its position is load-bearing.** It is not
merely an answer arriving; it is what **restarts an interrupted agent**, taking the
empty slot and clearing the flag. Rank it below the requests and an agent could sit
interrupted with its restart signal queued behind fresh work it cannot act on.

**`[ERROR]` outranks `[QUERY]`, and that ordering is a claim about causation.** An
`[ERROR]` is normally a permission that was missing before the work started. Letting
a `[QUERY]` run first means querying against a grant nobody has fixed yet — so the
correction goes first, and then the asking.

Read the five ranks as a single sentence: **answers before asks, and among the asks,
the one that unblocks before the one that merely wants to know.**

Note what the table no longer does. No two labels share a rank. That is worth saying
because the queue's ordering rules are still written to survive a tie — note 3 tells
that story — and the reason is not superstition: a tie is a thing a later deployment
can introduce with a single edit to this table, and the rules that protect against it
cost nothing to keep.

### Rule two — `max_outstanding` caps work in flight

NULL means uncapped. Only two labels are capped: `[QUERY]` at 3, `[RESEARCH]` at
2 — the two that ask a partner to go and do something. The other three are
answers or reports, and refusing an answer because it is the fourth would be
absurd.

The critical word is **outstanding**, not *queued*. The cap counts the working
task as well as queued rows, so it limits work in flight. Note 3 shows the
mechanism, which is genuinely unusual: half the count is a SQL `COUNT` and half is
an in-memory lookup, and they are only correct together.

### Rule three — `stored` decides durable history

Three labels are written to `messages` and readable later: `[QUERY]`,
`[TRUTHFUL-REPORT]`, `[MESSAGE-RESPONSE]`. Two are not: `[RESEARCH]` and
`[ERROR]`. Those two are **transport** — they travel in a queue, are acted on, and
are never written down.

Why those two: `read` exists so an agent can recover context it has lost. A stored
`[RESEARCH]` would replay delegated work into that context on every call. An
`[ERROR]` is a condition to be resolved; resolving it is what matters, not the
record of it.

This rule is enforced by a trigger that *reads this column*, rather than by a
`CHECK` listing the labels. The schema comment gives the reason in a sentence
worth stealing: *"a list here is a second copy of `label_caps.stored` and two
copies of one fact eventually disagree."* Flip `stored` on a row and what
`messages` accepts changes — which is what "the table is the authority" has to
mean in order to be worth saying.

### Rule four — `reply_behavior`, where NULL is the load-bearing value

*Trace: the self-loop edge, and the gloss on the column: "NULL is what ends an
exchange".*

`reply_behavior` is what a partner sends back when a task carrying this label
finishes. Three labels expect an answer:

- `[RESEARCH]` → `[TRUTHFUL-REPORT]` (by way of the summary phase)
- `[QUERY]` → `[MESSAGE-RESPONSE]`
- `[ERROR]` → `[MESSAGE-RESPONSE]`

That last one is easy to miss and is not decorative: an agent that reported a
problem otherwise has no way to know its correction landed.

The other two — `[TRUTHFUL-REPORT]` and `[MESSAGE-RESPONSE]` — reply with
**nothing**, and that NULL is why the system terminates.

> Without a label whose reply is nothing, every completed task would produce a
> message that produced a task that produced a message, and two agents would talk
> to each other until one was archived.

Two agents, both behaving correctly at every individual step, in an infinite
exchange. Nothing in the priority system would stop it — each message would be
legitimate, admitted, delivered, answered. The termination condition is not a loop
guard or a hop counter. It is a NULL in a column.

### The self-reference, and the `CHECK` guarding it

```sql
reply_behavior  TEXT REFERENCES label_caps(behavior),
CHECK (reply_behavior IS NULL OR reply_behavior <> behavior)
```

The foreign key is to `label_caps` itself — that is the self-loop on the diagram.
It means a reply must name a real label, not an arbitrary string.

The `CHECK` forbids a label replying with itself, which is the same infinite
exchange written more compactly: a label answered by itself is a two-agent
ping-pong with a shorter description. Note it is written NULL-tolerantly. A bare
`reply_behavior <> behavior` against NULL evaluates to NULL, which SQLite treats
as satisfied anyway — so the explicit `IS NULL OR` changes nothing mechanically
and everything about readability. It says what is meant.

---

## 5. Why this is a table and not a module of constants

The alternative design is obvious and worth naming: a Python dict, or a set of
module constants, or an enum with attributes. The codebase explicitly rejects it.
`messaging_core/labels.py` carries the tuple of five label names and nothing else,
and says why: duplicating the per-label facts in Python would create *"a second
authority that could disagree."*

Three concrete gains from the table:

**The join is the lookup.** `_ADMIT_SQL` reads `max_outstanding` in the same
statement that performs the insert. `_HEAD_LABEL_SQL` and `_HEAD_ROW_SQL` join
`priority` in as part of the ordering. A Python dict would require reading the
value out, passing it into the query as a parameter, and hoping the two stayed in
step — which is precisely the read-then-write shape the cap is designed to avoid.

**The foreign key is real.** `message_queue.behavior` and `messages.behavior` both
`REFERENCES label_caps(behavior)`. An unknown label cannot be queued, not because
a validator rejects it but because the database will not store it.

**The trigger can read it.** `messages_stored_labels_only` consults
`label_caps.stored` at insert time. A constant in Python is invisible to SQLite; a
row is not.

---

## 6. Questions a new engineer asks

**"Can I add a fifth source?"** Yes — a row in `source_caps`, a row in
`agent_layers`, and an adapter. The `CHECK` on `source_prefix` lists the four
current values, so that constraint would need widening too; the schema is honest
that the four are enumerated rather than open. But nothing in the application code
branches on the prefix string: `_partner_type` resolves it by join, and every
behavioural question routes through `source_caps`.

**"Why is `priority` not `NOT NULL` with a default?"** It is `NOT NULL`, with no
default — deliberately. There is no sensible default priority; every label must
declare one. Contrast `stored`, which defaults to 0: "not stored" is a safe
assumption, and storing something by accident is worse than failing to.

**"What happens if two labels share a priority?"** Two already do —`[QUERY]` and
`[ERROR]`. Nothing breaks, because displacement requires a *strictly* lower
number, and the tie is resolved by a second ordering key scoped within a label.
This is the single most consequential fact in the queue design and it is note 3's
main subject.

**"Is `max_outstanding = 0` legal?"** No — `CHECK (max_outstanding IS NULL OR
max_outstanding > 0)`. A cap of zero would mean "this label may never be sent",
which is a different rule and belongs in `source_caps` as a capability flag, not
here as a quantity of nothing. Compare `budget_grants.budget_count`, where
`BETWEEN 0 AND 3` *does* permit zero — because a budget of zero is a meaningful
grant that has been spent, not a contradiction.

**"Nothing writes these tables — so what happens on an upgrade?"**
`_apply_schema_if_needed` only runs `schema.sql` against a database with zero
tables. An existing database keeps its seeded rows, including any that have since
changed in the file. There is no reconciliation of seed *data*, only of added
*columns*. Note 4 covers the migration story, which is smaller than you expect and
honest about its limits.

---

*Next: the entity graph — `projects`, `partners`, and the four tables that decide
who may speak to whom. Three different names for one partner, and why each exists.*
