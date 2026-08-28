# Four: Enforcement and operation — where each rule lives, and how data moves

**Have open:** `visualizations/03-schema-er.png`. Start at the **title bar across
the top of the page** — it is the only place in the entire repository where all
five triggers are enumerated, and this note is the one that explains them. Then
`DRAIN_THREADS`, lower middle. Then re-walk the cascade edges into `PARTNERS`
knowing what `CASCADE` costs.

Secondary: `10-write-path.png`, end to end.

**The claim this note argues:** where a rule lives is part of the rule. A
constraint the database refuses to break and a check some function remembers to
run are different guarantees, and this schema is consistently explicit about
which it has.

---

## 1. Three places a rule can live

*Trace: read the title bar — "schema.sql -- 12 tables, 5 triggers
(partners_live_limit, partners_no_rename_archived, messages_stored_labels_only,
budget_grants_roles_insert, partner_paths_gemini_only)".*

**A `CHECK`, `UNIQUE`, or foreign key.** The database refuses, always, regardless
of which code path issued the statement. Cheapest and strongest — but it can only
see the row in front of it.

**A trigger.** Used where a constraint cannot reach: when the rule needs to count
rows, or consult another table. Still the database refusing.

**Application code.** Holds only for callers that go through that function.

The distinction is stated in `docs/05` in a sentence that is the whole argument:

> A rule enforced by a `CHECK`, a `UNIQUE` constraint, or a trigger cannot be
> bypassed by a new code path. A rule enforced only in application code can be,
> and will be, by the next person who writes a second route to the same table.

The honesty matters as much as the placement. A rule enforced in application code
and *described* as though the database guaranteed it is how someone later adds a
second path and quietly bypasses it. "Currently unreachable" describes today's
call graph, not a rule.

The schema has **21 `CHECK` constraints**, 20 foreign keys, 4 indexes and 5
triggers. Three tables carry no `CHECK` at all — `projects`, `messages` and
`drain_threads`.

---

## 2. The five triggers, by name

All five are `BEFORE` triggers using `RAISE(ABORT, …)`. Four fire on `INSERT`; one
fires on `UPDATE`. Four use the idiom `SELECT RAISE(...) WHERE <condition>` rather
than a `WHEN` clause.

### 2.1 `partners_live_limit` — because a `CHECK` cannot count

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

The reason it is a trigger is exactly one sentence: **a `CHECK` cannot count
rows.** It is still the database refusing rather than whichever caller remembers,
and the ceiling itself is data in `source_caps`.

*Trace: the `PROJECTS ||--o{ PARTNERS` edge is labelled "hosts (project_id);
capped by partners_live_limit" — the diagram names this trigger on the
relationship it constrains.*

**A gap worth stating plainly: it fires on `INSERT` only.** An `UPDATE partners
SET archived_at = NULL` — un-archiving — is not gated by the ceiling. No
capability does that today, so it is unreachable rather than broken; but anyone
adding an un-archive feature needs to know the ceiling will not stop them.

### 2.2 `partners_no_rename_archived` — the only `UPDATE` trigger

```sql
CREATE TRIGGER partners_no_rename_archived
BEFORE UPDATE OF title ON partners
WHEN OLD.archived_at IS NOT NULL AND NEW.title <> OLD.title
BEGIN
    SELECT RAISE(ABORT, 'an archived partner cannot be renamed; its title stays spent');
END;
```

The only trigger scoped to a column (`BEFORE UPDATE OF title`) and the only one
using a `WHEN` clause.

Renaming an archived partner would **free** its title, and the next partner to
take it would silently inherit an address other agents may still be holding. Note
2 covered why that is the dangerous failure: nothing detects a misdirected
message.

The schema is explicit about why this is not merely a check in the tool:

> a rule that lives solely in application code is bypassed by any path that
> issues the `UPDATE` directly.

### 2.3 `messages_stored_labels_only` — one authority, not two

```sql
CREATE TRIGGER messages_stored_labels_only
BEFORE INSERT ON messages
BEGIN
    SELECT RAISE(ABORT, 'this behavior is transport-only and is never stored')
     WHERE (SELECT stored FROM label_caps WHERE behavior = NEW.behavior) IS NOT 1;
END;
```

A `CHECK` cannot reach another table, so this is a trigger. But the deeper reason
is that the obvious alternative — a `CHECK (behavior IN ('[QUERY]', …))` listing
the three stored labels — would be **a second copy of `label_caps.stored`, and two
copies of one fact eventually disagree.**

With the trigger, flipping `stored` on a `label_caps` row changes what `messages`
accepts. That is what "the table is the authority" has to mean to be worth saying,
and the test suite asserts exactly that: flip the column, confirm the trigger
follows.

**Note `IS NOT 1`, not `<> 1`.** `IS NOT` is NULL-safe. A behavior absent from
`label_caps` makes the subquery return NULL; `NULL <> 1` is NULL, which SQLite
treats as *satisfied* — the row would pass. `IS NOT 1` aborts. Four of the five
triggers use this idiom, and each time it is what makes the missing-row case fail
closed.

### 2.4 `budget_grants_roles_insert` — two refusals in one body

```sql
CREATE TRIGGER budget_grants_roles_insert
BEFORE INSERT ON budget_grants
BEGIN
    SELECT RAISE(ABORT, 'granted_by must hold project-orchestrator')
     WHERE (SELECT orchestrator_type FROM partners WHERE id = NEW.granted_by)
           IS NOT 'project-orchestrator';
    SELECT RAISE(ABORT, 'grantee_partner must hold gemini-orchestrator')
     WHERE (SELECT orchestrator_type FROM partners WHERE id = NEW.grantee_partner)
           IS NOT 'gemini-orchestrator';
END;
```

A budget is granted **by** a project-orchestrator **to** a gemini-orchestrator.
Neither end was constrained before this existed, so a budget could be granted by
anyone to anyone — and the handshake rule that *spends* it would then be metering
a meaningless number.

Both `IS NOT` again, so a role-less partner (NULL `orchestrator_type`) is rejected
at both ends rather than slipping through.

*Trace: the two `BUDGET_GRANTS` column glosses — "must hold gemini-orchestrator"
and "must hold project-orchestrator" — are this trigger, drawn.*

**Second gap worth stating: the name ends `_insert`, and there is no matching
`UPDATE` trigger.** Changing `granted_by` or `grantee_partner` on an existing row
is unguarded by the database. `grant_gemini_budget` upserts, and its
`ON CONFLICT DO UPDATE` path does not re-run this trigger — so the role check on
an update relies on the application having verified the roles first, which it
does. But the database is not enforcing it there, and the trigger's name is the
only hint.

### 2.5 `partner_paths_gemini_only` — a grant nothing will apply

```sql
CREATE TRIGGER partner_paths_gemini_only
BEFORE INSERT ON partner_paths
BEGIN
    SELECT RAISE(ABORT, 'partner_paths applies only to a gemini_ partner')
     WHERE (SELECT pr.source_prefix FROM partners p
              JOIN projects pr ON pr.id = p.project_id
             WHERE p.id = NEW.partner_id) IS NOT 'gemini_';
END;
```

Resolves the source two hops out — partner to project to `source_prefix` — which a
`CHECK` cannot do.

The reasoning is about what a stray row would *look like*:

> A row for any other source is a grant that nothing will ever apply —
> indistinguishable, to a reader, from one that is being enforced.

Not a correctness bug so much as a lie in the data. Someone reading
`partner_paths` for a `science_` partner would see paths and conclude they were in
force.

---

## 3. Rules that live in code, and say so

Not everything can be a constraint, and the codebase does not pretend otherwise.

**Priority decides the working slot, and only a strict win displaces.** Pure
application logic, in one place — `advance`. Strictly, not "or equal": otherwise
two callers at equal priority ping-pong a partner between them and neither gets an
answer.

**A paused task outranks only within its own label.** Two SQL statements rather
than one ordering, because it is genuinely two comparisons over two groupings
(note 3).

**A cap counts work in flight.** Half in SQL, half in memory — no constraint could
express it.

**Delegated work never travels upward.** Read from `agent_layers` rather than
compared against literals, so a deployment that adds a tier adds a row. But the
check is in code, and the layer rule alone is not sufficient: a project-
orchestrator and its plain worker share layer 2. What actually stops a worker
delegating back to its director is the forward-handshake requirement.

**Authorization before existence.** `grant_gemini_budget` takes another partner's
uuid — the only capability that does. The check runs in a specific order:
authorization is tested **before anything that depends on the named partner
existing**. A requester without the role is refused without a single query
touching the uuid it supplied. And past that gate, a uuid naming a partner in a
different project and a uuid naming nothing at all get the **same** refusal —
deliberately not distinguished, so the shape of a refusal never becomes an oracle
for whether an identity exists.

---

## 4. The write path

*Trace: switch to `10-write-path.png` and follow it top to bottom.*

### One writer thread

```python
self._writer_thread = threading.Thread(
    target=self._writer_loop, name="messaging-db-writer", daemon=True
)
```

Every write in the system is a closure submitted to a `queue.Queue` as a
`(fn, future, is_write)` triple; the calling thread blocks on the future. One
thread owns one connection for its lifetime.

`_submit` puts the item on the queue **inside** a lock and calls `future.result()`
**outside** it — blocking on the result while holding the submit lock would
deadlock every other submitter behind one job.

**The re-entrancy hazard this creates is real and documented.** Calling
`self.db.write` from inside a closure already running on the writer thread would
wait on itself forever. That is why `_report_lost_work` takes the already-open
`conn` as a parameter (`core.py:746-753`) rather than opening its own write, and
why `report_back` is explicitly named as off limits from inside `archive_sessions`.

### `BEGIN IMMEDIATE`, and why not deferred

```python
conn.execute("BEGIN IMMEDIATE")
...
result = fn(conn)          # the whole closure
...
conn.execute("COMMIT")
```

`isolation_level=None` turns Python's implicit transaction handling off, so the
bracket is explicit. `IMMEDIATE` takes the write lock at statement one rather than
at the first write — so a multi-statement closure like `_swap`'s DELETE-then-
INSERT, or `archive_sessions`' per-title loop, is atomic as a unit.

`BaseException` is caught, not `Exception` — so even a `KeyboardInterrupt`
mid-closure rolls back rather than leaving a half-applied write.

And the exception propagates to the caller **with its original type**. That is
what lets `create_partner` catch `sqlite3.IntegrityError` and demultiplex it, and
lets `delete_partner` raise a `Rejected` from *inside* the closure and have it
reach the caller intact.

### Readers, and read-only that a pragma cannot undo

```python
uri = f"file:{quoted}?mode=ro"
conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
```

Reads never go through the writer. Each thread gets its own connection, cached in
`threading.local`, opened once.

The `mode=ro` is the point, and the docstring is worth quoting because the
distinction is easy to miss:

> this is not a connection *setting* like `PRAGMA query_only` that a later
> statement on the same connection can flip back off. Whatever a caller passes to
> `read()`, including `PRAGMA query_only = OFF`, this connection still cannot
> write, because the OS never gave it permission to in the first place.

Read-only is a property of the file descriptor. `PRAGMA query_only = ON` is set on
these connections too, but only as defence in depth.

### The `:memory:` exception

A `:memory:` database exists only inside the connection that created it — there is
no file a second connection could open. So the reader design collapses: reads are
routed through the **same job queue** as writes, with `is_write=False`.

Which creates a problem the disk path does not have. That shared connection *is*
the writer's. So `_run_read` brackets each read:

```python
conn.execute("PRAGMA query_only = ON")
try:
    result = fn(conn)
finally:
    conn.execute("PRAGMA query_only = OFF")
```

The `finally` is load-bearing: without it, a read that raised would leave the flag
on and the writer thread permanently unable to write. This is the **only**
read-only enforcement in the `:memory:` branch — there is no `mode=ro` descriptor
to fall back on.

WAL is deliberately skipped for `:memory:` — there is no file to journal against.

### The PRAGMAs, and the one that must be repeated

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
```

Those three sit at the top of `schema.sql`. **Only `journal_mode = WAL` persists
in the database file.** The other two are **per-connection settings**, so running
the schema script once does not make them permanent.

That is why `_configure_connection` re-issues them on every connection the class
opens, with a comment saying exactly this. And the consequence if one were missed
is severe rather than subtle: **without `foreign_keys = ON`, none of the twelve
`ON DELETE CASCADE` clauses fire and none of the foreign-key refusals happen.**
All twenty foreign keys become decorative. The schema would look correct and
enforce nothing.

`busy_timeout = 5000` is five seconds. Reader connections repeat `foreign_keys`
and `busy_timeout` but never touch `journal_mode` — that is the writer's job,
once, globally.

---

## 5. Cascades, and why deletion refuses where archiving reports

*Trace: count the edges converging on `PARTNERS`. Every one of them is a cascade —
except one.*

Twelve foreign keys cascade on the deletion of a partner or project. Deleting one
partner takes rows from `handshakes` (both directions), `partner_paths`,
`budget_grants` (as grantee), `messages` (both directions), `message_queue` (both
`partner_id` **and** `caller_id`), and `drain_threads`. Deleting a project
cascades into `partners` first, and then into all of that, plus
`project_extension`.

**The one exception is `budget_grants.granted_by`**, which has no `ON DELETE`
clause and therefore blocks. Note 2 covered it.

### The asymmetry

**Archiving reports. Deletion refuses.** And the reason is structural rather than
a policy preference.

Archiving is an `UPDATE`. The row survives, so the partner remains a valid message
*sender* — which is what lets `_report_lost_work` write an `[ERROR]` to every
caller with work queued, attributed to the vanishing partner. It also unions in
the working slot's caller, because `advance` already promoted that task out of the
queue and a queue-only scan would miss the one task actually in flight.

Deletion cannot do that. `message_queue.caller_id` is `ON DELETE CASCADE`, so an
`[ERROR]` written to explain the loss would be **destroyed by the very deletion it
was warning about**. And attributing it to the requester instead collides with
`CHECK (caller_id <> partner_id)` in the normal case where the requester *is* the
waiting caller.

> The notice cannot outlive the row it depends on.

There is no shape of message that survives. So `delete_partner` refuses when work
is in flight — checking both the queue **and** the working slot — and names
`archive_sessions` as the route that reports. `delete_project` carries the same
two guards applied to every partner the cascade would take.

### `report_back` when the recipient has already gone

The liveness check is **inside** the same write transaction that would otherwise
insert. A separate read would leave a window for a deletion to land in — *"which
is exactly the read-then-write shape `_ADMIT_SQL`'s own comment refuses to use,
for the same reason."*

On a miss it returns quietly with `delivered: False` rather than raising. Because
the Polling Server calls `report_back` **after** releasing the working slot, an
exception there cannot be recovered from: the slot is already empty, so the answer
cannot be put back, and the drain thread is left stranded on an error nobody
reads. The caller was already told its work was gone by whichever of archiving or
deleting caused this.

---

## 6. `drain_threads` — a registry with one reader

*Trace: the `DRAIN_THREADS` entity, and its edge: "drained by (partner_id); row
DELETED when the thread retires". The rule is written on the relationship.*

```sql
CREATE TABLE drain_threads (
    partner_id  INTEGER PRIMARY KEY REFERENCES partners(id) ON DELETE CASCADE,
    thread_id   TEXT NOT NULL,
    started_at  TEXT NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) WITHOUT ROWID;
```

`partner_id` as primary key means at most one drain thread per partner, by
construction.

A SQLite detail worth knowing: in a `WITHOUT ROWID` table, `INTEGER PRIMARY KEY`
is **not** a rowid alias, so there is no implicit autoincrement and the value must
always be supplied. Which is correct here — it is a foreign key.

**The surprising part: this table has exactly one reader.** No arming path
consults it. `_ensure_thread` and the supervisor scan both de-duplicate against an
in-process dictionary. The only `SELECT` is in `start()`.

So what is the row *for*? It survives a restart. Its whole job is to let `start()`
re-arm a partner's thread in a new process.

**A thread deletes its own row when it retires** — when nothing is queued and
nothing is working. A row left behind is a claim that a thread is running when
none is.

**Shutdown is the deliberate exception.** `stop()` does *not* delete rows: it
signals threads for a process that is going away with work possibly still queued,
and the row is exactly what `start()` uses to bring that partner's thread back.

No owning PID column was added. It would answer "is this row stale" more directly,
but a stale row costs one thread that discovers it has nothing to do and retires
at once — and **a column exists to be maintained.**

---

## 7. Migration — smaller than you expect, and honest about it

```python
_ADDITIVE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("message_queue", "summary_phase INTEGER NOT NULL DEFAULT 0 CHECK (summary_phase IN (0, 1))"),
    ("message_queue", "origin_behavior TEXT REFERENCES label_caps(behavior)"),
    ("message_queue",
     "awaiting_resolution INTEGER NOT NULL DEFAULT 0 "
     "CHECK (awaiting_resolution IN (0, 1))"),
)
```

All three entries are on `message_queue`, and all three are **also** declared in
`schema.sql`. On a fresh database this is a no-op; the list exists for databases
created before each column was added.

The scope statement is the part to internalise:

> This is the whole migration story this project has: there is no version table
> and no migration runner, because every schema change so far has been adding a
> column with a default. This method does exactly that and nothing more — it does
> NOT drop a column, does NOT change a column's type, does NOT reorder columns,
> and has no way to express a non-additive change. A change like that needs a real
> migration, not another entry here.

`PRAGMA table_info` is checked before any `ALTER` is issued, so the common case is
a read rather than a write. A missing table is skipped rather than an error. It
runs on every open — and, notably, *before* `foreign_keys = ON` is set.

`_apply_schema_if_needed` only runs `schema.sql` against a database with **zero
tables**. Which is precisely why `_ADDITIVE_COLUMNS` has to exist: an existing
database never re-reads the schema file, so a column added there would never
appear.

**One consequence worth stating: seed data is never reconciled.** Only columns.
If a `label_caps` priority changed in `schema.sql`, an existing database would
keep its old value forever.

---

## 8. Timestamps, at one precision

```sql
DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
```

Identical in all eight places it appears. UTC, milliseconds, `Z` suffix, stored as
`TEXT`, `NOT NULL` every time.

The reason is a bug that predates this schema: the live JSON state it replaced
carried **two different precisions** because two code paths wrote it — which is
enough to break any naive comparison. Python's `_now()` reproduces the same shape
exactly, because `enqueued_at` comes from SQLite and `started_at` comes from
Python and the two get subtracted.

---

## 9. The closing lesson — columns deliberately not carried over

The system this schema replaced had an on-disk state file with fields that looked
load-bearing and were dead. They were not ported, and the reasoning was recorded
so nobody re-adds them:

| Dropped | Why |
|---|---|
| `notify_once` | Written by a superseded design, read by nothing. |
| `arm` | **The dangerous one.** It reads as persisted subscription state and is never written. A restart drops every watch while the file still says `"arm": "turn"`. Subscriptions are in-memory by design; the schema must not imply otherwise. |
| `lastPromptId`, `lastNotifyAt` | Kept in memory by the notification pump. Persisting them would re-announce or suppress across a restart. |
| `partialsThisTurn` | Never written, never read. |
| `queued_message_id` | A misnamed boolean — it held `"queued"` or null, never an id. Replaced by the presence of a `message_queue` row, which is the real fact. |
| the record's own `name` | Redundant with the key it was stored under. |

`arm` is the one to remember. It is not merely useless — it is **actively
misleading**, because a reader would reasonably take it for durable truth and be
wrong exactly after a restart, when being wrong is expensive.

That is the same argument as three other decisions in this schema, and seeing them
as one idea is the point of this note:

- no `dequeued_at`, because a promoted row is deleted
- no working-slot table, because process state does not survive a restart
- no owning PID on `drain_threads`, because a column exists to be maintained

**A column with no surviving writer is worse than no column, because it reads like
a measurement someone is taking.**

---

## 10. Questions a new engineer asks

**"Why not enforce everything with triggers?"** Because a trigger cannot see
process state, and much of what matters here is process state — the working slot,
the priority comparison, the cap's in-memory half. The schema enforces what it can
see, and the code is explicit about the rest.

**"What happens if I open the database with a connection that forgets
`foreign_keys = ON`?"** Every cascade silently stops firing and every FK refusal
disappears. Deleting a partner would leave orphaned queue rows pointing at nothing.
This is why the pragma is re-issued on every connection rather than trusted from
the schema file, and it is the single most consequential per-connection setting in
the system.

**"Can I add a column?"** Yes — declare it in `schema.sql` *and* add it to
`_ADDITIVE_COLUMNS`, with a default so existing rows are valid. Anything
non-additive needs a real migration this project does not have.

**"Why does `stop()` leave `drain_threads` rows behind?"** Because a shutdown is
not a retirement. Retirement means "there is nothing left to do"; shutdown means
"this process is going away, possibly with work still queued". The row is how the
next process learns to re-arm.

**"How do I know a rule is actually enforced rather than just intended?"** Look at
where it lives. If it is a `CHECK`, `UNIQUE`, foreign key or trigger, it holds
against any code path. If it is in application code, it holds for callers that go
through that function — and `docs/05` names, for each of its twenty-three
invariants, which of the two it is.

---

*That is the database: three tables of policy nothing writes, an entity graph
hubbed on `partners` with three kinds of name, one queue whose hardest columns
exist to reconcile rows with a slot that is not a row, and a set of rules that are
honest about whether the database or the code is the thing enforcing them.*
