-- Messaging MCP Server + Polling Server, relational schema.
-- Read "Schema notes" for the reasoning behind every non-obvious choice here,
-- and "SQLite as a lightweight queue" for the concurrency rules this assumes.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

-- Per-source facts. A new source is a row, not a code change.
--
-- There is deliberately no max_queue here any more: a queue limit is not a property of the
-- source. It is a property of (caller, label) -- see label_caps -- because the thing worth
-- limiting is how much of one KIND of work one caller may have outstanding against one
-- partner, not how many messages a partner can hold in total.
CREATE TABLE source_caps (
    source_prefix   TEXT PRIMARY KEY
                    CHECK (source_prefix IN ('nlm_', 'code_', 'science_', 'gemini_')),
    -- How many live (non-archived) partners one project may hold. This is the "limit of
    -- working sessions" -- the reason archive_sessions exists at all. Data, not a constant,
    -- for the same reason max_queue is.
    max_live_partners INTEGER NOT NULL DEFAULT 10 CHECK (max_live_partners > 0),
    can_execute     INTEGER NOT NULL DEFAULT 1 CHECK (can_execute IN (0, 1)),
    needs_handshake INTEGER NOT NULL DEFAULT 1 CHECK (needs_handshake IN (0, 1)),
    -- Whether a partner of this source may originate a message at all. NotebookLM cannot:
    -- it is a knowledge base with no agent behind it, so nothing there ever decides to
    -- speak. A partner that cannot send is reachable but never a caller.
    can_send        INTEGER NOT NULL DEFAULT 1 CHECK (can_send IN (0, 1)),
    -- Whether a partner of this source may RECEIVE delegated work. NotebookLM cannot:
    -- [RESEARCH] asks the recipient to go and do something, and NotebookLM only answers
    -- questions about what it already holds. It receives [QUERY] and nothing else that
    -- implies action.
    accepts_research INTEGER NOT NULL DEFAULT 1 CHECK (accepts_research IN (0, 1))
);

INSERT INTO source_caps
    (source_prefix, max_live_partners, can_execute, needs_handshake, can_send, accepts_research)
VALUES
    ('nlm_',     10, 0, 0, 0, 0),  -- context only: never executes, never sends, answers queries
    ('code_',    10, 1, 1, 1, 1),
    ('science_', 10, 1, 1, 1, 1),
    ('gemini_',  10, 1, 1, 1, 1);

-- Where each kind of agent sits in the delegation hierarchy:
--
--     NotebookLM, Claude code  >  project-orchestrator  >  gemini-orchestrator  >  Antigravity
--
-- A lower number is higher up. The one rule this exists to enforce: **[RESEARCH] only ever
-- travels down or sideways.** Delegated work flows away from whoever is directing it; a
-- lower agent handing [RESEARCH] back up would be reassigning its own director's work.
-- Every other label travels freely in both directions, which is what makes an answer, an
-- error, or a report able to come back.
--
-- Sideways is deliberately allowed: two partners at the same layer in extended Projects are
-- branches of one research effort (see project_extension), not a chain of command.
--
-- '*' is the source's default, used when no row names the partner's orchestrator_type --
-- including a partner holding no role at all. A more specific row always wins, so a
-- bridge-scientist is placed above the plain science_ default rather than beside it: it
-- takes direction from Claude code and passes it to the project-orchestrator.
CREATE TABLE agent_layers (
    source_prefix     TEXT NOT NULL REFERENCES source_caps(source_prefix),
    orchestrator_type TEXT NOT NULL,
    layer             INTEGER NOT NULL CHECK (layer >= 0),
    PRIMARY KEY (source_prefix, orchestrator_type)
) WITHOUT ROWID;

INSERT INTO agent_layers (source_prefix, orchestrator_type, layer) VALUES
    ('nlm_',     '*',                    0),
    ('code_',    '*',                    0),
    ('science_', 'bridge-scientist',     1),
    ('science_', 'project-orchestrator', 2),
    ('science_', 'gemini-orchestrator',  3),
    ('science_', '*',                    2),
    ('gemini_',  '*',                    4);

-- The five message labels, their relative priority, and how many of each one caller may
-- have outstanding against one partner.
--
-- Priority is what decides which task holds the working slot: a lower number wins.
--
-- [TRUTHFUL-REPORT] is highest, so a summarization completes without other traffic
-- contaminating the context it is summarizing. It is only ever produced after a [RESEARCH]
-- has been drained, so nothing is starved by it sitting at the top.
--
-- [QUERY] and [ERROR] come next, sharing a rank: both are issues that stop work, and
-- neither is more urgent than the other. That rank is also the whole interruption
-- mechanism. An agent that SENDS one is stopped, its work pushed back paused, and the
-- question it asked takes its own working slot -- and nothing below that rank can reach it
-- while it waits. There is no separate hold label; the question IS the hold. Only a
-- summary outranks a waiting agent, which is the one interruption worth allowing.
--
-- max_outstanding NULL means uncapped. A cap counts the working task as well as queued
-- ones, so it limits work in flight, not merely work waiting.
--
-- reply_behavior is what a Partner sends back when a task carrying this label finishes,
-- and NULL is the important value in the column: it is what makes the exchange terminate.
-- Three labels expect an answer -- [RESEARCH] is answered with a summary, and [QUERY] and
-- [ERROR] are each answered with a [MESSAGE-RESPONSE], since an agent that asked otherwise
-- has no way to know the answer landed. The other two ARE answers. Without a label whose
-- reply is nothing, every completed task would produce a message that produced a task that
-- produced a message, and two agents would talk to each other until one was archived.
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

INSERT INTO label_caps (behavior, priority, max_outstanding, stored, reply_behavior) VALUES
    ('[TRUTHFUL-REPORT]',  1, NULL, 1, NULL),
    ('[QUERY]',            2,    3, 1, '[MESSAGE-RESPONSE]'),
    ('[ERROR]',            2, NULL, 0, '[MESSAGE-RESPONSE]'),
    ('[MESSAGE-RESPONSE]', 3, NULL, 1, NULL),
    ('[RESEARCH]',         4,    2, 0, '[TRUTHFUL-REPORT]');

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
    -- Server-wide, not per-project, and archived titles stay spent.
    UNIQUE (title),
    -- Scoped to (project_id, partner_id_in_remote), not global: one remote object
    -- maps to one Partner, but a partner_id_in_remote string is only meaningful
    -- WITHIN the remote app that issued it, and the Project is what identifies
    -- that remote app (see verify_partner_id_in_remote, which is always called
    -- with both project_system_id and partner_id_in_remote together -- never the
    -- id alone). Two different projects -- even two different remotes, e.g. a
    -- science_ frame id and a gemini_ conversation id -- can coincidentally share
    -- the same id string without naming the same remote object; a global UNIQUE
    -- would reject that as a collision when it is none. create_partner enforces
    -- this with a single INSERT and catches the resulting IntegrityError -- the
    -- same shape as the queue cap -- rather than a check-then-insert, which races.
    UNIQUE (project_id, partner_id_in_remote)
);

CREATE INDEX partners_by_project ON partners(project_id) WHERE archived_at IS NULL;

-- One role per project, claimed once. Partial index so archived holders free the slot.
CREATE UNIQUE INDEX one_orchestrator_per_project_role
    ON partners(project_id, orchestrator_type)
    WHERE orchestrator_type IS NOT NULL AND archived_at IS NULL;

-- The live-partner ceiling. A CHECK cannot count rows, so this is a trigger -- but it is
-- still enforced by the database rather than left to whichever caller remembers. Archiving
-- is what frees a slot; deleting does too, but archiving is the intended move.
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

-- Archiving spends a title permanently. Renaming an archived partner would free it, and
-- the next partner to take that title would silently inherit an address other agents may
-- still be holding. The refusal belongs here and not only in the tool: a rule that lives
-- solely in application code is bypassed by any path that issues the UPDATE directly.
CREATE TRIGGER partners_no_rename_archived
BEFORE UPDATE OF title ON partners
WHEN OLD.archived_at IS NOT NULL AND NEW.title <> OLD.title
BEGIN
    SELECT RAISE(ABORT, 'an archived partner cannot be renamed; its title stays spent');
END;

-- Handshakes are one-way. A reply direction is a second row.
CREATE TABLE handshakes (
    id             INTEGER PRIMARY KEY,
    from_partner   INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    to_partner     INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    created_at     TEXT NOT NULL
                   DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (from_partner, to_partner),
    CHECK (from_partner <> to_partner)
);

-- The read/write grant a gemini_ partner is MEANT to hold. add_permissions and
-- delete_permissions are the only writers, and each one also pushes the change into the
-- conversation itself -- so this table is the intended set and get_permissions reports the
-- actual one. They are allowed to differ; the difference is exactly what a Caller needs to
-- see in order to correct it.
--
-- Deliberately not touched by send. A permission prompt means the grant was missing BEFORE
-- the work started, so configuring paths as a side effect of sending work would always be
-- one step too late. Read "Antigravity state handling".
CREATE TABLE partner_paths (
    partner_id  INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('read', 'write')),
    path        TEXT NOT NULL,
    PRIMARY KEY (partner_id, kind, path)
) WITHOUT ROWID;

CREATE TABLE budget_grants (
    grantee_partner INTEGER PRIMARY KEY REFERENCES partners(id) ON DELETE CASCADE,
    granted_by      INTEGER NOT NULL REFERENCES partners(id),
    budget_count    INTEGER NOT NULL CHECK (budget_count BETWEEN 0 AND 3),
    granted_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE messages (
    id            INTEGER PRIMARY KEY,
    from_partner  INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    to_partner    INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    -- Only the labels marked `stored` in label_caps are ever written here. [RESEARCH]
    -- and [ERROR] are transport: they travel in a queue, are acted on, and are never
    -- written down. The rule is enforced by the trigger below rather than by a CHECK
    -- listing the labels, because a list here is a second copy of label_caps.stored and two
    -- copies of one fact eventually disagree. Storage follows label_caps.stored alone -- the
    -- three stored labels are stored no matter which remote sends or receives them, an nlm_
    -- Partner included, because label_caps.stored is deliberately the one place this is
    -- decided.
    behavior      TEXT NOT NULL REFERENCES label_caps(behavior),
    body          TEXT NOT NULL,
    created_at    TEXT NOT NULL
                  DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX messages_readable ON messages(to_partner, id DESC);

-- The `stored` half of label_caps, enforced. A CHECK cannot reach another table, so this
-- is a trigger -- but it keeps the rule in the database, where a second code path writing
-- to `messages` directly still meets it.
CREATE TRIGGER messages_stored_labels_only
BEFORE INSERT ON messages
BEGIN
    SELECT RAISE(ABORT, 'this behavior is transport-only and is never stored')
     WHERE (SELECT stored FROM label_caps WHERE behavior = NEW.behavior) IS NOT 1;
END;

-- ONE queue, ordered by priority. Every message is a push; there is no separate reply
-- channel, so there is no routing decision to get wrong.
--
-- A row here is a QUEUED poll task. The task actually being worked -- the "working slot" --
-- is held in memory by the Polling Server and is deliberately NOT in this table: it is
-- process state, it changes on every swap, and persisting it would invite a reader to
-- believe it survives a restart when it does not.
--
-- in_process marks a task displaced from the working slot by a higher-priority arrival. It
-- is paused, not new, and it outranks other queued tasks carrying THE SAME label ONLY.
--
-- The scoping is load-bearing and cost a real bug to get right. [QUERY] and [ERROR] share a
-- priority deliberately, so a paused task that outranked fresh work at equal priority would
-- beat a different label: a partner interrupted mid [QUERY] would be handed "resume your
-- previous [QUERY]" instead of the [ERROR] its Caller just sent explaining what went wrong.
-- The correction would never be seen. See _HEAD_LABEL_SQL / _HEAD_ROW_SQL in core.py -- the
-- rule needs two statements because one ORDER BY cannot say "within a label".
--
-- Within a label it is what makes the resume prompt able to be one line: there is never more
-- than one paused candidate, so "resume your previous [RESEARCH]" has exactly one referent.
--
-- The body lives here rather than in `messages`: only the labels marked `stored` in
-- label_caps are ever written there.
CREATE TABLE message_queue (
    id           INTEGER PRIMARY KEY,
    partner_id   INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    caller_id    INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    behavior     TEXT NOT NULL REFERENCES label_caps(behavior),
    body         TEXT NOT NULL,
    in_process   INTEGER NOT NULL DEFAULT 0 CHECK (in_process IN (0, 1)),
    message_id   INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    -- Set when this row is a displaced [RESEARCH] summary phase: the task was
    -- relabelled [TRUTHFUL-REPORT] by begin_summary_phase and still owes its
    -- Caller the report. The label alone cannot say it -- a [TRUTHFUL-REPORT]
    -- can equally be one an agent sent directly, and that one owes nothing
    -- back, because it already IS the report.
    summary_phase   INTEGER NOT NULL DEFAULT 0 CHECK (summary_phase IN (0, 1)),
    -- The label this task was admitted under, when it differs from `behavior`.
    -- A summary phase runs at [TRUTHFUL-REPORT]'s priority but still counts
    -- against its Caller's [RESEARCH] cap, because it is the same delegated
    -- work under a second instruction.
    origin_behavior TEXT REFERENCES label_caps(behavior),
    -- Set when this row is an agent's own unanswered question, displaced out of
    -- its working slot. The question is not work: nothing is delivered for it,
    -- and it returns to the slot to go on waiting rather than to be run. Only a
    -- [TRUTHFUL-REPORT] outranks a waiting agent, so this is reachable exactly
    -- when a summary interrupts one -- and without the marker the question
    -- would come back looking like an ordinary [QUERY] and be handed to the
    -- agent as work it has already asked.
    awaiting_resolution INTEGER NOT NULL DEFAULT 0 CHECK (awaiting_resolution IN (0, 1)),
    -- When the message entered the queue. The other half of the latency measurement --
    -- when it actually started running -- is deliberately NOT a column here: a promoted row
    -- is DELETED, so a `dequeued_at` would only ever be written to a row about to
    -- disappear. Start time lives on the in-memory working slot beside the task it
    -- describes, and `status` reports it. A column with no surviving writer is worse than
    -- no column, because it reads like a measurement someone is taking.
    enqueued_at  TEXT NOT NULL
                 DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (caller_id <> partner_id)
);

-- The pop order is two questions, and this index serves both: which LABEL runs next
-- (priority, then whether the label has any unpaused work, then arrival), and which ROW of
-- that label (paused first, then arrival). Priority lives in label_caps and reaches the
-- query through a join, so it cannot be in this index; grouping by partner and behavior,
-- with in_process and enqueued_at ordered within, is exactly what both statements scan.
CREATE INDEX message_queue_order
    ON message_queue(partner_id, behavior, in_process DESC, enqueued_at);



-- The drain-thread registry that replaces the `.dbm` file.
CREATE TABLE drain_threads (
    partner_id  INTEGER PRIMARY KEY REFERENCES partners(id) ON DELETE CASCADE,
    thread_id   TEXT NOT NULL,
    started_at  TEXT NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) WITHOUT ROWID;

-- Two Projects may be declared extensions of one another so that Partners under them can
-- handshake across the Project boundary. A single Project cannot hold enough live Partners
-- to run research at scale, and the ceiling exists for a reason -- so the answer is more
-- Projects, explicitly linked, rather than a larger ceiling.
--
-- Symmetric by construction: the pair is stored with the lower id first and a CHECK
-- enforces it, so "is A an extension of B" has exactly one row to look at and cannot
-- disagree with itself depending on which way it is asked.
CREATE TABLE project_extension (
    project_a   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    project_b   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (project_a, project_b),
    CHECK (project_a < project_b)
) WITHOUT ROWID;

-- A gemini budget is granted BY a project-orchestrator TO a gemini-orchestrator. Neither
-- end was constrained before, so a budget could be granted by anyone to anyone -- and the
-- handshake rule that spends it would then be metering a meaningless number. A CHECK cannot
-- reach another table, so both ends are triggers.
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

-- A read/write path grant only means anything for a remote that executes against a
-- filesystem, which here is Antigravity. A row for any other source is a grant that nothing
-- will ever apply -- indistinguishable, to a reader, from one that is being enforced.
CREATE TRIGGER partner_paths_gemini_only
BEFORE INSERT ON partner_paths
BEGIN
    SELECT RAISE(ABORT, 'partner_paths applies only to a gemini_ partner')
     WHERE (SELECT pr.source_prefix FROM partners p
              JOIN projects pr ON pr.id = p.project_id
             WHERE p.id = NEW.partner_id) IS NOT 'gemini_';
END;
