"""The messaging core: the capabilities that sit on top of `schema/schema.sql`.

`MessagingCore` is the only place business rules live. Everything that touches
the database goes through `Database.read`/`.read_one` (this thread) or
`Database.write(fn)` (the single writer thread, inside `BEGIN IMMEDIATE`).
Everything that touches a remote system goes through the one optional
`RemoteExtension` this core was constructed with.

One modelling decision this module makes is worth calling out up front
because the rest of the file depends on it:

**A Partner has no source of its own -- it takes the source of its
Project.** `projects.source_prefix` is the single authority, and a Project
has exactly one. Two Partners in the same Project therefore always share a
source: `_partner_type` simply joins `partners` -> `projects` and returns
`projects.source_prefix`. Titles are free-form, human-readable addresses
(the string a caller reaches a partner by) and carry no type information at
all.

A consequence of this that keeps the handshake rules consistent: because a
Project has exactly one source, a cross-source handshake ("science_" ->
"gemini_") is necessarily cross-Project. The "both agents must be under the
same Project" rule therefore only applies *within* a source -- two
`science_` partners handshaking must share a Project; a `science_` partner
reaching a `gemini_` partner never can, and that is correct rather than a
gap to close.

Remote calls follow the same authority: `RemoteExtension` instances carry a
fixed `source_prefix` (see `extension/base.py`); a Partner is verified,
delivered to, stopped, or reconfigured through whichever extension matches
*its Project's* `source_prefix`, because that is the remote system that
actually manages sessions/sub-agents under that project's
`project_system_id`. A single `MessagingCore` holds at most one extension at a
time; a capability that needs a different `source_prefix` than the one
currently configured raises `NeedsRemote` exactly as if no extension were
configured at all -- never a fabricated answer from the wrong remote.

The second decision worth stating up front is the queue.

**There is one queue per Partner, ordered by priority, and every message is a
push into it.** There is no reply channel and no second queue, so there is no
routing decision to get wrong. A label does not say which direction a message
travels -- any label travels either way -- it says how urgent the message is
relative to whatever the Partner is doing, and `label_caps.priority` is the
authority for that.

The task a Partner is actually working sits in an in-memory working slot
(`messaging_core/slots.py`), never in SQLite. `advance` is the single place
where the queue head is compared against that slot, the slot is swapped if the
head wins, and the winning task is handed to the remote. `send`,
`interrupt_partner`, and the Polling Server's drain thread all call that one
method rather than each carrying their own copy of the rule.
"""

from __future__ import annotations

import datetime as _dt
import difflib
import logging
import sqlite3
import uuid as uuid_lib
from typing import Any

from extension.base import RemoteExtension, RemoteFailure
from messaging_core import templates
from messaging_core.db import Database
from messaging_core.errors import NeedsRemote, Rejected
from messaging_core.labels import BEHAVIORS, INTERRUPT_BEHAVIOR
from messaging_core.slots import WorkingSlots

# No `logging.basicConfig` here or anywhere else in this module -- that
# configures the ROOT logger, and a library that does that has taken a
# decision away from whoever embeds it. A logger under this module's own
# name is all a library ever gets to assume.
logger = logging.getLogger(__name__)

# The four recognized source prefixes. A Partner has no type of its own --
# see the module docstring and `MessagingCore._partner_type` -- so this
# tuple exists only to validate the `source_prefix` argument to
# `create_project` against `source_caps`.
_PREFIXES: tuple[str, ...] = ("nlm_", "code_", "science_", "gemini_")

#: Labels that may never travel through `report_back`. `[RESEARCH]` is
#: delegation -- letting it through would bypass the hierarchy rule `send`
#: enforces. `[IDLE]` is a hold rather than a message and has no meaning in a
#: Caller's queue.
#: The two labels a Partner raises when it cannot finish without its Caller --
#: a question about what was meant, or a statement that something blocked it.
#: Sent UPWARD (see `travelling_up` in `send`) they park the sender, because an
#: agent waiting on an answer must not also be receiving new work.
_RAISES_UPWARD: tuple[str, ...] = ("[QUERY]", "[ERROR]")

_NOT_REPORTABLE: tuple[str, ...] = ("[RESEARCH]", INTERRUPT_BEHAVIOR)

#: Rejection codes meaning "this remote has no cancel", as opposed to "the
#: cancel failed". The first is a fact about the remote and must not stop a
#: displacement; the second is an error and must.
_UNCANCELLABLE: frozenset[str] = frozenset({"no_remote_cancel", "not_executable"})

_ORCHESTRATOR_TYPES: tuple[str, ...] = (
    "project-orchestrator",
    "gemini-orchestrator",
    "bridge-scientist",
)

_DESCR_MAX_LEN = 1200
_DESCR_PREVIEW_LEN = 160

# Admission, in ONE statement. Being over cap must show up as rowcount 0, never
# as a separate read followed by a write -- two concurrent callers can both pass
# a read-then-write check and both insert, which is precisely how a cap of three
# admits four.
#
# The cap is keyed (caller, label) within one Partner's queue, and `:working`
# carries the working slot into the count: a caller allowed three [QUERY] tasks
# has three in flight, not three waiting plus one running. A NULL
# max_outstanding means the label is uncapped and the first branch admits
# unconditionally.
_ADMIT_SQL = """
INSERT INTO message_queue (partner_id, caller_id, behavior, body, message_id)
SELECT :pid, :cid, :behavior, :body, :mid
 WHERE (SELECT max_outstanding FROM label_caps WHERE behavior = :behavior) IS NULL
    OR (SELECT COUNT(*) FROM message_queue
         WHERE partner_id = :pid AND caller_id = :cid
           AND COALESCE(origin_behavior, behavior) = :behavior) + :working
     < (SELECT max_outstanding FROM label_caps WHERE behavior = :behavior)
"""

# The pop order, and the only definition of it. Two steps, because the rule
# genuinely needs two: priority decides between labels, and `in_process`
# decides only WITHIN one label.
#
# A single ORDER BY cannot express that. `in_process DESC` after priority makes
# a paused task outrank a fresh one of a DIFFERENT label at the same priority --
# and `[QUERY]` and `[ERROR]` share priority 2 deliberately. The concrete
# failure that produced these two statements: a Partner is interrupted mid
# `[QUERY]`, the Caller sends the `[ERROR]` explaining what went wrong, and the
# Partner is handed "resume your previous [QUERY]" instead. The correction is
# never seen, which is exactly the flow the approval doctrine depends on.
#
# Step 1 picks the winning LABEL. `has_fresh` is MIN(in_process) over the
# label's rows: 0 when the label has any unstarted work, 1 when every row of it
# is paused. So at equal priority a label with something new to say outranks
# one that is only waiting to be resumed.
#
# That is not quite enough once a label holds BOTH a paused row and a fresh
# one. MIN(in_process) is then 0 -- tied with a label that has only fresh rows
# -- and the next key, MIN(enqueued_at), falls back to the OLDEST row in the
# label, which is exactly the paused one: pausing is what happens to the thing
# that has been waiting longest. A fresh `[ERROR]` then loses the label to a
# `[QUERY]` that is part paused, part fresh, arriving via the tie-break itself
# -- the same failure this two-statement design exists to prevent, showing up
# one level in.
#
# So a key sits between the two: the earliest arrival among only the label's
# FRESH rows (`in_process = 0`), via a CASE that maps a paused row to NULL so
# it cannot supply the label's timestamp. No NULLS LAST is needed, and none
# should be added -- the preceding MIN(q.in_process) ASC key already separates
# a label with at least one fresh row from one with none, so this new key is
# only ever compared between two labels that both have fresh rows (both
# non-NULL) or both have none (both NULL); the mixed case a NULLS LAST choice
# would matter for never reaches this key. MIN(q.enqueued_at) ASC stays last,
# unchanged, to break a tie between two labels' fresh rows (or the absence of
# any) that lands exactly even.
_HEAD_LABEL_SQL = """
SELECT q.behavior AS behavior
  FROM message_queue q JOIN label_caps c ON c.behavior = q.behavior
 WHERE q.partner_id = :pid
 GROUP BY q.behavior
 ORDER BY MIN(c.priority) ASC, MIN(q.in_process) ASC,
          MIN(CASE WHEN q.in_process = 0 THEN q.enqueued_at END) ASC,
          MIN(q.enqueued_at) ASC
 LIMIT 1
"""

# Step 2 picks the row within that label, and here paused DOES win -- a Partner
# finishes what it started before starting anything else of the same kind.
# At most one row per label is ever paused in practice, which is what lets the
# resume prompt be a single line: "resume your previous [RESEARCH]" has exactly
# one referent.
_HEAD_ROW_SQL = """
SELECT q.id AS id, q.partner_id AS partner_id, q.caller_id AS caller_id,
       q.behavior AS behavior, q.body AS body, q.in_process AS in_process,
       q.message_id AS message_id, q.enqueued_at AS enqueued_at,
       q.summary_phase AS summary_phase, q.origin_behavior AS origin_behavior,
       c.priority AS priority
  FROM message_queue q JOIN label_caps c ON c.behavior = q.behavior
 WHERE q.partner_id = :pid AND q.behavior = :behavior
 ORDER BY q.in_process DESC, q.enqueued_at ASC, q.id ASC
 LIMIT 1
"""

# Where an agent sits in the delegation hierarchy. A row naming the partner's
# own orchestrator_type wins over the source's '*' default -- the CASE is what
# orders them, since '*' sorts before every real role name alphabetically and
# would otherwise win by accident.
_LAYER_SQL = """
SELECT layer FROM agent_layers
 WHERE source_prefix = :src AND orchestrator_type IN (:role, '*')
 ORDER BY CASE orchestrator_type WHEN '*' THEN 1 ELSE 0 END
 LIMIT 1
"""


def _now() -> str:
    """The same UTC timestamp shape the schema's own DEFAULTs produce.

    Matching the shape matters: `enqueued_at` comes from SQLite and
    `started_at` comes from here, and the two are subtracted to measure how
    long a task waited. Two formats would make that subtraction a parsing
    problem discovered at the worst moment.
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    return f"{now:%Y-%m-%dT%H:%M:%S}.{now.microsecond // 1000:03d}Z"


#: The exact shape `_now()` produces, and the shape SQLite's own
#: `strftime('%Y-%m-%dT%H:%M:%fZ', 'now')` DEFAULTs produce too -- see
#: `_now()`'s own docstring for why the two are made to match.
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _parse_ts(value: str | None) -> _dt.datetime | None:
    """Parse a timestamp in the one shape this module ever writes.

    Total, deliberately: `None`, an empty string, or a value that simply
    isn't that shape all come back as `None` rather than raising. This is
    what lets a wait be computed from two timestamps that are supposed to
    agree without a malformed or missing one taking down whatever asked for
    the wait -- `status` is a diagnostic, not the thing that should be
    breaking.
    """
    if not value:
        return None
    try:
        return _dt.datetime.strptime(value, _TS_FORMAT).replace(tzinfo=_dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def _score(query: str, candidate: str) -> float:
    """difflib ratio between `query` and `candidate`, case-insensitive."""
    return difflib.SequenceMatcher(None, (query or "").lower(), (candidate or "").lower()).ratio()


# The fuzzy half of relevance: below this, a ratio is coincidence, not a
# candidate. difflib's own `get_close_matches` treats 0.6 as its default
# "close enough" cutoff, and this is where it earns that trust -- as TYPO
# tolerance, not as the whole test (see `_is_relevant`). "reserch" against
# "research-worker" scores 0.636 and needs to survive; that is the shape of
# match 0.6 is actually good at recognizing.
_RELEVANCE_FLOOR = 0.6

#: Shortest query `_is_relevant`'s substring rule will accept on its own.
#: Below this a match is too likely by chance to count as "deliberate" --
#: a single letter matches nearly everything.
_MIN_SUBSTRING_LEN = 3


def _is_relevant(query_title: str, candidate_title: str, score: float) -> bool:
    """Whether a candidate clears the bar `search_partner`/`search_project` enforce.

    A fuzzy floor alone cannot do this job. `difflib.ratio` penalises length
    difference, so a short, deliberate query against a long title scores low
    no matter how exact the match is: "gemini" against
    "gemini-orchestrator" scores 0.48, and "orch" against that same title
    scores 0.35 -- both well under `_RELEVANCE_FLOOR`, and neither a query
    anybody would call a coincidence.

    Lowering the floor to catch them does not work either, and this is the
    part that rules out a one-rule design rather than just complicating it:
    "xylophone" -- a word sharing nothing meaningful with
    "photosynthesis-study" -- scores 0.345 against it, which is HIGHER than
    "res" scores against its own exact, intended target, "research-worker"
    (0.333), and about equal to "orch" against "gemini-orchestrator"
    (0.348). There is no cutoff that keeps "res" and "orch" while dropping
    "xylophone": ratio alone does not separate a short, exact prefix from an
    unrelated word of similar length.

    What does separate them is that "gemini", "orch", and every other short
    query above is a literal substring of its target, and "xylophone" is a
    substring of nothing here. So a candidate qualifies if EITHER rule
    fires:

    1. A literal, case-insensitive substring match at least
       `_MIN_SUBSTRING_LEN` characters long. A deliberate substring is a
       deliberate search, not a coincidence, whatever it does to the ratio.
    2. A fuzzy ratio at or above `_RELEVANCE_FLOOR`, for the near-misses a
       substring test cannot catch -- a typo, a transposed letter.

    Sorting is untouched by this: candidates are still ranked by `score`
    alone, descending, so an exact match still outranks a bare substring
    hit. This only changes which candidates are eligible to be ranked at
    all.
    """
    query = (query_title or "").strip().lower()
    if len(query) >= _MIN_SUBSTRING_LEN and query in (candidate_title or "").lower():
        return True
    return score >= _RELEVANCE_FLOOR


def _preview(descr: str, length: int = _DESCR_PREVIEW_LEN) -> str:
    if len(descr) <= length:
        return descr
    return descr[: length - 1].rstrip() + "…"


class MessagingCore:
    """The messaging capabilities, backed by `db` and (optionally) `extension`.

    `extension` may be None. Any capability that needs a remote to finish
    raises `NeedsRemote(capability, reason)` -- but only after every check
    this module can make locally (uniqueness, roles, caps, project
    membership, ...) has already passed. Nothing is ever fabricated on the
    remote's behalf.

    `slots` holds the in-memory working slot per Partner. It defaults to a
    fresh one, so a core used on its own behaves correctly; a Polling Server
    passes the same `WorkingSlots` it drains against, because a slot that two
    objects each keep their own copy of is not a slot.
    """

    def __init__(
        self,
        db: Database,
        extension: RemoteExtension | None = None,
        slots: WorkingSlots | None = None,
    ) -> None:
        self.db = db
        self.extension = extension
        self.slots = slots if slots is not None else WorkingSlots()
        # Displacements where the previous turn could not be stopped because the
        # remote has no cancel, as `(partner_id, displaced_label, new_label)`.
        # Not part of the contract; the record exists so an operator can see
        # where two turns were live against one remote at once.
        self.uncancelled_displacements: list[tuple[int, str, str]] = []

    # -- shared resolution helpers -----------------------------------------

    def _resolve_requester(self, requester_uuid: str) -> sqlite3.Row:
        """A caller identifies itself by UUID only. Must be live."""
        row = self.db.read_one(
            "SELECT * FROM partners WHERE uuid = ? AND archived_at IS NULL", (requester_uuid,)
        )
        if row is None:
            raise Rejected(
                "unknown_requester", "No live partner is registered under this identity."
            )
        return row

    def _resolve_live_partner_by_title(self, title: str) -> sqlite3.Row:
        """Resolve a title to an identity. A title naming nothing live is a Rejected."""
        row = self.db.read_one(
            "SELECT * FROM partners WHERE title = ? AND archived_at IS NULL", (title,)
        )
        if row is None:
            raise Rejected(
                "no_such_partner",
                f"{title!r} does not name a live partner.",
                next_call="Call search_partner to find the exact title.",
            )
        return row

    def _resolve_project_by_title(self, title: str) -> sqlite3.Row:
        row = self.db.read_one("SELECT * FROM projects WHERE title = ?", (title,))
        if row is None:
            raise Rejected(
                "no_such_project",
                f"{title!r} does not name an existing project.",
                next_call="Call search_project to find the exact title.",
            )
        return row

    def _project_by_id(self, project_id: int) -> sqlite3.Row | None:
        return self.db.read_one("SELECT * FROM projects WHERE id = ?", (project_id,))

    def _source_cap(self, source_prefix: str) -> sqlite3.Row | None:
        return self.db.read_one(
            "SELECT * FROM source_caps WHERE source_prefix = ?", (source_prefix,)
        )

    def _needs_handshake(self, partner_row) -> bool:
        """Whether messaging this Partner requires a handshake.

        Read from `source_caps.needs_handshake` rather than comparing against a literal
        prefix. The design keeps per-source limits as data so that "this app is different"
        is a row and not a code change; the same argument applies to this flag, and a
        hardcoded comparison is how a seeded column silently becomes decoration.
        """
        row = self.db.read_one(
            "SELECT c.needs_handshake FROM partners p "
            "JOIN projects pr ON pr.id = p.project_id "
            "JOIN source_caps c ON c.source_prefix = pr.source_prefix "
            "WHERE p.id = ?",
            (partner_row["id"],),
        )
        return bool(row["needs_handshake"]) if row else True

    def _partner_type(self, partner: sqlite3.Row) -> str:
        """A Partner has no source of its own -- it takes its Project's source.

        `projects.source_prefix` is the single authority, and a Project has
        exactly one, so this joins `partners` -> `projects` and returns the
        Project's `source_prefix`. Two Partners in the same Project always
        share this value; nothing here ever looks at a title.
        """
        row = self.db.read_one(
            "SELECT pr.source_prefix AS source_prefix "
            "FROM partners p JOIN projects pr ON pr.id = p.project_id "
            "WHERE p.id = ?",
            (partner["id"],),
        )
        return row["source_prefix"]

    def _require_executable(self, project: sqlite3.Row) -> None:
        """Rejected("not_executable", ...) if this Partner's Project source
        says can_execute=0.

        A Partner's type IS its Project's source_prefix (see
        `_partner_type`), so this is the only check needed -- an nlm_
        Partner can only ever live in an nlm_ Project, and that project's
        can_execute=0 is what makes it un-interruptible and un-resumable.
        """
        cap = self._source_cap(project["source_prefix"])
        if cap is not None and not cap["can_execute"]:
            raise Rejected(
                "not_executable",
                f"{project['source_prefix']} partners never execute; there is nothing to "
                "stop or resume.",
            )

    def _extension_for(self, source_prefix: str, capability: str, reason: str) -> RemoteExtension:
        """The configured extension, iff it speaks for `source_prefix`.

        A missing extension and an extension configured for the *wrong*
        remote are treated identically: NeedsRemote. Calling the wrong
        remote's extension would be exactly the "fabricated remote answer"
        this module must never produce.
        """
        if self.extension is None:
            raise NeedsRemote(capability, reason)
        if self.extension.source_prefix != source_prefix:
            raise NeedsRemote(
                capability,
                f"The configured remote extension speaks for "
                f"source_prefix={self.extension.source_prefix!r}, not {source_prefix!r}. {reason}",
            )
        return self.extension

    def _layer(self, partner: sqlite3.Row) -> int:
        """Where this Partner sits in the delegation hierarchy. Lower is higher up.

        Read from `agent_layers` rather than compared against literals here,
        for the same reason `needs_handshake` is read from `source_caps`: the
        hierarchy is a fact about how this deployment is organized, and a
        deployment that adds a tier should add a row.
        """
        row = self.db.read_one(
            _LAYER_SQL,
            {"src": self._partner_type(partner), "role": partner["orchestrator_type"]},
        )
        if row is None:
            # Every source has a '*' row, so this is unreachable unless the
            # seed data has been edited. Treat an unplaced agent as the
            # bottom of the hierarchy: it may receive delegated work and may
            # not delegate upward, which is the safe direction to be wrong in.
            return 1_000_000
        return row["layer"]

    def _require_gemini(self, partner: sqlite3.Row) -> sqlite3.Row:
        """Return the Partner's project, or refuse: only Antigravity carries path grants.

        A read/write path means something only to a remote that executes
        against a filesystem. A NotebookLM source never executes at all, and a
        Claude Science frame has no per-frame path concept -- granting one a
        path would record an intention nothing will ever apply, which reads
        exactly like a grant that is being enforced. The `partner_paths` table
        refuses the same thing with a trigger; this is the readable version of
        that refusal.
        """
        project = self._project_by_id(partner["project_id"])
        if project is None or project["source_prefix"] != "gemini_":
            source = project["source_prefix"] if project is not None else "unknown"
            raise Rejected(
                "not_path_configurable",
                f"{partner['title']!r} is a {source} partner; only Antigravity conversations "
                "carry read/write path permissions.",
            )
        return project

    def _recorded_paths(self, partner_id: int) -> dict[str, list[str]]:
        """The grant `partner_paths` says this Partner is meant to hold."""
        rows = self.db.read(
            "SELECT kind, path FROM partner_paths WHERE partner_id = ? ORDER BY kind, path",
            (partner_id,),
        )
        return {
            "read": [r["path"] for r in rows if r["kind"] == "read"],
            "write": [r["path"] for r in rows if r["kind"] == "write"],
        }

    # -- identity / project management --------------------------------------

    def create_project(self, *, title: str, source_prefix: str, project_system_id: str) -> int:
        if source_prefix not in _PREFIXES:
            raise Rejected(
                "invalid_source_prefix",
                f"{source_prefix!r} is not a recognized source prefix; expected one of {_PREFIXES}.",
            )
        if self.db.read_one("SELECT id FROM projects WHERE title = ?", (title,)) is not None:
            raise Rejected("duplicate_project_title", f"A project titled {title!r} already exists.")
        if (
            self.db.read_one(
                "SELECT id FROM projects WHERE source_prefix = ? AND project_system_id = ?",
                (source_prefix, project_system_id),
            )
            is not None
        ):
            raise Rejected(
                "duplicate_project_system_id",
                f"A project for ({source_prefix!r}, {project_system_id!r}) already exists.",
            )

        ext = self._extension_for(
            source_prefix,
            "verify_project_system_id",
            "Cannot verify the project exists in the remote app without a matching remote extension.",
        )
        if not ext.verify_project_system_id(project_system_id=project_system_id):
            raise Rejected(
                "project_system_id_not_found",
                f"{project_system_id!r} does not name a real project in the remote app.",
            )

        def _create(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO projects (source_prefix, project_system_id, title) VALUES (?, ?, ?)",
                (source_prefix, project_system_id, title),
            )
            return cur.lastrowid

        return self.db.write(_create)

    def create_partner(
        self,
        *,
        project_id: int,
        title: str,
        partner_id_in_remote: str,
        descr: str,
        uuid: str | None = None,
    ) -> dict:
        project = self._project_by_id(project_id)
        if project is None:
            raise Rejected("no_such_project", f"No project with id {project_id} exists.")

        if len(descr) > _DESCR_MAX_LEN:
            raise Rejected(
                "descr_too_long", f"Description must be at most {_DESCR_MAX_LEN} characters."
            )

        if self.db.read_one("SELECT id FROM partners WHERE title = ?", (title,)) is not None:
            raise Rejected(
                "duplicate_partner_title",
                f"A partner titled {title!r} already exists (titles are unique server-wide, "
                "archived included).",
            )

        # Fast path only -- two concurrent callers can both pass this read, so it
        # is not what makes the rule hold. The schema's UNIQUE (project_id,
        # partner_id_in_remote) constraint is what actually enforces it; the
        # IntegrityError it raises is caught below, same shape as the queue cap.
        if (
            self.db.read_one(
                "SELECT id FROM partners WHERE project_id = ? AND partner_id_in_remote = ?",
                (project_id, partner_id_in_remote),
            )
            is not None
        ):
            raise Rejected(
                "partner_id_in_remote_taken",
                f"{partner_id_in_remote!r} is already registered to another partner in this "
                "project.",
            )

        ext = self._extension_for(
            project["source_prefix"],
            "verify_partner_id_in_remote",
            "Cannot verify partner_id_in_remote names a real object in the remote app without "
            "a matching remote extension.",
        )
        verified = ext.verify_partner_id_in_remote(
            project_system_id=project["project_system_id"],
            partner_id_in_remote=partner_id_in_remote,
        )
        if not verified:
            raise Rejected(
                "partner_id_in_remote_not_found",
                f"{partner_id_in_remote!r} does not name a real object in the remote app.",
            )

        new_uuid = str(uuid) if uuid is not None else str(uuid_lib.uuid4())

        def _create(conn: sqlite3.Connection) -> int:
            try:
                cur = conn.execute(
                    "INSERT INTO partners (uuid, project_id, title, partner_id_in_remote, descr) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (new_uuid, project_id, title, partner_id_in_remote, descr),
                )
            except sqlite3.IntegrityError as exc:
                if "live-partner limit" in str(exc):
                    raise Rejected(
                        "live_partner_limit",
                        "This project is at its live-partner limit.",
                        next_call="Call archive_sessions to free a slot.",
                    ) from exc
                if "partner_id_in_remote" in str(exc):
                    # The pre-check above is only a fast path and can race; this
                    # is the actual enforcement -- UNIQUE (project_id,
                    # partner_id_in_remote) caught the concurrent duplicate the
                    # read-then-insert above could not.
                    raise Rejected(
                        "partner_id_in_remote_taken",
                        f"{partner_id_in_remote!r} is already registered to another partner "
                        "in this project.",
                    ) from exc
                raise Rejected("constraint_violation", str(exc)) from exc
            return cur.lastrowid

        partner_id = self.db.write(_create)
        # The one sanctioned return of a uuid: this is the identity being
        # freshly minted for whoever will BECOME this partner, not a leak of
        # someone else's identity to a third party.
        return {"id": partner_id, "uuid": new_uuid, "title": title, "project_id": project_id}

    def search_partner(
        self,
        *,
        requester_uuid: str,
        query_title: str,
        project_id: int | None = None,
        limit: int = 3,
    ) -> list[dict]:
        """Fuzzy-match live partners by title against `query_title`, best first.

        A candidate that fails `_is_relevant` is dropped before `limit` is
        ever applied -- see that function for why a single fuzzy floor
        cannot tell a short, deliberate query from a coincidence, and why a
        literal substring match is checked as well as a fuzzy one. A query
        that matches nothing well returns fewer than `limit` results,
        possibly none, and an empty list is a normal, correct answer here,
        not a failure: the alternative is handing back `limit`
        confident-looking results for a query none of them actually
        resemble, and an agent then addressing a partner by a title it was
        never asked for.
        """
        self._resolve_requester(requester_uuid)
        sql = (
            "SELECT id, title, project_id, descr, orchestrator_type FROM partners "
            "WHERE archived_at IS NULL"
        )
        params: list[Any] = []
        if project_id is not None:
            sql += " AND project_id = ?"
            params.append(project_id)
        rows = self.db.read(sql, params)
        scored = sorted(
            (
                (
                    _score(query_title, row["title"]),
                    {
                        "id": row["id"],
                        "title": row["title"],
                        "project_id": row["project_id"],
                        "orchestrator_type": row["orchestrator_type"],
                        "descr_preview": _preview(row["descr"]),
                    },
                )
                for row in rows
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )
        result = []
        for score, item in scored:
            # Not a `break` on score: substring qualification is not
            # monotone in `score` (see `_is_relevant`), so a later, lower-
            # scoring row can still qualify after an earlier one didn't.
            # Every candidate has to be checked; only the count already
            # collected caps how far this goes.
            if not _is_relevant(query_title, item["title"], score):
                continue
            if len(result) >= limit:
                break
            item = dict(item)
            item["score"] = score
            result.append(item)
        return result

    def search_project(
        self, *, requester_uuid: str, query_title: str, limit: int = 3
    ) -> list[dict]:
        """Fuzzy-match projects by title against `query_title`, best first.

        Same relevance test as `search_partner`, and the same reasoning: a
        candidate that fails `_is_relevant` is dropped before `limit` is
        applied, so an empty list is a normal, correct answer for a query
        that matches no project well -- not `limit` weak guesses padded out
        to look like confident ones.
        """
        self._resolve_requester(requester_uuid)
        rows = self.db.read(
            "SELECT id, title, source_prefix, project_system_id FROM projects", ()
        )
        scored = sorted(
            (
                (
                    _score(query_title, row["title"]),
                    {
                        "id": row["id"],
                        "title": row["title"],
                        "source_prefix": row["source_prefix"],
                        "project_system_id": row["project_system_id"],
                    },
                )
                for row in rows
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )
        result = []
        for score, item in scored:
            # Not a `break` on score: substring qualification is not
            # monotone in `score` (see `_is_relevant`), so a later, lower-
            # scoring row can still qualify after an earlier one didn't.
            # Every candidate has to be checked; only the count already
            # collected caps how far this goes.
            if not _is_relevant(query_title, item["title"], score):
                continue
            if len(result) >= limit:
                break
            item = dict(item)
            item["score"] = score
            result.append(item)
        return result

    def _report_lost_work(self, conn: sqlite3.Connection, partner_id: int) -> None:
        """Tell every Caller with a stake in `partner_id` that its work is gone, not delayed.

        `archive_sessions` makes a Partner permanently unreachable, and used to
        do it with no regard for what was in flight: `advance()` later DELETEs
        the archived Partner's whole queue outright the moment anything next
        looks at it -- correctly, since an archived Partner can never be
        messaged again -- so every Caller waiting on one of those messages just
        never heard back.

        Called from INSIDE `archive_sessions`'s own `db.write` closure, on the
        open `conn`, right after `archived_at` is set and while the queue this
        Partner owes answers on is still readable. It must stay that way:
        `self.db.write` blocks the calling thread until the single writer
        thread finishes running whatever it was given, and that thread is this
        closure -- calling it again from here would wait on itself forever.
        `report_back` opens its own `db.write` for the exact same reason and is
        equally off limits.

        A Caller can have a stake two ways, and both are checked: a row
        already sitting in `partner_id`'s queue, and -- separately, because it
        lives in memory rather than in that table -- the Caller of whatever
        this Partner's in-memory working slot currently holds. Missing the
        second would leave the one task actually in flight unreported, since
        `advance()` promoted it out of the queue before either of these
        callers ran.

        The notice is attributed to the vanishing Partner itself, as
        `caller_id` -- not to the requester archiving it. The row surviving
        the row it names is exactly why: `archived_at` leaves the Partner's own
        row in place, so it stays a valid, permanent `caller_id` to attribute
        to, while the requester usually is NOT a safe choice -- only a
        project-orchestrator may handshake a plain `science_` worker at all, so
        the requester calling `archive_sessions` is normally the SAME partner
        as the one Caller with work queued against it, and attributing the
        notice to the requester would then name the notice's own recipient as
        its sender, which `message_queue`'s `CHECK (caller_id <> partner_id)`
        refuses outright. (This is also why `delete_partner` cannot reuse this
        helper: deleting the row instead of archiving it would cascade the
        just-inserted notice away along with everything else that named it,
        since `message_queue.caller_id` is `ON DELETE CASCADE` -- see
        `delete_partner`'s own `partner_has_work_in_flight` refusal.)

        A Caller that is the vanishing Partner itself is skipped (the
        `message_queue` row that produced it already enforces `caller_id <>
        partner_id`, but the working slot is not that table, so the check is
        repeated rather than assumed) and so is one that is already archived
        or deleted -- writing an `[ERROR]` into a queue nothing will ever
        drain is the same silent loss, one Caller removed.
        """
        partner = conn.execute(
            "SELECT title FROM partners WHERE id = ?", (partner_id,)
        ).fetchone()
        title = partner["title"] if partner is not None else "an unknown partner"

        caller_ids = {
            row["caller_id"]
            for row in conn.execute(
                "SELECT DISTINCT caller_id FROM message_queue WHERE partner_id = ?",
                (partner_id,),
            ).fetchall()
        }
        working = self.slots.get(partner_id)
        if working is not None:
            caller_ids.add(working["caller_id"])

        body = (
            f"{title!r} was just archived. Work you sent it will not run and no answer is "
            "coming back -- it is gone, not delayed."
        )
        for caller_id in caller_ids:
            if caller_id == partner_id:
                continue
            live = conn.execute(
                "SELECT id FROM partners WHERE id = ? AND archived_at IS NULL", (caller_id,)
            ).fetchone()
            if live is None:
                continue
            # `[ERROR]` is uncapped and `stored = 0` in `label_caps` -- no cap
            # to check, no `messages` row to create, so `message_id` is left
            # NULL rather than run through `_admit`. At priority 2 it goes to
            # the front of everything below it and displaces a running task
            # unless that task outranks it or ties -- displacement needs a
            # STRICTLY lower priority number, so a Caller mid-`[QUERY]` or
            # mid-summary finishes that first. That is still the interruption;
            # no separate mechanism is needed to deliver it.
            conn.execute(
                "INSERT INTO message_queue (partner_id, caller_id, behavior, body) "
                "VALUES (?, ?, '[ERROR]', ?)",
                (caller_id, partner_id, body),
            )

        # The vanishing Partner cannot be polled again either way, so nothing
        # will ever act on its working slot -- leaving it set would just be a
        # second place the same stale fact could be read from.
        self.slots.clear(partner_id)

    def delete_partner(self, *, requester_uuid: str, partner_title: str) -> dict:
        requester = self._resolve_requester(requester_uuid)
        target = self._resolve_live_partner_by_title(partner_title)

        # Titles are not secret -- search_partner already discloses every
        # live partner's title to any live requester -- so resolving the
        # target before authorizing does not create the kind of
        # existence-oracle grant_gemini_budget had over grantee_uuid.
        # Deletion is destructive and irreversible, so it is scoped MORE
        # tightly than archive_sessions -- which requires only that the target
        # be in the requester's own project. The extra requirement here is the
        # role. Archiving frees a live-partner slot and leaves the row (and its
        # spent title) in place; deletion does not, so the two are not the same
        # authority and an earlier version of this comment was wrong to say so.
        if (
            requester["orchestrator_type"] != "project-orchestrator"
            or requester["project_id"] != target["project_id"]
        ):
            raise Rejected(
                "not_authorized",
                "Only the project-orchestrator of the target's own project may delete a "
                "partner.",
            )

        def _delete(conn: sqlite3.Connection) -> None:
            # Deletion is irreversible and, unlike archive_sessions, has no way
            # to tell anyone about it afterward: message_queue.caller_id is ON
            # DELETE CASCADE, so an [ERROR] inserted here to explain the loss
            # would be deleted right along with everything else naming this
            # partner, by the DELETE two lines below -- the notice cannot
            # outlive the row it depends on. Attributing the notice to the
            # requester instead does not fix it either: only a
            # project-orchestrator may handshake a plain worker at all, so the
            # requester deleting it is normally the SAME partner as the one
            # Caller with work queued against it, and a notice cannot name its
            # own recipient as its sender (message_queue's own `CHECK
            # (caller_id <> partner_id)` refuses that). So this refuses instead
            # of silently dropping the work or crashing a later report_back --
            # archive_sessions is the version of this that CAN tell every
            # waiting Caller, because it leaves the row in place.
            has_queued = (
                conn.execute(
                    "SELECT 1 FROM message_queue WHERE partner_id = ? LIMIT 1", (target["id"],)
                ).fetchone()
                is not None
            )
            if has_queued or self.slots.get(target["id"]) is not None:
                raise Rejected(
                    "partner_has_work_in_flight",
                    f"{target['title']!r} still has work queued or in progress against it; "
                    "deleting it now would drop that work with no way to tell whoever is "
                    "waiting.",
                    next_call="Call archive_sessions instead; it reports the loss to every "
                    "caller waiting.",
                )
            try:
                conn.execute("DELETE FROM partners WHERE id = ?", (target["id"],))
            except sqlite3.IntegrityError as exc:
                raise Rejected(
                    "partner_has_dependents",
                    "This partner cannot be deleted because another record still refers to it "
                    "(for example, a gemini budget grant it made). Archive it instead.",
                    next_call="Call archive_sessions instead.",
                ) from exc

        self.db.write(_delete)
        return {"deleted_id": target["id"], "title": target["title"]}

    def delete_project(self, *, requester_uuid: str, project_title: str) -> dict:
        requester = self._resolve_requester(requester_uuid)
        project = self._resolve_project_by_title(project_title)

        # Same scoping as delete_partner/archive_sessions: only the
        # project-orchestrator of the project being deleted may cascade-
        # delete it and every partner it holds.
        if (
            requester["orchestrator_type"] != "project-orchestrator"
            or requester["project_id"] != project["id"]
        ):
            raise Rejected(
                "not_authorized",
                "Only the project-orchestrator of a project may delete it.",
            )

        def _delete(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM partners WHERE project_id = ?", (project["id"],)
            ).fetchone()
            # The same two guards `delete_partner` carries, for the same two
            # reasons, applied to every Partner the cascade would take.
            #
            # Work in flight first: deleting the project cascades away every
            # queue row under it, and `message_queue.caller_id` is
            # `ON DELETE CASCADE` too, so a notice written to warn a waiting
            # Caller is destroyed by the very DELETE it warns about. There is
            # no shape of message that survives this, which is why it refuses
            # rather than reporting.
            in_flight = conn.execute(
                "SELECT p.title AS title FROM partners p "
                "JOIN message_queue q ON q.partner_id = p.id "
                "WHERE p.project_id = ? LIMIT 1",
                (project["id"],),
            ).fetchone()
            if in_flight is None:
                for held in self.slots.occupied():
                    owner = conn.execute(
                        "SELECT title FROM partners WHERE id = ? AND project_id = ?",
                        (held, project["id"]),
                    ).fetchone()
                    if owner is not None:
                        in_flight = owner
                        break
            if in_flight is not None:
                raise Rejected(
                    "partner_has_work_in_flight",
                    f"{project['title']!r} still holds work in flight (for example against "
                    f"{in_flight['title']!r}); deleting it now would drop that work with no "
                    "way to tell whoever is waiting.",
                    next_call="Call archive_sessions on its partners instead; archiving "
                    "reports the loss to every caller waiting.",
                )
            try:
                conn.execute("DELETE FROM projects WHERE id = ?", (project["id"],))
            except sqlite3.IntegrityError as exc:
                # A budget grant made by one of these partners references it
                # with no `ON DELETE` clause, so the cascade stops on a foreign
                # key rather than a rule -- and a raw IntegrityError reaching a
                # caller is a stack trace where an explanation belongs.
                raise Rejected(
                    "partner_has_dependents",
                    "This project cannot be deleted because a record outside it still refers "
                    "to one of its partners (for example, a gemini budget grant one of them "
                    "made). Archive its partners instead.",
                    next_call="Call archive_sessions instead.",
                ) from exc
            return row["n"]

        partners_deleted = self.db.write(_delete)
        return {
            "deleted_id": project["id"],
            "title": project["title"],
            "partners_deleted": partners_deleted,
        }

    def claim_orchestrator(
        self, *, requester_uuid: str, project_id: int, orchestrator_type: str
    ) -> dict:
        requester = self._resolve_requester(requester_uuid)

        if orchestrator_type not in _ORCHESTRATOR_TYPES:
            raise Rejected(
                "invalid_orchestrator_type",
                f"{orchestrator_type!r} is not a recognized role; expected one of "
                f"{_ORCHESTRATOR_TYPES}.",
            )
        if requester["project_id"] != project_id:
            raise Rejected(
                "wrong_project", "The requester is not a partner of the given project."
            )
        if requester["orchestrator_type"] is not None:
            raise Rejected(
                "already_has_role",
                "This partner already holds a role; roles are claimed once and never reassigned.",
            )
        # ALL THREE roles are Claude Science roles. Not just gemini-orchestrator,
        # which is the only one an earlier version restricted -- an Antigravity
        # or NotebookLM partner could claim project-orchestrator or
        # bridge-scientist, and every path that gap could have been exploited
        # through turned out to be independently guarded, so nothing crossed a
        # boundary. "Currently unreachable" is not a rule, and the roles are
        # named after what they orchestrate rather than what holds them: a
        # gemini-orchestrator is a Claude Science agent that directs Antigravity,
        # never an Antigravity agent. Read `handshake`.
        #
        # The requester is already known to be a partner of `project_id` (the
        # wrong_project check above), and a Partner's type IS its Project's
        # source_prefix -- so the project's source is the only thing left to
        # check; there is no separate "is this partner science_" question.
        project = self._project_by_id(project_id)
        if project is None or project["source_prefix"] != "science_":
            source = project["source_prefix"] if project is not None else "unknown"
            raise Rejected(
                "orchestrator_requires_science_project",
                f"{orchestrator_type!r} can only be claimed inside a science_ project; this "
                f"partner is {source}. All three orchestrator roles are Claude Science roles.",
            )

        def _claim(conn: sqlite3.Connection) -> None:
            try:
                conn.execute(
                    "UPDATE partners SET orchestrator_type = ? "
                    "WHERE id = ? AND orchestrator_type IS NULL",
                    (orchestrator_type, requester["id"]),
                )
            except sqlite3.IntegrityError as exc:
                raise Rejected(
                    "role_already_claimed",
                    f"Another partner already holds {orchestrator_type!r} in this project.",
                ) from exc

        self.db.write(_claim)
        return {
            "partner_id": requester["id"],
            "project_id": project_id,
            "orchestrator_type": orchestrator_type,
        }

    def grant_gemini_budget(
        self, *, requester_uuid: str, grantee_uuid: str, budget_count: int
    ) -> dict:
        requester = self._resolve_requester(requester_uuid)

        # Authorization is checked before anything that depends on
        # `grantee_uuid`'s existence or role. `grantee_uuid` is a value that
        # "must never be leaked" -- so a requester who isn't entitled to ask
        # about it at all must not be able to use the shape of the refusal
        # to learn whether it names nothing, names a live partner with the
        # wrong role, or names a live gemini-orchestrator. A requester who
        # doesn't hold project-orchestrator anywhere is refused without a
        # single query ever touching `grantee_uuid`.
        if requester["orchestrator_type"] != "project-orchestrator":
            raise Rejected(
                "not_authorized",
                "Only the project-orchestrator of the grantee's own project may grant gemini "
                "budget.",
            )

        # A requester who IS a project-orchestrator, but names a grantee
        # that either does not exist or belongs to a different project,
        # gets the SAME refusal and code as above -- "wrong project" and
        # "does not exist" are deliberately not distinguished, for the same
        # information-flow reason. Only once the grantee is confirmed to be
        # a live partner of the requester's OWN project (something the
        # requester is already entitled to know via search_partner) does
        # this proceed to a role check that can safely differ.
        grantee = self.db.read_one(
            "SELECT * FROM partners WHERE uuid = ? AND archived_at IS NULL AND project_id = ?",
            (grantee_uuid, requester["project_id"]),
        )
        if grantee is None:
            raise Rejected(
                "not_authorized",
                "Only the project-orchestrator of the grantee's own project may grant gemini "
                "budget.",
            )
        if grantee["orchestrator_type"] != "gemini-orchestrator":
            raise Rejected(
                "grantee_not_gemini_orchestrator",
                "Gemini budget can only be granted to a gemini-orchestrator.",
            )
        if not (0 <= budget_count <= 3):
            raise Rejected("invalid_budget_count", "Budget must be between 0 and 3 inclusive.")

        def _grant(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO budget_grants (grantee_partner, granted_by, budget_count)
                VALUES (?, ?, ?)
                ON CONFLICT(grantee_partner) DO UPDATE SET
                    granted_by = excluded.granted_by,
                    budget_count = excluded.budget_count,
                    granted_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (grantee["id"], requester["id"], budget_count),
            )

        self.db.write(_grant)
        return {
            "grantee_id": grantee["id"],
            "granted_by_id": requester["id"],
            "budget_count": budget_count,
        }

    def archive_sessions(self, *, requester_uuid: str, titles: list[str]) -> dict:
        requester = self._resolve_requester(requester_uuid)

        def _archive(conn: sqlite3.Connection) -> tuple[list[str], list[dict]]:
            archived: list[str] = []
            skipped: list[dict] = []
            for t in titles:
                row = conn.execute(
                    "SELECT id, project_id FROM partners WHERE title = ? AND archived_at IS NULL",
                    (t,),
                ).fetchone()
                if row is None:
                    skipped.append({"title": t, "reason": "not_found_or_already_archived"})
                    continue
                # Scoped to the requester's own project: archive_sessions
                # exists to free THAT project's live-partner slot.
                if row["project_id"] != requester["project_id"]:
                    skipped.append({"title": t, "reason": "different_project"})
                    continue
                conn.execute(
                    "UPDATE partners SET archived_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    "WHERE id = ?",
                    (row["id"],),
                )
                # An archived partner can never be messaged again, and its
                # whole queue is deleted outright the moment advance() next
                # looks at it (see advance()'s own comment). Report it here,
                # while the queue this partner owes answers on is still
                # readable, rather than let every Caller on it wait forever
                # for a reply that was never going to come.
                self._report_lost_work(conn, row["id"])
                archived.append(t)
            return archived, skipped

        archived, skipped = self.db.write(_archive)
        return {"archived": archived, "archived_count": len(archived), "skipped": skipped}

    def status(self, *, requester_uuid: str) -> dict:
        requester = self._resolve_requester(requester_uuid)
        project = self._project_by_id(requester["project_id"])

        # The queue, broken down by label rather than as one number. A depth of
        # four says nothing useful; "three [RESEARCH], one [QUERY]" says what
        # the partner is about to do next and why.
        queue_rows = self.db.read(
            "SELECT q.behavior AS behavior, COUNT(*) AS n, "
            "       SUM(q.in_process) AS paused, MIN(c.priority) AS priority "
            "  FROM message_queue q JOIN label_caps c ON c.behavior = q.behavior "
            " WHERE q.partner_id = ? GROUP BY q.behavior ORDER BY MIN(c.priority)",
            (requester["id"],),
        )
        queued = [
            {
                "behavior": r["behavior"],
                "count": r["n"],
                "paused": r["paused"] or 0,
                "priority": r["priority"],
            }
            for r in queue_rows
        ]
        queue_depth = sum(r["count"] for r in queued)
        working = self.slots.get(requester["id"])
        working_task = None
        if working is not None:
            # `enqueued_at` and `started_at` are written in the same UTC shape
            # on purpose (see `_now()`) so this subtraction is possible at
            # all -- the queue row that held `enqueued_at` is long gone by the
            # time a task is in the working slot; the slot is the only place
            # the wait survives to be measured. `_parse_ts` is total, so a
            # missing or malformed timestamp yields `None` here rather than an
            # exception in a diagnostic call.
            enqueued_dt = _parse_ts(working.get("enqueued_at"))
            started_dt = _parse_ts(working.get("started_at"))
            waited_ms = (
                round((started_dt - enqueued_dt).total_seconds() * 1000)
                if enqueued_dt is not None and started_dt is not None
                else None
            )
            working_task = {
                "behavior": working["behavior"],
                "caller_id": working["caller_id"],
                "resumed": bool(working.get("in_process")),
                "enqueued_at": working.get("enqueued_at"),
                "started_at": working.get("started_at"),
                "waited_ms": waited_ms,
            }

        # An archived partner "never appears in status" -- filter both
        # directions of the handshake join, not just the read/send/handshake
        # entry points, or a counterpart's status() would keep listing an
        # archived partner's title forever.
        out_titles = [
            r["title"]
            for r in self.db.read(
                "SELECT p.title AS title FROM handshakes h "
                "JOIN partners p ON p.id = h.to_partner "
                "WHERE h.from_partner = ? AND p.archived_at IS NULL",
                (requester["id"],),
            )
        ]
        in_titles = [
            r["title"]
            for r in self.db.read(
                "SELECT p.title AS title FROM handshakes h "
                "JOIN partners p ON p.id = h.from_partner "
                "WHERE h.to_partner = ? AND p.archived_at IS NULL",
                (requester["id"],),
            )
        ]

        gemini_budget = None
        if requester["orchestrator_type"] == "gemini-orchestrator":
            grant = self.db.read_one(
                "SELECT budget_count FROM budget_grants WHERE grantee_partner = ?",
                (requester["id"],),
            )
            used = self.db.read_one(
                "SELECT COUNT(*) AS n FROM handshakes h "
                "JOIN partners p ON p.id = h.to_partner "
                "JOIN projects pr ON pr.id = p.project_id "
                "WHERE h.from_partner = ? AND pr.source_prefix = 'gemini_'",
                (requester["id"],),
            )["n"]
            gemini_budget = {
                "budget_count": grant["budget_count"] if grant is not None else 0,
                "used": used,
            }

        return {
            "partner_id": requester["id"],
            "title": requester["title"],
            "project_id": requester["project_id"],
            "project_title": project["title"] if project is not None else None,
            "orchestrator_type": requester["orchestrator_type"],
            "layer": self._layer(requester),
            "queue_depth": queue_depth,
            "queued": queued,
            "working": working_task,
            "handshakes_out": out_titles,
            "handshakes_in": in_titles,
            "gemini_budget": gemini_budget,
        }

    def handshake(self, *, requester_uuid: str, partner_title: str) -> dict:
        requester = self._resolve_requester(requester_uuid)
        target = self._resolve_live_partner_by_title(partner_title)

        if requester["id"] == target["id"]:
            raise Rejected("self_handshake", "A partner cannot handshake itself.")

        tgt_type = self._partner_type(target)
        req_type = self._partner_type(requester)

        if not self._needs_handshake(target):
            raise Rejected(
                "handshake_not_needed",
                "nlm_ partners need no handshake and may be messaged directly.",
                next_call="Call send directly.",
            )

        # Claude code is handled first and on its own, because it is the one
        # participant that holds no orchestrator role and so cannot satisfy
        # the general rule below. It has exactly one legal counterpart: a
        # bridge-scientist. That pairing is the entire reason the
        # bridge-scientist role exists -- it is the seam between a human's
        # Claude Code session and the research project, and a seam with two
        # ends on one side is not a seam.
        code_involved = req_type == "code_" or tgt_type == "code_"
        if code_involved:
            science_end, code_end = (
                (target, requester) if req_type == "code_" else (requester, target)
            )
            if self._partner_type(science_end) != "science_" or (
                science_end["orchestrator_type"] != "bridge-scientist"
            ):
                raise Rejected(
                    "code_handshakes_bridge_only",
                    "A code_ partner may only handshake a bridge-scientist, and a "
                    "bridge-scientist is the only partner that may handshake a code_ partner.",
                )
            # "with exactly one Claude code": a bridge holding two would make
            # "the Caller" ambiguous for every message that reaches it.
            other_code = self.db.read_one(
                "SELECT h.id FROM handshakes h "
                "JOIN partners p ON p.id IN (h.from_partner, h.to_partner) "
                "JOIN projects pr ON pr.id = p.project_id "
                "WHERE ? IN (h.from_partner, h.to_partner) AND pr.source_prefix = 'code_' "
                "  AND p.id != ? AND p.archived_at IS NULL",
                (science_end["id"], code_end["id"]),
            )
            if other_code is not None:
                raise Rejected(
                    "bridge_single_code_partner",
                    "This bridge-scientist is already handshaken with a different code_ partner.",
                )
        # A gemini_ pair is decided here, above the orchestrator check, for
        # exactly the reason the code_ branch is: neither participant holds a
        # role and neither ever will. All three orchestrator roles are Claude
        # Science roles (`orchestrator_requires_science_project`), so a gemini_
        # requester can never satisfy the general rule below, and the
        # cross-Project branch further down would refuse it a second time for
        # not matching a role its counterpart also does not hold.
        #
        # What this permits is one Antigravity conversation continuing another.
        # A Project holds at most `max_live_partners` live Partners, so an
        # effort outlasting one conversation needs another Project rather than
        # a larger ceiling -- and `project_extension` is how two Projects are
        # declared parts of one effort. Nothing is carried across: not
        # permissions, not queued work. It is a handshake and only that.
        elif req_type == "gemini_" and tgt_type == "gemini_":
            if requester["project_id"] == target["project_id"]:
                raise Rejected(
                    "no_handshake_between_gemini",
                    "Two Antigravity conversations in one project already answer to the "
                    "same gemini-orchestrator; there is nothing for one to inherit from "
                    "the other.",
                )
            lo, hi = sorted((requester["project_id"], target["project_id"]))
            link = self.db.read_one(
                "SELECT 1 AS ok FROM project_extension WHERE project_a = ? AND project_b = ?",
                (lo, hi),
            )
            if link is None:
                raise Rejected(
                    "different_project",
                    "These two Antigravity conversations belong to projects that have not "
                    "been declared extensions of one another.",
                    next_call="Have the gemini-orchestrator call extend_project on the two "
                    "projects first.",
                )
            # A lineage is a line, not a fork. Without this, "which conversation
            # continues this one" has more than one answer, and every message
            # travelling back up the chain has more than one place to arrive.
            successor = self.db.read_one(
                "SELECT h.from_partner FROM handshakes h "
                "JOIN partners p ON p.id = h.from_partner "
                "JOIN projects pr ON pr.id = p.project_id "
                "WHERE h.to_partner = ? AND pr.source_prefix = 'gemini_' "
                "  AND p.id != ? AND p.archived_at IS NULL",
                (target["id"], requester["id"]),
            )
            if successor is not None:
                raise Rejected(
                    "gemini_already_inherited",
                    "Another Antigravity conversation already continues this one. A "
                    "conversation is inherited from once, so that which conversation "
                    "succeeds it has exactly one answer.",
                )
        elif requester["orchestrator_type"] is None:
            raise Rejected(
                "requester_not_orchestrator", "Only an orchestrator may initiate a handshake."
            )

        # A Project has exactly one source_prefix, so a same-source pair is
        # necessarily in the same Project unless the two Projects have been
        # declared extensions of one another, and a cross-source pair
        # (science_ -> gemini_, code_ -> science_) is necessarily in
        # DIFFERENT Projects. "Both agents must be under the same Project" is
        # therefore scoped to same-source pairs only -- rejecting a
        # cross-source handshake for being cross-Project would reject the one
        # shape that is supposed to work.
        # A gemini_ pair was already decided above, extension row and all --
        # by the one branch that knows neither side holds a role. Falling into
        # the general cross-project rule here would ask them for a matching
        # role a second time and refuse every one of them.
        gemini_pair = req_type == "gemini_" and tgt_type == "gemini_"
        extended = gemini_pair
        if not gemini_pair and req_type == tgt_type and requester["project_id"] != target["project_id"]:
            lo, hi = sorted((requester["project_id"], target["project_id"]))
            link = self.db.read_one(
                "SELECT 1 AS ok FROM project_extension WHERE project_a = ? AND project_b = ?",
                (lo, hi),
            )
            if link is None:
                raise Rejected(
                    "different_project",
                    "Both partners must belong to the same project, or to two projects "
                    "registered as extensions of one another.",
                    next_call="Call extend_project to link the two projects first.",
                )
            # Across an extension the pair must hold the SAME role. The point
            # of an extension is a research effort too large for one Project's
            # live-partner ceiling, branching sideways -- not a second chain of
            # command. Requiring identical roles is what stops a
            # gemini-orchestrator in one Project from taking direction from a
            # project-orchestrator in another, which would be inheriting a
            # superior it was never given.
            if (
                requester["orchestrator_type"] is None
                or requester["orchestrator_type"] != target["orchestrator_type"]
            ):
                raise Rejected(
                    "cross_project_requires_same_role",
                    "A handshake across a project extension is only legal between two "
                    "partners holding the same orchestrator role.",
                )
            extended = True
            # Note what this does NOT do: it grants nothing inside a single
            # Project. Two partners of one Project can never reach here (their
            # project ids are equal), and one Project can hold only one partner
            # per role anyway -- so an extension cannot be used to loosen the
            # rules among partners that were already together.

        existing = self.db.read_one(
            "SELECT id FROM handshakes WHERE from_partner = ? AND to_partner = ?",
            (requester["id"], target["id"]),
        )
        if existing is not None:
            raise Rejected("duplicate_handshake", "A handshake already exists in this direction.")

        # A gemini_ pair reaching here has already been through the branch
        # above, which is the only place that knows what a legal one looks
        # like: different Projects, linked by an extension, and no successor
        # already claimed. Refusing it again here would make that branch
        # unreachable -- the pair-of-sources rules below are about who directs
        # whom, and inheritance is not a direction of command.
        if req_type == "gemini_" and tgt_type == "science_":
            raise Rejected(
                "gemini_to_science_illegal", "The gemini_ -> science_ direction is never legal."
            )

        if extended:
            # Same role, registered extension, both generic checks passed. The
            # role-pair rules below are about who directs whom inside one
            # Project and have nothing to say about two peers.
            pass
        elif code_involved:
            pass
        elif req_type == "science_" and tgt_type == "science_":
            if requester["orchestrator_type"] == "bridge-scientist":
                # The bridge's only other counterpart. It hands work down to
                # the project-orchestrator and takes it back; it has no
                # business wiring up anything else in the project.
                if target["orchestrator_type"] != "project-orchestrator":
                    raise Rejected(
                        "bridge_handshakes_orchestrator_or_code",
                        "A bridge-scientist may only handshake the project-orchestrator or "
                        "a single code_ partner.",
                    )
            elif requester["orchestrator_type"] != "project-orchestrator":
                raise Rejected(
                    "requires_project_orchestrator",
                    "Only the project-orchestrator may handshake two science_ partners.",
                )
        elif req_type == "science_" and tgt_type == "gemini_":
            if requester["orchestrator_type"] != "gemini-orchestrator":
                raise Rejected(
                    "requires_gemini_orchestrator",
                    "Only the gemini-orchestrator may handshake a science_ partner to a "
                    "gemini_ partner.",
                )
            # Scoped to science_ sources, which is what this rule has always
            # said it counts. Counting every inbound handshake would make an
            # inherited conversation permanently unreachable by the
            # orchestrator that pays budget for it -- the successor's
            # inheritance row would read as a second master.
            other_source = self.db.read_one(
                "SELECT h.from_partner FROM handshakes h "
                "JOIN partners p ON p.id = h.from_partner "
                "JOIN projects pr ON pr.id = p.project_id "
                "WHERE h.to_partner = ? AND h.from_partner != ? "
                "  AND pr.source_prefix = 'science_'",
                (target["id"], requester["id"]),
            )
            if other_source is not None:
                raise Rejected(
                    "gemini_single_science_source",
                    "This gemini_ partner is already handshaken from a different science_ partner.",
                )
            grant = self.db.read_one(
                "SELECT budget_count FROM budget_grants WHERE grantee_partner = ?",
                (requester["id"],),
            )
            if grant is None:
                raise Rejected(
                    "no_gemini_budget",
                    "No gemini budget has been granted to this gemini-orchestrator.",
                    next_call="Call grant_gemini_budget first.",
                )
            used = self.db.read_one(
                "SELECT COUNT(*) AS n FROM handshakes h "
                "JOIN partners p ON p.id = h.to_partner "
                "JOIN projects pr ON pr.id = p.project_id "
                "WHERE h.from_partner = ? AND pr.source_prefix = 'gemini_'",
                (requester["id"],),
            )["n"]
            if used >= grant["budget_count"]:
                raise Rejected(
                    "gemini_budget_exceeded",
                    "This gemini-orchestrator has used its entire gemini budget.",
                    next_call="Call grant_gemini_budget to raise the budget.",
                )
        # bridge-scientist and any other (type, type) pair not named above
        # falls through, allowed once the generic checks above have passed.

        def _insert(conn: sqlite3.Connection) -> int:
            try:
                cur = conn.execute(
                    "INSERT INTO handshakes (from_partner, to_partner) VALUES (?, ?)",
                    (requester["id"], target["id"]),
                )
            except sqlite3.IntegrityError as exc:
                raise Rejected(
                    "duplicate_handshake", "A handshake already exists in this direction."
                ) from exc
            return cur.lastrowid

        handshake_id = self.db.write(_insert)
        return {
            "handshake_id": handshake_id,
            "from_partner_id": requester["id"],
            "to_partner_id": target["id"],
            "to_partner_title": target["title"],
        }

    def read(
        self, *, requester_uuid: str, partner_title: str, page: int = 1, page_size: int = 10
    ) -> dict:
        self._resolve_requester(requester_uuid)
        target = self._resolve_live_partner_by_title(partner_title)

        if page < 1 or page_size < 1:
            raise Rejected("invalid_pagination", "page and page_size must both be at least 1.")

        total = self.db.read_one(
            "SELECT COUNT(*) AS n FROM messages WHERE to_partner = ?", (target["id"],)
        )["n"]
        offset = (page - 1) * page_size
        rows = self.db.read(
            """
            SELECT m.id AS id, m.behavior AS behavior, m.body AS body, m.created_at AS created_at,
                   p.title AS from_title
            FROM messages m JOIN partners p ON p.id = m.from_partner
            WHERE m.to_partner = ?
            ORDER BY m.id DESC
            LIMIT ? OFFSET ?
            """,
            (target["id"], page_size, offset),
        )
        messages = [
            {
                "id": r["id"],
                "from_partner_title": r["from_title"],
                "behavior": r["behavior"],
                "body": r["body"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
        # Empty is a real answer, not an error -- no special-casing needed.
        return {
            "partner_id": target["id"],
            "title": target["title"],
            "page": page,
            "page_size": page_size,
            "total": total,
            "messages": messages,
        }


    def extend_project(
        self, *, requester_uuid: str, project_title: str,
        other_project_title: str | None = None,
    ) -> dict:
        """Declare two Projects extensions of each other.

        A Project holds at most `source_caps.max_live_partners` live Partners,
        and that ceiling is deliberate -- it is what `archive_sessions` exists
        to manage. Research at scale therefore needs more Projects, not a
        larger ceiling, and this is how two of them are declared to be parts
        of one effort. Once linked, Partners under them may handshake across
        the boundary. Read `handshake` for what that then permits.

        Two forms, and the second exists because the first cannot reach every
        pair it should.

        With `other_project_title` omitted, the requester's OWN Project is
        linked to the named one, and only its project-orchestrator may do it.

        With `other_project_title` given, the two NAMED Projects are linked and
        neither need be the requester's own -- permitted to a
        gemini-orchestrator linking two `gemini_` Projects. A `gemini_` Project
        can hold no role at all (all three are Claude Science roles), so it has
        no project-orchestrator to extend it and no "requester's own Project"
        to be extended from; without this form two Antigravity Projects could
        never be linked, and one conversation could never continue another.
        The gemini-orchestrator is the authority because it is already the only
        role that may reach an Antigravity conversation, and the one whose
        budget is spent per conversation.

        Symmetric, and stored once: the pair goes in with the lower project id
        first, so "is A an extension of B" has exactly one row to look at and
        cannot answer differently depending on which way it is asked.

        Raises:
            Rejected: `requires_project_orchestrator`, `requires_gemini_orchestrator`,
                `self_extension`, `cross_source_extension`, `no_such_project`.
        """
        requester = self._resolve_requester(requester_uuid)
        if other_project_title is None:
            if requester["orchestrator_type"] != "project-orchestrator":
                raise Rejected(
                    "requires_project_orchestrator",
                    "Only the project-orchestrator may extend its project.",
                )
            mine = self._project_by_id(requester["project_id"])
            other = self._resolve_project_by_title(project_title)
        else:
            if requester["orchestrator_type"] != "gemini-orchestrator":
                raise Rejected(
                    "requires_gemini_orchestrator",
                    "Only the gemini-orchestrator may link two projects it does not "
                    "belong to, and only two gemini_ projects.",
                )
            mine = self._resolve_project_by_title(project_title)
            other = self._resolve_project_by_title(other_project_title)
            if mine["source_prefix"] != "gemini_" or other["source_prefix"] != "gemini_":
                raise Rejected(
                    "requires_gemini_orchestrator",
                    "The two-project form links gemini_ projects only; a project of any "
                    "other source is extended by its own project-orchestrator.",
                )
        if other["id"] == mine["id"]:
            raise Rejected(
                "self_extension", "A project cannot be declared an extension of itself."
            )
        if other["source_prefix"] != mine["source_prefix"]:
            # A cross-source pair already handshakes without an extension --
            # that is the ordinary delegation shape (science_ -> gemini_). An
            # extension only ever loosens the SAME-source rule, so linking two
            # sources would be a row that changes nothing and implies it does.
            raise Rejected(
                "cross_source_extension",
                "Two projects of different sources are already able to handshake across the "
                "project boundary; an extension would grant nothing.",
            )
        lo, hi = sorted((mine["id"], other["id"]))

        def _link(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "INSERT OR IGNORE INTO project_extension (project_a, project_b) VALUES (?, ?)",
                (lo, hi),
            )
            return cur.rowcount == 1

        created = self.db.write(_link)
        return {
            "project_a": lo,
            "project_b": hi,
            "created": created,
            "already_linked": not created,
        }

    # -- permissions ---------------------------------------------------------
    #
    # Three standalone capabilities, and `send` is deliberately not one of
    # them. A permission prompt means the grant was missing BEFORE the work
    # started, so configuring paths as a side effect of sending work would
    # always be one step too late -- the prompt has already happened by then.
    # Paths are configured in advance, on purpose, and that is the whole
    # doctrine in "Antigravity state handling".
    #
    # They are also what replaced the old resume capability. The route back
    # from a blocked Partner is: interrupt_partner, an [ERROR] reply naming
    # what was missing, add_permissions/delete_permissions to correct the set,
    # then a fresh send. There is nothing to "resume" -- correcting the grant
    # and sending again IS the resumption, and it is one fewer state to get
    # wrong.

    def get_permissions(self, *, requester_uuid: str, partner_title: str) -> dict:
        """Report what the conversation actually allows, beside what it is meant to.

        Both, always. `partner_paths` records the intended grant and the
        remote holds the real one; they are allowed to drift, and the drift is
        the only thing a Caller can act on. A report of one number would leave
        it unable to tell a missing grant from an unrecorded one.
        """
        self._resolve_requester(requester_uuid)
        target = self._resolve_live_partner_by_title(partner_title)
        self._require_gemini(target)

        ext = self._extension_for(
            "gemini_",
            "get_permissions",
            "Reading a conversation's permission set requires the Antigravity extension.",
        )
        actual = list(ext.get_permissions(partner_id_in_remote=target["partner_id_in_remote"]))
        recorded = self._recorded_paths(target["id"])
        intended = [f"read_file({p})" for p in recorded["read"]]
        intended += [f"write_file({p})" for p in recorded["write"]]
        return {
            "partner_id": target["id"],
            "title": target["title"],
            "allowed": actual,
            "recorded": recorded,
            "missing": [r for r in intended if r not in actual],
            "unrecorded": [r for r in actual if r not in intended],
        }

    def _apply_and_verify(self, target: sqlite3.Row, ext, apply, expect: dict) -> list[str]:
        """Run a permission change, then read the remote back and check it landed.

        `expect` maps a rule string to whether it must be present afterwards.
        A mismatch raises rather than returning -- a Caller that believes a
        permission was granted when it was not will send work that stops on a
        prompt, which is the exact failure the approval doctrine exists to
        prevent. Reporting success it cannot see is worse than refusing.
        """
        apply()
        actual = list(ext.get_permissions(partner_id_in_remote=target["partner_id_in_remote"]))
        wrong = [rule for rule, present in expect.items() if (rule in actual) != present]
        if wrong:
            raise Rejected(
                "permission_not_applied",
                "The conversation's permission set does not match what was just written: "
                f"{', '.join(sorted(wrong))}. Nothing has been recorded locally.",
                next_call="Call get_permissions to see the conversation's current set.",
            )
        return actual

    def add_permissions(
        self,
        *,
        requester_uuid: str,
        partner_title: str,
        read_paths: list[str] | None = None,
        write_paths: list[str] | None = None,
    ) -> dict:
        """Grant paths to an Antigravity conversation, and record the intent.

        Write paths must include files that do not exist yet but are expected
        to be created. A grant covering only what is already on disk
        guarantees a prompt the first time the Partner writes something new,
        and that prompt is an error rather than a question.

        Args:
            requester_uuid: The Caller's identity.
            partner_title: The conversation to grant to.
            read_paths: Paths it may read. Adding one it already holds is not
                an error; it is reported as unchanged.
            write_paths: Paths it may write, existing or not.

        Returns:
            What was granted, what was already there, and the conversation's
            full set afterwards.

        Raises:
            Rejected: `not_path_configurable` for any non-Antigravity partner;
                `no_paths` if both lists are empty; `permission_not_applied`
                if the remote does not show the grant afterwards.
            NeedsRemote: if no Antigravity extension is configured.
        """
        self._resolve_requester(requester_uuid)
        target = self._resolve_live_partner_by_title(partner_title)
        self._require_gemini(target)
        reads = list(dict.fromkeys(read_paths or []))
        writes = list(dict.fromkeys(write_paths or []))
        if not reads and not writes:
            raise Rejected("no_paths", "Give at least one read path or one write path to grant.")

        ext = self._extension_for(
            "gemini_",
            "add_permissions",
            "Granting a path to a conversation requires the Antigravity extension.",
        )
        before = set(ext.get_permissions(partner_id_in_remote=target["partner_id_in_remote"]))
        wanted = [f"read_file({p})" for p in reads] + [f"write_file({p})" for p in writes]
        already = [r for r in wanted if r in before]
        new = [r for r in wanted if r not in before]

        actual = before
        if new:
            actual = self._apply_and_verify(
                target,
                ext,
                lambda: ext.add_permissions(
                    partner_id_in_remote=target["partner_id_in_remote"], rules=new
                ),
                {rule: True for rule in new},
            )

        # Recorded only once the remote is confirmed to hold it. The other
        # order would leave `partner_paths` claiming a grant that does not
        # exist, which is the one direction of drift a Caller cannot recover
        # from by reading.
        def _record(conn: sqlite3.Connection) -> None:
            for kind, paths in (("read", reads), ("write", writes)):
                for path in paths:
                    conn.execute(
                        "INSERT OR IGNORE INTO partner_paths (partner_id, kind, path) "
                        "VALUES (?, ?, ?)",
                        (target["id"], kind, path),
                    )

        self.db.write(_record)
        return {
            "partner_id": target["id"],
            "title": target["title"],
            "granted": new,
            "unchanged": already,
            "allowed": list(actual),
        }

    def delete_permissions(
        self, *, requester_uuid: str, partner_title: str, paths: list[str]
    ) -> dict:
        """Revoke paths from an Antigravity conversation, and drop the intent.

        Revocation exists because granting alone cannot correct anything. A
        permission set is corrected by making the conversation match the set
        it should have, and a set that can only grow cannot be made to match
        one that shrank -- a path granted by mistake would outlive every
        attempt to withdraw it.

        A path is revoked in BOTH kinds. `paths` names filesystem locations,
        not grants, and a caller withdrawing access to a directory that means
        to leave the read half behind is doing something specific enough to
        say so by re-granting it.

        Raises:
            Rejected: `not_path_configurable` for any non-Antigravity partner;
                `no_paths` for an empty list; `permission_not_applied` if the
                remote still shows a revoked rule afterwards.
            NeedsRemote: if no Antigravity extension is configured.
        """
        self._resolve_requester(requester_uuid)
        target = self._resolve_live_partner_by_title(partner_title)
        self._require_gemini(target)
        wanted_paths = list(dict.fromkeys(paths or []))
        if not wanted_paths:
            raise Rejected("no_paths", "Give at least one path to revoke.")

        ext = self._extension_for(
            "gemini_",
            "delete_permissions",
            "Revoking a path from a conversation requires the Antigravity extension.",
        )
        before = set(ext.get_permissions(partner_id_in_remote=target["partner_id_in_remote"]))
        candidates = [f"{kind}_file({p})" for p in wanted_paths for kind in ("read", "write")]
        present = [r for r in candidates if r in before]
        absent = [r for r in candidates if r not in before]

        actual = before
        if present:
            actual = self._apply_and_verify(
                target,
                ext,
                lambda: ext.delete_permissions(
                    partner_id_in_remote=target["partner_id_in_remote"], rules=present
                ),
                {rule: False for rule in present},
            )

        def _forget(conn: sqlite3.Connection) -> int:
            n = 0
            for path in wanted_paths:
                cur = conn.execute(
                    "DELETE FROM partner_paths WHERE partner_id = ? AND path = ?",
                    (target["id"], path),
                )
                n += cur.rowcount
            return n

        forgotten = self.db.write(_forget)
        return {
            "partner_id": target["id"],
            "title": target["title"],
            "revoked": present,
            "unchanged": absent,
            "recorded_rows_removed": forgotten,
            "allowed": list(actual),
        }

    # -- the queue -----------------------------------------------------------

    def _admit(
        self,
        conn: sqlite3.Connection,
        *,
        partner_id: int,
        caller_id: int,
        behavior: str,
        body: str,
        store: bool,
        from_partner: int,
    ) -> tuple[int | None, int]:
        """Store the message if its label says so, then push it, or refuse on cap.

        Runs inside a `write()` transaction, so a refusal here rolls back the
        `messages` row as well -- a message that was never queued must not be
        readable as though it had been delivered.
        """
        message_id = None
        if store:
            cur = conn.execute(
                "INSERT INTO messages (from_partner, to_partner, behavior, body) "
                "VALUES (?, ?, ?, ?)",
                (from_partner, partner_id, behavior, body),
            )
            message_id = cur.lastrowid

        working = self.slots.outstanding(partner_id, caller_id, behavior)
        cur = conn.execute(
            _ADMIT_SQL,
            {
                "pid": partner_id,
                "cid": caller_id,
                "behavior": behavior,
                "body": body,
                "mid": message_id,
                "working": working,
            },
        )
        if cur.rowcount == 0:
            cap = conn.execute(
                "SELECT max_outstanding FROM label_caps WHERE behavior = ?", (behavior,)
            ).fetchone()["max_outstanding"]
            raise Rejected(
                "over_queue",
                f"This caller already has {cap} {behavior} task(s) outstanding against this "
                "partner, counting the one being worked on.",
                next_call="Wait for one to complete rather than retrying immediately.",
            )
        depth = conn.execute(
            "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id = ?", (partner_id,)
        ).fetchone()["n"]
        return message_id, depth

    def _stored(self, behavior: str) -> bool:
        """Whether this label is written to `messages`, per `label_caps.stored`."""
        row = self.db.read_one("SELECT stored FROM label_caps WHERE behavior = ?", (behavior,))
        return bool(row["stored"]) if row is not None else False

    def send(
        self,
        *,
        requester_uuid: str,
        queried_partner_title: str,
        message: str,
        behavior: str,
    ) -> dict:
        """Push a labelled message into a Partner's queue, then let the queue run.

        There is no direction parameter and no queue to choose. Every message
        is a push; the label decides how urgently it is taken up relative to
        whatever the Partner is already doing.

        Raises:
            Rejected: `unknown_behavior`, `idle_not_sendable`,
                `source_cannot_send`, `research_not_accepted`,
                `research_cannot_flow_upward`, `research_needs_a_forward_handshake`,
                `no_handshake`, `over_queue`. Any `Rejected` or `NeedsRemote` raised
                by the `advance()` call at the end carries `already_committed =
                True` -- the queue push above it already landed, so that failure
                is not grounds to retry the whole call.
            NeedsRemote: after the message is admitted, if no matching
                extension can deliver it. Admission stands; delivery does not.
        """
        requester = self._resolve_requester(requester_uuid)
        target = self._resolve_live_partner_by_title(queried_partner_title)

        if behavior not in BEHAVIORS:
            raise Rejected(
                "unknown_behavior",
                f"{behavior!r} is not a recognized behavior label; expected one of {BEHAVIORS}.",
            )
        if behavior == INTERRUPT_BEHAVIOR:
            # [IDLE] is the vehicle for a forced interruption and nothing
            # else. Accepting it here would be a second route to interrupting
            # a partner -- one that skips the same-project check and never
            # stops the remote, leaving it working on something it has just
            # been told to abandon.
            raise Rejected(
                "idle_not_sendable",
                f"{INTERRUPT_BEHAVIOR} is not a message; it is a hold, and no tool sends "
                "one. A Partner is parked by the Polling Server when it is waiting on an "
                "answer, or by a human -- stopping a Partner mid-turn is never an agent's "
                "decision, so there is nothing here for you to call instead.",
            )

        req_cap = self._source_cap(self._partner_type(requester))
        if req_cap is not None and not req_cap["can_send"]:
            raise Rejected(
                "source_cannot_send",
                f"A {req_cap['source_prefix']} partner never originates a message; it is a "
                "knowledge base with no agent behind it.",
            )
        tgt_cap = self._source_cap(self._partner_type(target))
        if behavior == "[RESEARCH]":
            if tgt_cap is not None and not tgt_cap["accepts_research"]:
                raise Rejected(
                    "research_not_accepted",
                    f"A {tgt_cap['source_prefix']} partner does not take delegated work; it "
                    "answers [QUERY] about what it already holds.",
                )
            # Delegated work travels down the hierarchy or sideways, never up.
            # A lower agent handing [RESEARCH] to a higher one would be
            # reassigning its own director's work. Every other label travels
            # freely, which is what lets an answer, an error, or a report come
            # back.
            if self._layer(requester) > self._layer(target):
                raise Rejected(
                    "research_cannot_flow_upward",
                    "[RESEARCH] delegates work downward. This partner sits above the "
                    "requester in the hierarchy.",
                    next_call="Use [QUERY] to ask it something, or [TRUTHFUL-REPORT] to "
                    "report back.",
                )

        # Whether this message travels back ALONG a handshake rather than out
        # along one. Only the park below reads it, and it is False for a target
        # that needs no handshake at all (nlm_), where there is no direction to
        # speak of -- and nothing behind it that could be waiting on an answer.
        travelling_up = False
        if self._needs_handshake(target):
            # A handshake row only ever gets written in the direction an
            # orchestrator claims it (see `handshake`'s `requester_not_orchestrator`
            # check): `from_partner` is always the orchestrator, `to_partner`
            # always the worker it directs. A worker replying to that Caller has
            # no row of its own to point back with -- it was never the one who
            # called `handshake` -- so requiring the forward row for every label
            # would make every reply direction permanently unreachable. Accepting
            # the reverse row for an ANSWER opens nothing new: it is still the
            # same pair, already joined by the same orchestrator, and the only
            # thing this adds is a reply travelling back along a link that
            # already exists.
            #
            # `[RESEARCH]` is not a reply, and the layer rule alone does not
            # stand in for the row here: `research_cannot_flow_upward` refuses
            # only a STRICTLY higher target, and a `project-orchestrator` sits at
            # the SAME layer as the plain `science_` worker it directs (both 2 in
            # `agent_layers`). Accepting the reverse row for every label would
            # therefore hand that worker exactly what requiring the forward row
            # used to prevent -- the ability to delegate `[RESEARCH]` back to its
            # own director -- as a side effect of opening the answer direction.
            # So the two directions are told apart: an answer travels back along
            # a handshake, but delegated work only ever travels ALONG it in the
            # direction the orchestrator claimed, never back.
            forward_row = self.db.read_one(
                "SELECT id FROM handshakes WHERE from_partner = ? AND to_partner = ?",
                (requester["id"], target["id"]),
            )
            if forward_row is None:
                reverse_row = self.db.read_one(
                    "SELECT id FROM handshakes WHERE from_partner = ? AND to_partner = ?",
                    (target["id"], requester["id"]),
                )
                travelling_up = reverse_row is not None
                if reverse_row is None:
                    raise Rejected(
                        "no_handshake",
                        "No handshake exists from the requester to this partner.",
                        next_call="Call handshake first.",
                    )
                if behavior == "[RESEARCH]":
                    raise Rejected(
                        "research_needs_a_forward_handshake",
                        "A handshake exists between these two, but only in the reverse "
                        "direction -- this partner is the one who claimed it, not the one it "
                        "was claimed against. An answer may travel back along that row; "
                        "[RESEARCH] delegates NEW work, and delegated work only travels a "
                        "handshake in the direction an orchestrator claimed it.",
                        next_call="If this partner is genuinely meant to receive delegated "
                        "work, have an orchestrator call handshake naming it as the target, "
                        "in that direction.",
                    )

        store = self._stored(behavior)
        lock = self.slots.lock_for(target["id"])

        def _push(conn: sqlite3.Connection) -> tuple[int | None, int]:
            return self._admit(
                conn,
                partner_id=target["id"],
                caller_id=requester["id"],
                behavior=behavior,
                body=message,
                store=store,
                from_partner=requester["id"],
            )

        with lock:
            message_id, depth = self.db.write(_push)

        # Admission is local and is now committed -- worth a record on its
        # own, before delivery is even attempted: a `NeedsRemote` or
        # `Rejected` raised by the `advance()` call below leaves this the only
        # trace that the message was accepted at all.
        logger.info(
            "admitted %s for %s (queue depth now %s)", behavior, target["title"], depth
        )

        # Admission is local and is now committed. Delivery never is: it goes
        # through advance(), which is also what the drain thread calls, so
        # there is one implementation of "compare the head against the working
        # slot and act" rather than one per caller.
        result = {
            "message_id": message_id,
            "behavior": behavior,
            "queue_depth": depth,
            "partner_id": target["id"],
        }
        # A Partner that has just raised a question upward stops and waits.
        #
        # Without this, the next queued message reaches an agent that is
        # blocked on an unanswered question, and it interleaves the two: the
        # work it cannot finish and whatever arrived next, in one context, with
        # nothing marking where one ends. The hold is what keeps the unfinished
        # work paused and intact until the answer comes.
        #
        # `travelling_up` is what makes this precise, and it is the condition
        # that must not be dropped. A Caller dispatching a routine [QUERY] down
        # to a worker holds a working slot too, and stopping ITSELF every time
        # it asked a worker anything would halt the orchestrator that drives
        # everything. Only a message travelling back along a handshake -- which
        # is a worker answering the Caller that claimed it -- is a Partner
        # raising a question about work it is in the middle of.
        #
        # The hold ends by itself: anything displaces an [IDLE], so the
        # Caller's answer takes the slot and the paused work resumes behind it.
        # Nothing has to remember to release it.
        if (
            behavior in _RAISES_UPWARD
            and travelling_up
            and self.slots.get(requester["id"]) is not None
        ):
            self._park(
                requester, behavior=behavior,
                target_title=target["title"], waiting_on_id=target["id"],
            )

        try:
            advanced = self.advance(partner_id=target["id"])
        except RemoteFailure as exc:
            # A remote that exists and did not work -- a missing binary, a
            # refused connection, an HTTP error. Marked for the same reason a
            # Rejected is: the push above already committed, and `advance` has
            # already put the task back in the queue. Left unmarked, the tool
            # layer renders it with "send the work again", and the caller does
            # -- producing the exact double-send the flag exists to prevent.
            exc.already_committed = True
            raise
        except (Rejected, NeedsRemote) as exc:
            # The push above already committed; only what happens next --
            # delivery -- failed. Marking the SAME exception object (not a new
            # one, which would drop the code and the traceback) is what lets
            # whoever renders this tell "nothing happened, retry" from "it
            # happened, only delivery didn't" -- without the flag, a retry
            # burns the caller's cap re-doing work the system already holds.
            exc.already_committed = True
            raise
        result.update(advanced or {"delivered": None})
        return result

    def advance(self, *, partner_id: int) -> dict | None:
        """Compare the queue head against the working slot, swap if it wins, deliver.

        The single implementation of the pushing mechanism. `send`,
        `interrupt_partner`, and the Polling Server's drain thread all call
        this; none of them reimplements it.

        What happens, in order, under this partner's slot lock:

        1. Read the queue head in priority order.
        2. If the slot is empty, the head is promoted into it.
        3. If the head's priority strictly beats the working task's, the
           working task is marked `in_process` and pushed back, and the head
           takes the slot. Strictly, not "or equal": an arriving `[QUERY]`
           must not displace a `[QUERY]` already being answered, or two
           callers could ping-pong a partner between their questions and
           neither would ever get an answer.
        4. Otherwise nothing moves and this returns None.
        5. The promoted task is rendered into a prompt and handed to the
           remote. A task returning from `in_process` gets the one-line resume
           prompt instead of its original body.

        Returns:
            A dict describing what moved, or None if nothing did.

        Raises:
            NeedsRemote: if no extension can deliver for this partner's
                source. The queue is left untouched -- the extension is
                resolved before anything is written.
        """
        partner = self.db.read_one(
            "SELECT * FROM partners WHERE id = ? AND archived_at IS NULL", (partner_id,)
        )
        if partner is None:
            # An archived partner can never be messaged again. Work admitted
            # before it was archived is dropped rather than delivered, and
            # dropped rather than left in place -- leaving it would mean every
            # later advance() hits this same branch forever.
            self.db.write(
                lambda conn: conn.execute(
                    "DELETE FROM message_queue WHERE partner_id = ?", (partner_id,)
                )
            )
            self.slots.clear(partner_id)
            return None

        project = self._project_by_id(partner["project_id"])
        lock = self.slots.lock_for(partner_id)
        with lock:
            label = self.db.read_one(_HEAD_LABEL_SQL, {"pid": partner_id})
            if label is None:
                return None
            head = self.db.read_one(
                _HEAD_ROW_SQL, {"pid": partner_id, "behavior": label["behavior"]}
            )
            if head is None:
                return None
            working = self.slots.get(partner_id)
            head_priority = head["priority"]
            # An [IDLE] in the working slot is a hold, not a task. It has the
            # highest priority for ENTERING the slot -- that is how a forced
            # interruption works at all -- and no claim on staying there: the
            # partner is stopped and waiting, and the next thing to arrive is
            # what it was waiting for. Comparing priorities here would make
            # the interruption permanent, since nothing outranks [IDLE].
            holding = working is not None and working["behavior"] == INTERRUPT_BEHAVIOR
            if working is not None and not holding and head_priority >= working["priority"]:
                return None

            ext = self._extension_for(
                project["source_prefix"],
                "deliver_message",
                f"The queue head for partner {partner_id} is ready to run; handing it to the "
                "remote still requires a matching remote extension.",
            )

            if working is not None and not holding:
                # Displacement, not arrival. Stop the remote BEFORE touching
                # the queue, so a stop that fails leaves everything exactly as
                # it was and the next advance() simply tries again. Doing it
                # after the swap would leave the displaced task in the queue
                # AND in the slot -- one task, two places, and no way for a
                # later read to tell which is real. And it must happen before
                # delivery either way: an agent handed a second instruction
                # while still executing the first interleaves them.
                #
                # A remote that CANNOT be cancelled is a different matter from
                # one whose cancellation failed. Claude Science has no usable
                # interrupt at all -- its only route needs an execution id no
                # other call returns -- so `stop_remote_execution` refuses by
                # design, every time. Treating that refusal as an error would
                # mean no science_ Partner could ever be displaced, and the
                # refusal would propagate to whoever called `send` after their
                # message was already committed.
                #
                # So a designed refusal is recorded and the swap proceeds. The
                # consequence is real and worth naming: the old turn keeps
                # running on the remote while the new one is delivered, and the
                # agent sees both. That is the honest behaviour for a remote
                # with no cancel; pretending otherwise would be worse.
                try:
                    ext.stop_remote_execution(
                        partner_id_in_remote=partner["partner_id_in_remote"],
                        reason=f"displaced by a higher-priority {head['behavior']}",
                    )
                except Rejected as exc:
                    if exc.code not in _UNCANCELLABLE:
                        raise
                    self.uncancelled_displacements.append(
                        (partner_id, working["behavior"], head["behavior"])
                    )

            def _swap(conn: sqlite3.Connection) -> None:
                conn.execute("DELETE FROM message_queue WHERE id = ?", (head["id"],))
                # A displaced [IDLE] is discarded rather than requeued: it has
                # already done its whole job, which was to stop the partner.
                # Requeuing it would stop the partner again the moment it
                # resumed.
                if working is not None and not holding:
                    conn.execute(
                        "INSERT INTO message_queue "
                        "(partner_id, caller_id, behavior, body, in_process, message_id, "
                        "summary_phase, origin_behavior) "
                        "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                        (
                            partner_id,
                            working["caller_id"],
                            working["behavior"],
                            working["body"],
                            working["message_id"],
                            # Carried through so a displaced summary phase is
                            # still recognisable as one on resume -- otherwise
                            # it comes back as an ordinary [TRUTHFUL-REPORT]
                            # with no marker saying its Caller is still owed
                            # the report (see _render) and no marker saying it
                            # still counts against the [RESEARCH] cap (see
                            # WorkingSlots.outstanding / _ADMIT_SQL).
                            int(bool(working.get("summary_phase"))),
                            working.get("origin_behavior"),
                        ),
                    )

            self.db.write(_swap)

            task = dict(head)
            self.slots.clear(partner_id)

            # An [IDLE] is a hold, and nothing is said to a held agent. Its
            # remote was stopped above, before the swap; typing a paragraph at
            # a stopped agent hands it something to act on when the entire
            # point of the hold is that it should be doing nothing. So the
            # slot is taken and no prompt is rendered or delivered.
            #
            # It also means an [IDLE] cannot fail to deliver, which is why
            # there is no requeue path for one -- and none is wanted: a hold
            # that had to be retried would stop the partner twice.
            if task["behavior"] == INTERRUPT_BEHAVIOR:
                task["remote_call_id"] = None
                task["prompt"] = None
                task["started_at"] = _now()
                self.slots.set(partner_id, task)
                return {
                    "delivered": None,
                    "held": True,
                    "resumed": False,
                    "displaced": None if working is None or holding else working["behavior"],
                    "remote_call_id": None,
                }

            prompt = self._render(task, partner)
            try:
                remote_call_id = ext.deliver_message(
                    partner_id_in_remote=partner["partner_id_in_remote"],
                    behavior=task["behavior"],
                    body=prompt,
                )
            except Exception:
                # Delivery failed after the row was already removed. Put the
                # task back -- marked in_process, because from the queue's
                # point of view it is a task that started and stopped -- and
                # let the caller see the remote's own error. Silently dropping
                # it is the one outcome nothing downstream could detect.
                self._requeue(task)
                raise
            task["remote_call_id"] = remote_call_id
            task["prompt"] = prompt
            # The second half of the latency measurement, kept beside the task
            # it describes rather than in a queue row that no longer exists.
            task["started_at"] = _now()
            self.slots.set(partner_id, task)
            displaced = None if working is None or holding else working["behavior"]
            if displaced is None:
                logger.info("delivered %s to partner %s", task["behavior"], partner_id)
            else:
                logger.info(
                    "delivered %s to partner %s, displacing %s",
                    task["behavior"], partner_id, displaced,
                )
            return {
                "delivered": task["behavior"],
                "resumed": bool(task["in_process"]),
                "displaced": displaced,
                "remote_call_id": remote_call_id,
            }

    def _requeue(self, task: dict) -> None:
        """Put a task back in the queue, paused. Used when delivery fails."""

        def _put(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO message_queue "
                "(partner_id, caller_id, behavior, body, in_process, message_id, "
                "summary_phase, origin_behavior) "
                "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    task["partner_id"],
                    task["caller_id"],
                    task["behavior"],
                    task["body"],
                    task["message_id"],
                    # Same reason as the _swap insert above: a failed delivery
                    # must not be the second, quieter way a summary phase loses
                    # its markers and comes back as an ordinary report.
                    int(bool(task.get("summary_phase"))),
                    task.get("origin_behavior"),
                ),
            )

        self.db.write(_put)

    def _render(self, task: dict, partner: sqlite3.Row) -> str:
        """Wrap a queued message in the prompt the remote actually receives.

        A queue row holds the Caller's raw text. What reaches an agent is that
        text inside a template that names who is speaking and what the reply
        must contain -- see `messaging_core/templates.py`, and the project note
        "Prompt templates" it mirrors.
        """
        caller = self.db.read_one(
            "SELECT title FROM partners WHERE id = ?", (task["caller_id"],)
        )
        caller_title = caller["title"] if caller is not None else "an unknown caller"
        # Checked BEFORE the general in_process branch below, for the same
        # reason [IDLE] is: a displaced-and-resumed summary phase is not an
        # ordinary paused task, and the general branch renders it wrong. Its
        # row's body is deliberately the ORIGINAL request (see
        # begin_summary_phase), not the instruction to summarize -- so
        # falling through to resume_displaced would hand back "resume your
        # previous [TRUTHFUL-REPORT]", which names nothing the agent is
        # holding, instead of asking for the summary again.
        if task.get("summary_phase"):
            return templates.truthful_report_request(
                caller_title=caller_title, original_request=task["body"]
            )
        if task["in_process"]:
            return templates.resume_displaced(behavior=task["behavior"])
        if task["behavior"] == "[RESEARCH]":
            paths = self._recorded_paths(partner["id"])
            return templates.research_dispatch(
                caller_title=caller_title,
                body=task["body"],
                read_paths=paths["read"],
                write_paths=paths["write"],
                partner_uuid=partner["uuid"],
                partner_title=partner["title"],
            )
        # A notebook is asked in its own terms. `relay` is written for an
        # agent -- it names a speaker and closes with the call the recipient
        # may answer with -- and two of those three mean nothing to a source
        # that holds documents and never acts. See `templates.notebook_query`
        # for what replaces them, and why it carries no identity block.
        if task["behavior"] == "[QUERY]" and self._partner_type(partner) == "nlm_":
            return templates.notebook_query(
                caller_title=caller_title,
                source=partner["partner_id_in_remote"],
                body=task["body"],
            )
        # A [TRUTHFUL-REPORT] reaching this point is always a summary being
        # DELIVERED, never a request for one -- a request still in progress
        # was already caught by the summary_phase branch above, whether it is
        # fresh (rendered by begin_summary_phase, never queued) or a displaced
        # copy resuming (queued, but marked). That is what keeps one label
        # from meaning two opposite things depending on which end of the
        # exchange is reading it.
        return templates.relay(
            caller_title=caller_title,
            behavior=task["behavior"],
            body=task["body"],
            partner_uuid=partner["uuid"],
            partner_title=partner["title"],
        )

    def begin_summary_phase(self, *, partner_id: int) -> str | None:
        """Turn the working [RESEARCH] task into its own summary phase, in place.

        A research round trip is two exchanges against one remote: do the
        work, then report on it. The second is NOT pushed into the queue as a
        new message -- it is the same task, still in the working slot, asked a
        second thing. Two reasons that matters:

        A queued `[TRUTHFUL-REPORT]` would be ambiguous. The label would mean
        "summarize this" travelling one way and "here is the summary"
        travelling the other, and a queue row has nothing on it that says
        which. Keeping the request out of the queue leaves the label with one
        meaning everywhere it can be seen.

        And the summary is protected while it is being written. The task's
        effective priority is RAISED to `[TRUTHFUL-REPORT]`'s for the second
        phase, and the effect is that from then on **arriving messages queue
        rather than reach the agent**: `[QUERY]`, `[ERROR]`,
        `[MESSAGE-RESPONSE]` and `[RESEARCH]` all wait, because `advance` only
        displaces on a strictly lower priority number and only `[IDLE]` has
        one.

        That blocking is the whole point. It is stated as a counterfactual
        because the counterfactual is what makes it worth doing: a summary
        written while other traffic WAS reaching the same context would
        summarize the traffic.

        Returns:
            The prompt to hand to the remote, or None if the slot is empty or
            is not holding a `[RESEARCH]` task.
        """
        with self.slots.lock_for(partner_id):
            task = self.slots.get(partner_id)
            if task is None or task["behavior"] != "[RESEARCH]":
                return None
            caller = self.db.read_one(
                "SELECT title FROM partners WHERE id = ?", (task["caller_id"],)
            )
            caller_title = caller["title"] if caller is not None else "an unknown caller"
            prompt = templates.truthful_report_request(
                caller_title=caller_title, original_request=task["body"]
            )
            promoted = self.db.read_one(
                "SELECT priority FROM label_caps WHERE behavior = '[TRUTHFUL-REPORT]'"
            )
            task = dict(task)
            task["behavior"] = "[TRUTHFUL-REPORT]"
            task["priority"] = promoted["priority"]
            task["prompt"] = prompt
            # An explicit marker, not an inference from the label. A
            # [TRUTHFUL-REPORT] in the working slot can have got there two ways:
            # promoted from a [RESEARCH] here, which still owes its Caller the
            # summary; or sent directly by an agent, which owes nothing back
            # because it IS the report. Telling them apart by label alone means
            # a directly-sent report replies with another report, and the pair
            # never stops.
            task["summary_phase"] = True
            # The label being run is now [TRUTHFUL-REPORT], but the cap this
            # task was admitted under was [RESEARCH]'s -- it is still the same
            # delegated work, just under a second instruction. Recording the
            # origin is what lets a displaced-and-resumed copy keep counting
            # against that cap instead of escaping it for as long as the
            # summary takes (see WorkingSlots.outstanding / _ADMIT_SQL).
            task["origin_behavior"] = "[RESEARCH]"
            # The body stays the ORIGINAL request. If this phase is later
            # displaced and resumed, the row that goes back into the queue
            # carries the request the summary is supposed to be about --
            # not the instruction to summarize, which would summarize itself.
            self.slots.set(partner_id, task)
            return prompt

    def reply_behavior(self, behavior: str) -> str | None:
        """What a finished task carrying `behavior` sends back, or None for nothing."""
        row = self.db.read_one(
            "SELECT reply_behavior FROM label_caps WHERE behavior = ?", (behavior,)
        )
        return row["reply_behavior"] if row is not None else None

    def report_back(
        self, *, to_partner_id: int, from_partner_id: int, behavior: str, body: str
    ) -> dict:
        """Push a finished Partner's answer into the Caller's own queue.

        Not `send`: there is no requester holding a UUID here, and the
        handshake was already established in the direction that made this
        exchange possible. What this does keep is the cap and the storage
        rule, because those are about the Caller's queue rather than about
        who is allowed to talk to whom.

        What it also keeps -- and must -- is that `behavior` is something a
        Partner can REPORT rather than something it can delegate. `[RESEARCH]`
        is delegation and `[IDLE]` is a hold; neither is a report, and admitting
        either here would make this method a hole in rules that `send` enforces.
        `send` refuses `[RESEARCH]` travelling upward, and anything holding a
        `MessagingCore` could otherwise route the identical message through here
        and land it in a superior's queue. It is not reachable from the tool
        surface, but "not currently reachable" is not a rule.

        Everything else is reportable, and all four are reached in practice: a
        `[MESSAGE-RESPONSE]` or `[TRUTHFUL-REPORT]` answering a finished task, an
        `[ERROR]` raised on a Partner's behalf when it stops on a permission it
        does not hold, and a `[QUERY]` when it needs context only the Caller has.
        The last two are the cases a Partner cannot report itself -- an agent
        stopped on a prompt is not running, and nothing else is watching.

        The recipient is checked live before anything is inserted, and a dead
        one is a quiet non-delivery -- `delivered: False` in the result --
        rather than the `sqlite3.IntegrityError` `message_queue`'s own foreign
        key would otherwise raise. `archive_sessions` and `delete_partner` can
        each make `to_partner_id` disappear (archived, or gone outright) after
        the task this is reporting on was already handed out, and the Polling
        Server calls this AFTER it has released the working slot the task
        held. A raise at that point cannot be recovered from where it is
        caught: the slot is already empty, so the answer cannot be put back
        there, and the drain thread that called this is left stranded on an
        exception nobody reads. Returning quietly instead lets that caller
        move on -- the Caller was already told its work is gone, by whichever
        of archiving or deleting caused this, so there is nothing left for
        this particular answer to do.

        Raises:
            Rejected: `not_reportable` if `behavior` is `[RESEARCH]` or `[IDLE]`.
        """
        if behavior in _NOT_REPORTABLE:
            raise Rejected(
                "not_reportable",
                f"{behavior!r} cannot be reported back: it is "
                + ("delegated work, and delegation is what send is for."
                   if behavior == "[RESEARCH]"
                   else "a hold, not a message."),
            )
        if behavior not in BEHAVIORS:
            raise Rejected(
                "unknown_behavior",
                f"{behavior!r} is not a recognized behavior label; expected one of {BEHAVIORS}.",
            )
        store = self._stored(behavior)

        def _push(conn: sqlite3.Connection) -> tuple[int | None, int, bool]:
            # Checked INSIDE the same write transaction that would otherwise
            # insert, not as a separate read before it -- a separate read
            # leaves a window between "was live" and "still is" for
            # delete_partner's DELETE to land in, which is exactly the
            # read-then-write shape _ADMIT_SQL's own comment refuses to use
            # for the cap check, for the same reason.
            live = conn.execute(
                "SELECT id FROM partners WHERE id = ? AND archived_at IS NULL", (to_partner_id,)
            ).fetchone()
            if live is None:
                return None, 0, False
            message_id, depth = self._admit(
                conn,
                partner_id=to_partner_id,
                caller_id=from_partner_id,
                behavior=behavior,
                body=body,
                store=store,
                from_partner=from_partner_id,
            )
            return message_id, depth, True

        with self.slots.lock_for(to_partner_id):
            message_id, depth, delivered = self.db.write(_push)
        if not delivered:
            # The row may still exist (archived) or may be gone outright
            # (deleted) -- either way the title, if there still is one, is
            # more useful to whoever reads the log than a bare id.
            target = self.db.read_one("SELECT title FROM partners WHERE id = ?", (to_partner_id,))
            logger.info(
                "could not report back to partner %s: gone or archived",
                target["title"] if target is not None else to_partner_id,
            )
        return {
            "message_id": message_id,
            "queue_depth": depth,
            "behavior": behavior,
            "delivered": delivered,
        }

    def release(self, *, partner_id: int) -> dict | None:
        """Empty the working slot because the remote finished its turn.

        Returns what the slot held, or None if it was already empty. Calling
        it twice is harmless, which matters because a completion can be
        observed by a drain thread and by a push notification at the same
        time.
        """
        with self.slots.lock_for(partner_id):
            return self.slots.clear(partner_id)

    def working_task(self, *, partner_id: int) -> dict | None:
        """The task this Partner is being worked on, or None."""
        return self.slots.get(partner_id)

    def _park(self, requester, *, behavior: str, target_title: str,
              waiting_on_id: int) -> None:
        """Stop `requester` until the question it just asked is answered.

        The hold is pushed through the same door as everything else -- admitted
        as an `[IDLE]` and promoted by `advance` -- rather than written into the
        slot directly. `advance` is the one place a swap happens, and it is
        what marks the displaced task `in_process` so it resumes with its body
        intact; a slot written behind its back would strand the work it
        replaced.

        The `[IDLE]` is attributed to the Partner being waited ON, which is
        both true and the only value the schema permits: `message_queue` has
        `CHECK (caller_id <> partner_id)`, so a Partner cannot be recorded as
        the caller of its own hold.

        Failure here is logged and swallowed, deliberately. By this point the
        question is already committed to the Caller's queue, and the two
        outcomes are not symmetric: a hold that did not take leaves a Partner
        working while it waits, which the next arriving message will correct
        anyway, whereas raising would fail a `send` whose message has already
        been accepted and send the Caller looking for a message it will find.
        """
        body = (
            f"Waiting on {target_title} to answer the {behavior} just sent. "
            "The work paused behind this hold resumes when that answer arrives."
        )

        def _push(conn: sqlite3.Connection) -> tuple[int | None, int]:
            return self._admit(
                conn,
                partner_id=requester["id"],
                caller_id=waiting_on_id,
                behavior=INTERRUPT_BEHAVIOR,
                body=body,
                store=False,
                from_partner=waiting_on_id,
            )

        try:
            with self.slots.lock_for(requester["id"]):
                self.db.write(_push)
            self.advance(partner_id=requester["id"])
        except (Rejected, NeedsRemote) as exc:
            logger.warning(
                "could not park %s after it raised a %s: %s",
                requester["title"], behavior, exc,
            )

    def interrupt_partner(self, *, requester_uuid: str, partner_title: str, reason: str) -> dict:
        """Stop a Partner by pushing an `[IDLE]` into its queue.

        This is not a special path through the queue -- it is a normal push
        that wins. `[IDLE]` holds the highest priority in `label_caps`, so it
        takes the working slot by construction, which means interruption and
        ordinary delivery are the same mechanism and there is no second code
        path to keep in step with the first.

        The displaced task is marked `in_process` and stays in the queue, so
        the Partner goes back to it once the interruption clears.

        Raises:
            Rejected: `different_project`, `not_executable`.
            NeedsRemote: if no extension can reach the partner. The `[IDLE]`
                is admitted regardless; stopping the remote is what needs one.
        """
        requester = self._resolve_requester(requester_uuid)
        target = self._resolve_live_partner_by_title(partner_title)
        if requester["project_id"] != target["project_id"]:
            raise Rejected("different_project", "Both partners must belong to the same project.")

        project = self._project_by_id(target["project_id"])
        self._require_executable(project)

        lock = self.slots.lock_for(target["id"])

        def _push(conn: sqlite3.Connection) -> tuple[int | None, int]:
            return self._admit(
                conn,
                partner_id=target["id"],
                caller_id=requester["id"],
                behavior=INTERRUPT_BEHAVIOR,
                body=reason,
                store=False,
                from_partner=requester["id"],
            )

        with lock:
            _, depth = self.db.write(_push)

        outcome = self.advance(partner_id=target["id"]) or {}
        return {
            "partner_id": target["id"],
            "behavior": INTERRUPT_BEHAVIOR,
            "queue_depth": depth,
            "displaced": outcome.get("displaced"),
            "remote_call_id": outcome.get("remote_call_id"),
        }
