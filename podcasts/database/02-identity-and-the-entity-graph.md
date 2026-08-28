# Two: Identity and the entity graph — who exists, and who may speak to whom

**Have open:** `visualizations/03-schema-er.png`. This note walks the centre and
lower-left of the page: `PROJECTS` at the top, `PARTNERS` in the middle (the tall
box with nine columns), and the four entities hanging off it —`HANDSHAKES`,
`PARTNER_PATHS`, `BUDGET_GRANTS`, and `PROJECT_EXTENSION` sitting between the two
`PROJECTS` edges. Secondary: `09-identity-and-addressing.png` for the addressing
story, and `07-handshake-legality.png` for the decision tree these tables feed.

**The claim this note argues:** a partner has three different names, each with a
different lifetime and a different scope, and almost every subtle rule in this
part of the schema follows from keeping them apart.

---

## 1. `projects` — the unit of remote identity

*Trace: the `PROJECTS` entity, five columns, with two `UK` markers.*

```sql
CREATE TABLE projects (
    id                INTEGER PRIMARY KEY,
    source_prefix     TEXT NOT NULL REFERENCES source_caps(source_prefix),
    project_system_id TEXT NOT NULL,
    title             TEXT NOT NULL,
    created_at        TEXT NOT NULL
                      DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (title),
    UNIQUE (source_prefix, project_system_id)
);
```

A project is this system's handle on a remote application's own container: a
NotebookLM notebook, a Claude Science project, an Antigravity workspace.
`project_system_id` is that remote's identifier for it; `source_prefix` says which
remote issued it.

**A project has exactly one `source_prefix`, and that single column is the
authority for a great deal downstream.** It is the reason a cross-source pair is
*necessarily* cross-project — a `science_` partner and a `gemini_` partner cannot
share a project, because a project cannot have two sources. That structural fact
is used directly in the handshake rules, and it is drawn on
`09-identity-and-addressing` as "different source_prefix implies different
Project, BY CONSTRUCTION".

**`UNIQUE (source_prefix, project_system_id)`, not `UNIQUE (project_system_id)`.**
The same reasoning that governs `partner_id_in_remote` below: a remote's
identifier is only meaningful inside the remote that issued it.

There is **no `UPDATE` of `projects` anywhere in the codebase.** A project's
title, source and system id are immutable once created. The only write after
insert is deletion.

---

## 2. `partners` — and the three names for one thing

*Trace: the `PARTNERS` entity — the tall box. Read the nine columns top to bottom;
that order is exactly the order below, because the diagram draws every table's
columns in schema order.*

```sql
CREATE TABLE partners (
    id             INTEGER PRIMARY KEY,
    uuid           TEXT NOT NULL UNIQUE,
    project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title          TEXT NOT NULL,
    partner_id_in_remote TEXT NOT NULL,
    descr          TEXT NOT NULL CHECK (length(descr) <= 1200),
    orchestrator_type TEXT
                   CHECK (orchestrator_type IN
                          ('project-orchestrator', 'gemini-orchestrator', 'bridge-scientist')),
    archived_at    TEXT,
    created_at     TEXT NOT NULL
                   DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (title),
    UNIQUE (project_id, partner_id_in_remote)
);
```

Three of those columns are names, and they do not overlap.

### `id` — the internal handle

An ordinary `INTEGER PRIMARY KEY`, so a rowid alias. **Every foreign key in the
schema that points at a partner points at `id`** — `handshakes.from_partner`,
`message_queue.caller_id`, `messages.to_partner`, all of them. It never appears in
any tool surface.

*Trace: on the diagram, count the edges converging on `PARTNERS` from below. Nine
of them, from six entities. Every one is an `id` reference.*

### `uuid` — identity, disclosed exactly once

```sql
uuid TEXT NOT NULL UNIQUE
```

This is what an agent proves it is with. Every capability takes `requester_uuid`
and **no capability anywhere takes a requester title** — a rule asserted by the
test suite, not merely intended.

The uuid is returned from `create_partner` and from nowhere else, ever. The
codebase calls that "the one sanctioned return of a uuid", and the MCP tool
description tells the caller to record it because it will not be shown again.

One correction to a common assumption: the uuid is not always *minted*.
`create_partner` accepts a caller-supplied `uuid` parameter and only generates one
when none is given. The diagram's gloss says this precisely — "minted at
create_partner, or supplied by the caller; returned exactly once".

Why identity and address must differ: a uuid is a credential. If agents addressed
each other by uuid, then discovering who exists would mean discovering how to
impersonate them. Because they address by title, `search_partner` can return
titles freely — and it is asserted that no capability leaks a foreign uuid in any
field of any response.

### `title` — the address, unique server-wide and permanently spent

```sql
UNIQUE (title)
```

Not `UNIQUE (project_id, title)`. **Server-wide.** And a title is spent
permanently: archiving does not free it, and an archived partner cannot be
renamed — enforced by a trigger, covered in note 4.

The reason is what a stale address does. Suppose titles were reusable. Agent A
holds "lit-review" as the address of a partner it has been working with. That
partner is archived; a new one takes the title. A now points at something that is
not what it thinks it is, and *nothing detects this* — the message is admitted,
delivered, and answered by a stranger.

The schema comment is blunt: *"the next partner to take that title would silently
inherit an address other agents may still be holding."*

`docs/05` states the cost honestly rather than pretending there isn't one: a
semantic title is spent after one use, so a long-lived deployment should plan for
a generation suffix. That is the correct trade — a burned name is recoverable, a
misdirected message is not.

### `partner_id_in_remote` — the remote's own handle, scoped to a project

```sql
UNIQUE (project_id, partner_id_in_remote)
```

*Trace: the diagram's gloss reads "UNIQUE within a project" — note it says
*within*, not *server-wide*, and that is the entire point.*

The schema carries twelve lines of reasoning here, and it is the sharpest argument
in the file:

> a `partner_id_in_remote` string is only meaningful WITHIN the remote app that
> issued it, and the Project is what identifies that remote app … Two different
> projects — even two different remotes, e.g. a `science_` frame id and a
> `gemini_` conversation id — can coincidentally share the same id string without
> naming the same remote object; a global UNIQUE would reject that as a collision
> when it is none.

Two remotes that have never heard of each other both issue an id like `abc123`.
Under a global unique constraint, whichever created its partner second is
rejected — for a conflict that does not exist. Scoping to the project makes the
constraint mean what it should: *one remote object maps to one partner*.

**And the enforcement pattern is deliberate.** `create_partner` does the insert
and catches the resulting `IntegrityError`, demultiplexing it into
`live_partner_limit`, `partner_id_in_remote_taken`, or `constraint_violation`
(`messaging_core/core.py:599-616`). The schema says why, and names the alternative
it is refusing:

> rather than a check-then-insert, which races.

It calls this *"the same shape as the queue cap"*. That is a doctrine running
through the whole codebase, and note 3 shows it in its purest form: **make the
constraint do the checking, inside one statement, and interpret the failure.**
There is still a pre-check in `create_partner`, but only as a fast path for a nicer
error message — the constraint is what is actually trusted.

### `descr` — a backstop under a word-count rule

```sql
descr TEXT NOT NULL CHECK (length(descr) <= 1200)
```

The intended rule is a word count; the database cannot express that, so it
enforces a character ceiling instead. A backstop, not the rule itself.

### `orchestrator_type` — a role, or none

```sql
orchestrator_type TEXT
    CHECK (orchestrator_type IN
           ('project-orchestrator', 'gemini-orchestrator', 'bridge-scientist'))
```

Nullable, and the `CHECK` only constrains non-NULL values — in SQLite,
`NULL IN (...)` is NULL, which passes. **A partner with no role is entirely
legal**, and is the common case: a plain worker holds none. Such a partner falls
to the `'*'` row in `agent_layers`, as note 1 covered.

All three roles are Claude Science roles. A `gemini_` or `code_` partner holds no
role and never will — a fact that forces real structure into the handshake rules,
because a rule about roles can never be satisfied by a participant that cannot
have one.

### `archived_at` — soft delete, and why it is a timestamp

```sql
archived_at TEXT
```

NULL means live. This is the column that makes archiving *reportable*, and the
difference between archiving and deletion is one of the most instructive
distinctions in the schema — note 4 takes it properly. For now: the row survives,
so the partner remains a valid foreign key target and a valid message *sender*,
which is precisely what lets the system tell everyone who was waiting.

---

## 3. The two partial indexes on `partners`

*Trace: nothing to trace — **indexes are drawn nowhere on the ER diagram.** This
is one of the three categories the diagram cannot show, and you need to know they
exist.*

```sql
CREATE INDEX partners_by_project ON partners(project_id) WHERE archived_at IS NULL;

CREATE UNIQUE INDEX one_orchestrator_per_project_role
    ON partners(project_id, orchestrator_type)
    WHERE orchestrator_type IS NOT NULL AND archived_at IS NULL;
```

Both are **partial** — they carry a `WHERE` clause, so they index a subset of
rows. That is not an optimisation detail here; in the second case it is the rule.

### `one_orchestrator_per_project_role` — a role is claimed once

A unique index over `(project_id, orchestrator_type)`. One project-orchestrator
per project, one gemini-orchestrator, one bridge-scientist.

Both predicates in the `WHERE` are load-bearing, for different reasons:

**`orchestrator_type IS NOT NULL`** keeps role-less partners out of the uniqueness
domain entirely. Without it, a project could contain at most one partner holding
no role — which is exactly backwards, since most partners hold none.

**`archived_at IS NULL`** is what makes archiving *release* the role. Archive the
project-orchestrator and the slot frees; a new partner may claim it. Without this
predicate a role would be spent forever, like a title.

And because it is a database-level unique index rather than an application check,
a concurrent double claim has exactly one winner. `claim_orchestrator` issues
`UPDATE … SET orchestrator_type = ? WHERE id = ? AND orchestrator_type IS NULL`
(`core.py:1022-1026`) and lets the index arbitrate. Two racing callers both issue
the update; one gets an `IntegrityError`.

### `partners_by_project` — and the cost of a partial index

The same `archived_at IS NULL` predicate, serving the common "live partners in
this project" scan.

Worth knowing the consequence: **SQLite only uses a partial index when the query's
`WHERE` provably implies the index predicate.** A query that wants archived
partners cannot use this index at all and falls back to a scan. That is the right
trade here — the live set is what the hot paths read — but it is the kind of thing
that surprises someone adding an admin view over archived rows later.

---

## 4. `handshakes` — permission to speak, one direction at a time

*Trace: the `HANDSHAKES` entity, lower left. Four columns. Two edges arrive from
`PARTNERS`: "opens (from_partner)" and "receives (to_partner)".*

```sql
CREATE TABLE handshakes (
    id             INTEGER PRIMARY KEY,
    from_partner   INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    to_partner     INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    created_at     TEXT NOT NULL
                   DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (from_partner, to_partner),
    CHECK (from_partner <> to_partner)
);
```

**Handshakes are one-way. A reply direction is a second row.** `UNIQUE
(from_partner, to_partner)` is on the *ordered* pair, so A→B and B→A are two
distinct legal rows. The one-way rule is not enforced by extra logic; it is what
a directed unique constraint means.

`CHECK (from_partner <> to_partner)` — no self-handshake. `handshake` also
refuses it in application code with `self_handshake` (`core.py:1253-1254`), so
this is one of several places where the same rule is stated twice, in the database
and in the tool, so that the refusal carries a readable message *and* cannot be
bypassed.

### How `send` reads it, and the asymmetry that matters

`send` looks for a handshake row in **both** directions (`core.py:2029-2037`): a
forward row (requester → target), and a reverse one (target → requester). Either
admits an ordinary message — which is what lets an answer, an error or a report
travel back along a relationship an orchestrator opened.

**But `[RESEARCH]` requires the forward row specifically.** Delegated work travels
along a handshake only in the direction the orchestrator claimed. Without this,
a plain worker could hand `[RESEARCH]` to its own project-orchestrator — they
share layer 2, so note 1's layer rule does not fire. The refusal is
`research_needs_a_forward_handshake`.

This is the second half of "delegated work never travels upward". The layer rule
handles strictly-higher targets; the forward-handshake rule handles peers.

### What the table does not have

There is **no index on `to_partner` alone.** The only index is the implicit one
from `UNIQUE (from_partner, to_partner)`, which serves lookups keyed on
`from_partner` or on the pair. "Who has handshaken *to* me" — which `status`
reports — is not index-served. At this scale that is fine; it is worth knowing
before someone adds a fan-in query to a hot path.

Rows are only ever removed by cascade. There is no `DELETE FROM handshakes` and no
`UPDATE` anywhere in the codebase: a handshake, once made, lasts as long as both
partners do.

---

## 5. `budget_grants` — metering, with both ends enforced

*Trace: the `BUDGET_GRANTS` entity. Look carefully at the two edges from
`PARTNERS`: one is `||--o|` ("is granted budget"), the other `||--o{` ("grants
budget"). That asymmetry in the crow's feet is a schema fact drawn visually.*

```sql
CREATE TABLE budget_grants (
    grantee_partner INTEGER PRIMARY KEY REFERENCES partners(id) ON DELETE CASCADE,
    granted_by      INTEGER NOT NULL REFERENCES partners(id),
    budget_count    INTEGER NOT NULL CHECK (budget_count BETWEEN 0 AND 3),
    granted_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
```

**`grantee_partner` is both the primary key and a foreign key.** That is why the
diagram draws `o|` on that side: a partner holds **at most one** budget grant,
ever, enforced by the PK rather than by a rule. `grant_gemini_budget` is
consequently an upsert — `INSERT … ON CONFLICT DO UPDATE` (`core.py:1087-1097`) —
refreshing `granted_by`, `budget_count` and `granted_at` rather than accumulating
rows.

`granted_by` has no such constraint, so one project-orchestrator may grant to many
grantees. Hence `o{`.

**A budget is granted BY a project-orchestrator TO a gemini-orchestrator**, and
both ends are checked by a trigger (note 4). Without that, a budget could be
granted by anyone to anyone — and the handshake rule that *spends* it would then
be metering a meaningless number.

`BETWEEN 0 AND 3` is inclusive. **Zero is a legal grant** — meaningfully different
from no grant at all. No row means "never granted"; a row of 0 means "granted, and
fully spent or deliberately withheld". `handshake` distinguishes them: no row is
`no_gemini_budget`, a spent one is `gemini_budget_exceeded`.

### The one foreign key that blocks instead of cascading

This is the detail most worth carrying away from this note.

`grantee_partner` is `ON DELETE CASCADE`. **`granted_by` has no `ON DELETE`
clause at all** — so it defaults to `NO ACTION`. Of the twelve foreign keys in the
schema that reference `partners(id)`, eleven cascade and this one does not.

The consequence: **deleting a project-orchestrator that has issued a budget grant
fails.** The `IntegrityError` surfaces as `partner_has_dependents`
(`core.py:890-896`), and the same thing happens one level up in `delete_project`
(`core.py:956-967`), where the cascade "stops on a foreign key rather than a
rule".

Whether that asymmetry is intentional design or an oversight, the schema does not
say. What it produces is defensible: you cannot quietly delete the authority
behind an outstanding grant. But it is worth knowing that this particular refusal
comes from a missing clause rather than from a stated rule.

---

## 6. `partner_paths` — recorded intention, not observation

*Trace: the `PARTNER_PATHS` entity. Three columns, all three marked `PK`. The
edge from `PARTNERS` reads "intends to grant (partner_id); add_ and
delete_permissions only" — the diagram states the writer restriction on the
relationship itself.*

```sql
CREATE TABLE partner_paths (
    partner_id  INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('read', 'write')),
    path        TEXT NOT NULL,
    PRIMARY KEY (partner_id, kind, path)
) WITHOUT ROWID;
```

A three-column composite primary key and no surrogate id — so the row *is* its
key, and `WITHOUT ROWID` is the natural shape. The same path may be granted both
`read` and `write` as two separate rows.

**The important idea here is epistemic rather than structural.** This table holds
the grant a partner is *meant* to have. `get_permissions` reports what the remote
*actually* has. They are allowed to differ, and the schema says why that is a
feature:

> the difference is exactly what a Caller needs to see in order to correct it.

A table that claimed to mirror the remote would be lying the moment someone edited
permissions through Antigravity's own UI. A table that records intention is
always true about intention.

`add_permissions` and `delete_permissions` are the only writers, and they write
**only after the remote has been seen to hold the change** (`core.py:1794-1798`,
`core.py:1862-1865`). The record follows reality rather than predicting it.

And it is **deliberately not touched by `send`.** A permission prompt means the
grant was missing *before* the work started, so configuring paths as a side effect
of sending work would always be one step too late. That is the schema encoding the
approval doctrine: an approval prompt is an error, never a question.

One asymmetry in the writers: `delete_permissions` deletes by `(partner_id, path)`
— **both kinds** — while `add_permissions` inserts a specific `kind`. Revoking a
path revokes it entirely.

---

## 7. `project_extension` — symmetric by construction

*Trace: the `PROJECT_EXTENSION` entity, sitting between two edges from `PROJECTS`
labelled "extends (project_a)" and "extended by (project_b)". Its two column
glosses tell the whole story: "CHECK project_a < project_b" and "so the pair has
ONE row".*

```sql
CREATE TABLE project_extension (
    project_a   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    project_b   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (project_a, project_b),
    CHECK (project_a < project_b)
) WITHOUT ROWID;
```

Two projects declared parts of one effort, so partners under them may handshake
across the project boundary.

**Why the table exists at all** is a design decision worth stating: a single
project cannot hold enough live partners to run research at scale — ten, from
`source_caps` — and that ceiling exists for a reason. So the answer is *more
projects, explicitly linked*, rather than a larger ceiling.

**`CHECK (project_a < project_b)` does two jobs with one clause.** It canonicalises
the pair, so "is A an extension of B" has exactly one row to look at and cannot
answer differently depending on which way it is asked. And because it is strict
`<`, it simultaneously rules out a project being an extension of itself.

`extend_project` sorts the pair before inserting (`core.py:1642`) and uses
`INSERT OR IGNORE`, distinguishing *created* from *already_linked* by whether
`rowcount` is 1 (`core.py:1645-1648`). Same doctrine as everywhere else: let the
constraint decide, then interpret.

**What an extension does and does not grant.** It permits messaging across the
boundary. It grants nothing administratively — no permissions move, no queued work
moves. And a same-source pair across an extension must hold the **same role**:
the point is a research effort branching sideways, not a second chain of command,
so a gemini-orchestrator cannot inherit a superior from another project.

The exception, because a role rule cannot bind participants that hold no role: a
`gemini_` → `gemini_` pair — one Antigravity conversation inheriting from another
— is decided *before* the orchestrator gate and requires no role match at all. It
requires an extension row, refuses a same-project pair outright, and allows a
predecessor to be inherited from only once. `07-handshake-legality` draws that
branch.

---

## 8. Reading the graph as a whole

*Trace: step back from the page. Almost every edge in the lower two-thirds
originates at `PARTNERS`.*

`PARTNERS` is the hub. Six entities reference it, across nine foreign keys —
`handshakes` twice, `messages` twice, `message_queue` twice, plus
`partner_paths`, `budget_grants` (twice, one of which does not cascade), and
`drain_threads`.

That shape has a direct operational consequence, which note 4 develops: deleting
one partner cascades into seven tables. Deleting a *project* cascades into
`partners` first and then into all of those. It is why deletion is guarded so
heavily, and why archiving exists as the intended move.

The three names map cleanly onto three scopes:

| Name | Scope | Lifetime | Who sees it |
|---|---|---|---|
| `id` | the database | the row | nothing outside the schema |
| `uuid` | server-wide | the partner | only its own holder |
| `title` | server-wide | **permanent, even after archiving** | every agent |
| `partner_id_in_remote` | one project | the row | the adapter |

---

## 9. Questions a new engineer asks

**"Why can't I look a partner up by title and uuid together?"** Because that would
make the uuid a thing you can *check* rather than a thing you *hold*, and a
checkable credential leaks by oracle. `_resolve_live_partner_by_title` resolves a
title to a live partner identity and is the only place that happens. Other title
lookups exist — duplicate checks, archiving, search — but none of them yields an
identity.

**"What stops me messaging a partner in someone else's project?"** Nothing
directly — the rule is carried by `handshakes`. A cross-project pair needs either
a cross-source relationship (which is cross-project by construction) or a
`project_extension` row. Without one, `send` refuses `no_handshake`.

**"Is `archived_at` ever set back to NULL?"** No capability does it. Note that
`partners_live_limit` fires on `BEFORE INSERT` only, so a hand-written
un-archiving `UPDATE` would **not** be gated by the live-partner ceiling — a
project could end up over its limit. That is a real gap in the enforcement, and
worth knowing before anyone adds an un-archive feature.

**"Why does `budget_grants` use an upsert instead of allowing several rows?"**
Because `grantee_partner` is the primary key, so several rows are impossible by
construction. The upsert is not a convenience; it is the only shape that table
permits.

**"Are `WITHOUT ROWID` and the partial indexes performance tuning?"** The partial
index on roles is a *rule*, not tuning — its `WHERE` clause is what makes archiving
free a role. `WITHOUT ROWID` genuinely is a storage choice, applied to the four
tables whose identity is a natural composite key.

---

*Next: the queue. `message_queue` column by column, the two statements that decide
what runs, the cap that is half SQL and half memory, and the five different writers
that put rows in this table for three different reasons.*
