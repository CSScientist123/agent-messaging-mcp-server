# Messaging MCP: reference

**Audience.** An engineer maintaining or debugging this system who has not seen it before, and who needs to know exactly what a call does, what it returns, why it refused, and what to do next.

**Scope.** The 18 messaging capabilities in `messaging_core/core.py`; the exception vocabulary in `messaging_core/errors.py`; behavior labels in `messaging_core/labels.py`; the working slot in `messaging_core/slots.py`; the prompt templates in `messaging_core/templates.py`; the response-text helpers in `messaging_core/responses.py`; the SQLite wrapper in `messaging_core/db.py`; the schema in `schema/schema.sql`; the remote boundary in `extension/base.py`; the MCP tool surface in `mcp_server/server.py`; and the Polling Server in `polling/server.py`.

**Non-scope.** Why the system is shaped this way, how to operate or deploy it, and worked tutorials. Those belong in sibling documents. This document is consulted for a fact, not read start to finish.

**Assumed prior knowledge.** Python, SQLite (transactions, `PRAGMA`, triggers, `WITHOUT ROWID`), and roughly what MCP (Model Context Protocol) is — a way to expose callable tools to an agent over a defined transport.

Every fact below is drawn from the source listed above. Where the source contains something surprising, unused, or internally inconsistent, this document says so explicitly rather than smoothing it over.

---

## 1. Concepts

**Project.** A row in the `projects` table: one workspace in one remote application, identified by a server-wide-unique `title` and by the pair (`source_prefix`, `project_system_id`), which is also unique. A Project has exactly one `source_prefix` — this is the single fact that determines what kind of remote it is and what kind every Partner registered under it becomes. Created by `create_project`, which verifies `project_system_id` against the remote before the row is written.

**Partner.** A row in the `partners` table: one messaging identity — a session, sub-agent, or context source — registered under exactly one Project. A Partner has no source of its own; it takes on its Project's `source_prefix` (see `MessagingCore._partner_type`, which simply joins `partners` to `projects`). A Partner is identified externally by a `uuid` (its only identity credential, minted once at `create_partner` and never shown again) and addressed by other Partners through a free-form, server-wide-unique `title`. It carries a `descr` (at most 1200 characters), a `partner_id_in_remote` pointing at the actual object in the remote system, and an optional orchestrator role.

**Source prefix.** One of exactly four fixed strings: `nlm_`, `code_`, `science_`, `gemini_`. Each names a family of remote system and has one row in `source_caps` holding that family's live-partner cap and four booleans: whether it executes, whether it needs a handshake, whether it may send at all, and whether it accepts delegated work. Because a Project has exactly one `source_prefix` and a Partner takes on its Project's, the source prefix is the *only* type authority in the system — a title carries no type information at all.

**Layer.** A Partner's position in the delegation hierarchy, looked up in `agent_layers` by (`source_prefix`, `orchestrator_type`) with `'*'` as the source's default. Lower is higher up: `nlm_` and `code_` at 0, `bridge-scientist` at 1, `project-orchestrator` at 2, `gemini-orchestrator` at 3, `gemini_` at 4. It governs exactly one rule — `[RESEARCH]` may not travel to a lower-numbered layer — and nothing else.

**Orchestrator role.** The value of `partners.orchestrator_type`: `NULL` until claimed, then one of `"project-orchestrator"`, `"gemini-orchestrator"`, `"bridge-scientist"`, claimed once via `claim_orchestrator` and never reassigned or released. At most one live Partner per Project may hold a given role (enforced by the partial unique index `one_orchestrator_per_project_role`). Holding a role is the gate on several capabilities: initiating a handshake, deleting or archiving another Partner in the same Project, and granting or receiving gemini budget.

**Handshake.** A one-directional row in `handshakes` (`from_partner` → `to_partner`) that authorizes `send` from the first Partner to the second. `send` requires an existing handshake in that exact direction, except toward an `nlm_` Partner, which needs none. A reply direction is a second, independent row. Which pairings a handshake may legally connect is a fixed set of rules keyed on the two Partners' source prefixes and roles — see the `handshake` capability below.

**Behavior label.** One of five fixed markers a message carries: `[TRUTHFUL-REPORT]`, `[QUERY]`, `[ERROR]`, `[MESSAGE-RESPONSE]`, `[RESEARCH]` (`labels.BEHAVIORS`). Two of them — `[QUERY]` and `[ERROR]`, listed in `labels.BLOCKING_BEHAVIORS` — also stop the agent that sends one, until it is answered. Every per-label fact lives in the `label_caps` table rather than in code: its priority, how many one Caller may have outstanding, whether it is stored in `messages`, and what a finished task carrying it replies with. There is no direction in a label — any party may send any label either way.

**Priority queue.** One `message_queue` per Partner, holding what is waiting. The head is read in two steps: which **label** runs next (lowest `label_caps.priority`, then a label with any unpaused work over one whose rows are all paused, then arrival), and then which **row** of that label (paused first, then arrival). Two steps because `in_process` breaks ties only *within* a label, which one `ORDER BY` cannot say. A row is DELETED when its task is promoted — the queue holds what is waiting, not what is running.

**Working slot.** The one task a Partner is actually being worked on, held in process memory (`messaging_core/slots.py`) and never in SQLite. `MessagingCore.advance` is the single place the queue head is compared against it and swapped. A displaced task is marked `in_process = 1` and pushed back.

**Poll task.** Not a table. The concept survives the removal of `polling_tasks`: a poll task is a message that is either queued or in the working slot. There is no third place for it to be, and therefore no state column that can disagree with where it actually is.

**Project extension.** A row in `project_extension` declaring two Projects parts of one effort, so that Partners under them may handshake across the Project boundary — but only between two Partners holding the same orchestrator role. Stored with the lower project id first (`CHECK (project_a < project_b)`) so the pair has exactly one row.

**Extension.** A concrete subclass of `extension.base.RemoteExtension` that lets `MessagingCore` and `PollingServer` act on one family of remote system, identified by the extension's own fixed `source_prefix` attribute. A `MessagingCore` holds at most one extension at a time; any capability that needs to reach a Project or Partner of a *different* `source_prefix` than the configured extension's raises `NeedsRemote`, exactly as if no extension were configured at all.

---

## 2. The 17 capabilities

Every capability below is a method on `MessagingCore` (`messaging_core/core.py`). Its MCP tool wrapper, in `mcp_server/server.py`, takes the same parameters, catches `Rejected` and `NeedsRemote`, and renders the result as a formatted string (see §5) rather than returning the raw value shown here. Two capabilities — `create_project` and `create_partner` — take no `requester_uuid` at all; they are the only two of the 18 not gated by caller identity, and consequently the only two that can never raise `unknown_requester`.

`MessagingCore` also carries six methods that are **not** client capabilities and have no MCP tool: `advance`, `release`, `working_task`, `report_back`, `begin_summary_phase`, and `reply_behavior`. They are the queue machinery the Polling Server drives, documented in §8 rather than here.

Some `Rejected` codes originate from a pre-check `SELECT` and others from catching a `sqlite3.IntegrityError` raised by the write itself. Where both exist for the same fact (for example `duplicate_partner_title`), the pre-check is what a caller normally sees; a genuine race between two concurrent callers can still surface as the generic `constraint_violation` instead, because only `create_partner`'s catch block distinguishes the live-partner-limit message from everything else.

### archive_sessions

Archives one or more of the caller's own Project's Partners by title, freeing live-partner slots; unlike most capabilities, a per-title problem is reported in the result rather than failing the whole call.

**Parameters**

| Name | Type | Required | Default | Allowed values | Description |
|---|---|---|---|---|---|
| `requester_uuid` | `str` | required | — | must name a live partner | Caller's identity. |
| `titles` | `list[str]` | required | — | exact partner titles | Titles to archive; each is resolved independently. |

**Returns** `dict`:

| Field | Type | Description |
|---|---|---|
| `archived` | `list[str]` | Titles actually archived, in input order. |
| `archived_count` | `int` | `len(archived)`. |
| `skipped` | `list[dict]` | Each `{"title": str, "reason": str}`, `reason` one of `"not_found_or_already_archived"` or `"different_project"`. |

**Rejections**

| Code | Cause | Remediation |
|---|---|---|
| `unknown_requester` | `requester_uuid` does not name a live partner. | Re-check the uuid returned at `create_partner` time. |

`different_project` and a not-found/already-archived title never raise here — the code (and title) is reported as a `skipped` entry in the successful response instead, scoped to the caller's own `project_id`.

**Remote dependency.** None. Purely local; never calls the extension.

### claim_orchestrator

Claims an orchestrator role for the caller inside one Project, once and permanently.

**Parameters**

| Name | Type | Required | Default | Allowed values | Description |
|---|---|---|---|---|---|
| `requester_uuid` | `str` | required | — | must name a live partner | Caller's identity. |
| `project_id` | `int` | required | — | an existing project id | The Project the role is claimed within; must equal the requester's own `project_id`. |
| `orchestrator_type` | `str` | required | — | `"project-orchestrator"`, `"gemini-orchestrator"`, `"bridge-scientist"` | The role to claim. |

**Returns** `dict`: `{"partner_id": int, "project_id": int, "orchestrator_type": str}`.

**Rejections**

| Code | Cause | Remediation |
|---|---|---|
| `unknown_requester` | `requester_uuid` not live. | Re-check the uuid. |
| `invalid_orchestrator_type` | Not one of the three role strings. | Use one of the three listed values. |
| `wrong_project` | Requester's `project_id` ≠ the given `project_id`. | Pass the project the requester actually belongs to. |
| `already_has_role` | Requester already has a non-`NULL` `orchestrator_type`. | Do not retry; use the role already held. |
| `gemini_orchestrator_requires_science_project` | `orchestrator_type == "gemini-orchestrator"` but the Project's `source_prefix != "science_"`. | Claim this role only from a Partner of a `science_` project. |
| `role_already_claimed` | Another live Partner in this Project already holds this exact role (unique-index conflict on `(project_id, orchestrator_type)`). | Use the existing holder, or claim a different role. |

**Remote dependency.** None.

### create_partner

Registers a new Partner under an existing Project, after verifying `partner_id_in_remote` names a real object in that Project's remote.

**Parameters**

| Name | Type | Required | Default | Allowed values | Description |
|---|---|---|---|---|---|
| `project_id` | `int` | required | — | an existing project id | The Project this Partner belongs to. |
| `title` | `str` | required | — | any string, server-wide unique | The address other capabilities reach this Partner by. |
| `partner_id_in_remote` | `str` | required | — | must verify against the remote | The remote-side identifier for this Partner. |
| `descr` | `str` | required | — | ≤ 1200 characters | Free-text description; also backs `search_partner`'s preview. |
| `uuid` | `str \| None` | optional | `None` | any string | Assign this identity instead of generating a fresh one. |

**Returns** `dict`: `{"id": int, "uuid": str, "title": str, "project_id": int}`. This is the one place a `uuid` is ever returned by this API — it is the freshly minted credential for whoever becomes this Partner.

**Rejections**

| Code | Cause | Remediation |
|---|---|---|
| `no_such_project` | No project with this `project_id`. Note: this call site attaches no `next_call`, unlike `delete_project`'s. | Call `search_project` to find or confirm the correct `project_id`. |
| `descr_too_long` | `len(descr) > 1200`. | Shorten `descr` and resubmit. |
| `duplicate_partner_title` | `title` already used (archived titles count). | Choose an unused title. |
| `partner_id_in_remote_taken` | `partner_id_in_remote` already registered to another partner here. | Use the existing partner, or a different remote id. |
| `partner_id_in_remote_not_found` | The extension's `verify_partner_id_in_remote` returned `False`. | Confirm the id exists in the remote app, then retry. |
| `live_partner_limit` | The Project is already at `source_caps.max_live_partners` live partners (raised from the `partners_live_limit` trigger). | Call `archive_sessions` to free a slot. |
| `constraint_violation` | Any other `sqlite3.IntegrityError` on insert (for example, a race on `title` or `partner_id_in_remote` that slipped past the pre-checks). | Re-verify uniqueness (e.g. via `search_partner`) and retry. |

**Remote dependency.** Raises `NeedsRemote("verify_partner_id_in_remote", ...)` if no extension is configured for this Project's `source_prefix`. Has no `requester_uuid` parameter, so it never raises `unknown_requester`.

### create_project

Registers a new Project, after verifying `project_system_id` names a real project in the named remote.

**Parameters**

| Name | Type | Required | Default | Allowed values | Description |
|---|---|---|---|---|---|
| `title` | `str` | required | — | server-wide unique | The Project's title. |
| `source_prefix` | `str` | required | — | `"nlm_"`, `"code_"`, `"science_"`, `"gemini_"` | Which remote family this Project belongs to. |
| `project_system_id` | `str` | required | — | must verify against the remote | The Project's id inside the remote app. |

**Returns** `int` — the new project's row id. This is the one capability among the 15 that does not return a `dict`.

**Rejections**

| Code | Cause | Remediation |
|---|---|---|
| `invalid_source_prefix` | Not one of the four recognized prefixes. | Use one of the four listed values. |
| `duplicate_project_title` | `title` already used by another project. | Choose a different title. |
| `duplicate_project_system_id` | The pair `(source_prefix, project_system_id)` already registered. | Call `search_project` to find the existing project instead of recreating it. |
| `project_system_id_not_found` | The extension's `verify_project_system_id` returned `False`. | Confirm the id exists in the remote app, then retry. |

**Remote dependency.** Raises `NeedsRemote("verify_project_system_id", ...)` if no extension is configured for `source_prefix`. Has no `requester_uuid` parameter.

### delete_partner

Permanently deletes a live Partner by title. Irreversible, and scoped to the requester's own Project.

**Parameters**

| Name | Type | Required | Default | Allowed values | Description |
|---|---|---|---|---|---|
| `requester_uuid` | `str` | required | — | must name a live partner, and be `project-orchestrator` of the target's project | Caller's identity. |
| `partner_title` | `str` | required | — | exact title of a live partner | The partner to delete. |

**Returns** `dict`: `{"deleted_id": int, "title": str}`.

**Rejections**

| Code | Cause | Remediation |
|---|---|---|
| `unknown_requester` | `requester_uuid` not live. | Re-check the uuid. |
| `no_such_partner` | `partner_title` names no live partner. | Call `search_partner` to find the exact title. |
| `not_authorized` | Requester is not `project-orchestrator` of the target's own project. | Have that project's `project-orchestrator` perform the deletion. |
| `partner_has_dependents` | The `DELETE` violates a foreign key — in practice, `budget_grants.granted_by` references this partner and that reference has no `ON DELETE CASCADE` (see §4). | Call `archive_sessions` on this partner instead. |
| `partner_has_work_in_flight` | The target has queued rows or holds a working slot. Deletion is irreversible and, uniquely, cannot even report itself: `message_queue.caller_id` is `ON DELETE CASCADE`, so a notice attributed to the vanishing Partner is destroyed by the very `DELETE` it warns about, and attributing it to the requester collides with `CHECK (caller_id <> partner_id)` in the normal case where the requester **is** the Caller waiting. | Call `archive_sessions` instead — archiving leaves the row in place, so it can report the loss to every Caller waiting. |

**Remote dependency.** None.

### delete_project

Permanently deletes a Project and cascades to every Partner it holds.

**Parameters**

| Name | Type | Required | Default | Allowed values | Description |
|---|---|---|---|---|---|
| `requester_uuid` | `str` | required | — | must name a live partner, and be `project-orchestrator` of the target project | Caller's identity. |
| `project_title` | `str` | required | — | exact title of an existing project | The project to delete. |

**Returns** `dict`: `{"deleted_id": int, "title": str, "partners_deleted": int}`.

**Rejections**

| Code | Cause | Remediation |
|---|---|---|
| `unknown_requester` | `requester_uuid` not live. | Re-check the uuid. |
| `no_such_project` | `project_title` names no project. This call site (via `_resolve_project_by_title`) does attach a `next_call`, unlike `create_partner`'s `no_such_project`. | Call `search_project` to find the exact title. |
| `not_authorized` | Requester is not `project-orchestrator` of this project. | Have that project's `project-orchestrator` perform the deletion. |

**Remote dependency.** None. `partners.project_id` has `ON DELETE CASCADE`, so every Partner row under the project is removed by the database itself, along with everything that in turn cascades from those Partner rows.

### grant_gemini_budget

Grants, or replaces, a `gemini-orchestrator`'s budget of `gemini_` handshakes it may initiate.

**Parameters**

| Name | Type | Required | Default | Allowed values | Description |
|---|---|---|---|---|---|
| `requester_uuid` | `str` | required | — | must be `project-orchestrator` of the grantee's project | Caller's identity. |
| `grantee_uuid` | `str` | required | — | must name a live `gemini-orchestrator` in the requester's own project | Who receives the grant. |
| `budget_count` | `int` | required | — | `0`–`3` inclusive | The new budget; a later call for the same grantee replaces it outright (`ON CONFLICT ... DO UPDATE`). |

**Returns** `dict`: `{"grantee_id": int, "granted_by_id": int, "budget_count": int}`.

**Rejections**

| Code | Cause | Remediation |
|---|---|---|
| `unknown_requester` | `requester_uuid` not live. | Re-check the uuid. |
| `not_authorized` | Raised in **two** distinct situations that deliberately produce the identical code and message: (1) requester does not hold `project-orchestrator` anywhere, checked before `grantee_uuid` is even queried; (2) `grantee_uuid` does not name a live partner of the requester's own project. This is intentional information-flow control — a requester ineligible to ask is never told whether the grantee exists, is in the wrong project, or has the wrong role. | Confirm the requester is `project-orchestrator` of the grantee's own project, and that `grantee_uuid` is correct. |
| `grantee_not_gemini_orchestrator` | Grantee resolved, but its `orchestrator_type != "gemini-orchestrator"`. | Have the grantee claim `gemini-orchestrator` first. |
| `invalid_budget_count` | `budget_count` outside `0`–`3`. | Supply a value in `0`–`3`. |

**Remote dependency.** None.

### handshake

Opens a one-directional authorization from the caller to another Partner, subject to a fixed set of source/role pairing rules.

**Parameters**

| Name | Type | Required | Default | Allowed values | Description |
|---|---|---|---|---|---|
| `requester_uuid` | `str` | required | — | must name a live partner holding an orchestrator role, **unless** the requester is `code_` | Caller's identity; the handshake's `from_partner`. |
| `partner_title` | `str` | required | — | exact title of a live partner | The handshake's `to_partner`. |

**Returns** `dict`: `{"handshake_id": int, "from_partner_id": int, "to_partner_id": int, "to_partner_title": str}`.

**Rejections**, checked in this order:

| Code | Cause | Remediation |
|---|---|---|
| `unknown_requester` | `requester_uuid` not live. | Re-check the uuid. |
| `no_such_partner` | `partner_title` names no live partner. | Call `search_partner` to find the exact title. |
| `self_handshake` | Requester and target are the same partner. | Name a different target. |
| `handshake_not_needed` | Target's type is `nlm_`. | Call `send` directly; no handshake required. |
| `code_handshakes_bridge_only` | Either side is `code_` and the other is not a `science_` partner holding `bridge-scientist`. Claude code has exactly one legal counterpart. | Handshake the project's `bridge-scientist`, or nothing. |
| `bridge_single_code_partner` | Either side is `code_`, the pairing is otherwise legal, but that bridge already holds a handshake with a *different* live `code_` partner. Two would make "the Caller" ambiguous for every message reaching it. | Use the bridge already paired, or a different bridge. |
| `requester_not_orchestrator` | Requester's `orchestrator_type` is `NULL` **and** neither side is `code_`. The `code_` case is checked first and separately, because a `code_` partner can never hold one of the three roles — all three are Claude Science roles. | Claim an orchestrator role first via `claim_orchestrator`. |
| `different_project` | Both sides share a source but have different `project_id`, and no `project_extension` row links the two projects. | Call `extend_project` to link them, or handshake within one project. |
| `cross_project_requires_same_role` | The two projects ARE linked by an extension, but the two partners hold different orchestrator roles (or one holds none). An extension branches a research effort sideways; it is not a second chain of command, so a `gemini-orchestrator` cannot inherit from a `project-orchestrator` across one. | Pair two partners holding the same role. |
| `duplicate_handshake` | This exact `(from, to)` direction already exists — caught once by a pre-check `SELECT` and again by the insert's `IntegrityError` as a fallback. | Skip `handshake`; call `send` directly. |
| `no_handshake_between_gemini` | Both sides are `gemini_` **and in the same Project**. Two conversations under one gemini-orchestrator are peers; there is nothing for one to inherit from the other. | Inherit across a project extension instead, or send through the orchestrator that directs both. |
| `gemini_already_inherited` | The target Antigravity conversation is already continued by another one. A lineage is a line rather than a fork, so that "which conversation succeeds this one" has exactly one answer. | Continue the successor instead, or inherit from a conversation nothing has claimed. |
| `gemini_to_science_illegal` | Requester is `gemini_`, target is `science_`. | This direction never succeeds; only `science_ → gemini_` can bridge these two sources. |
| `bridge_handshakes_orchestrator_or_code` | Both sides `science_`, requester is `bridge-scientist`, target is not the `project-orchestrator`. The bridge hands work down to the orchestrator and takes it back; it wires up nothing else. | Target the `project-orchestrator`, or handshake a `code_` partner instead. |
| `requires_project_orchestrator` | Both sides `science_`, requester is neither `bridge-scientist` nor `project-orchestrator`. | Have that project's `project-orchestrator` initiate it. |
| `requires_gemini_orchestrator` | Requester `science_`, target `gemini_`, requester is not `gemini-orchestrator`. | Have that project's `gemini-orchestrator` initiate it. |
| `gemini_single_science_source` | Requester `science_` → target `gemini_`, and the target already has an admitted handshake from a *different* sender. | Target a different `gemini_` partner, or leave the existing pairing as-is. |
| `no_gemini_budget` | Requester `science_` → target `gemini_`, requester `gemini-orchestrator` has no `budget_grants` row at all. | Call `grant_gemini_budget` first. |
| `gemini_budget_exceeded` | Requester's granted budget is already fully used (count of existing `from_partner` → `gemini_` handshakes ≥ `budget_count`). | Call `grant_gemini_budget` to raise the budget. |

Two branches short-circuit the role-pair rules above and are allowed once their own checks pass: a legal `code_` ↔ `bridge-scientist` pairing, and a same-role pairing across a registered project extension. In both cases the role-pair rules are about who directs whom *inside* one project and have nothing to say about the pairing.

`visualizations/07-handshake-legality.png` is this table as a decision tree, in the order the code evaluates it.

**Remote dependency.** None. `handshake` never calls the extension, even though its two Partners can belong to different Projects and, in the `science_ → gemini_` case, different remotes entirely.

### read

Pages through a Partner's received `[QUERY]`/`[TRUTHFUL-REPORT]` message history, newest first.

**Parameters**

| Name | Type | Required | Default | Allowed values | Description |
|---|---|---|---|---|---|
| `requester_uuid` | `str` | required | — | must name a live partner | Caller's identity (no ownership check against `partner_title`). |
| `partner_title` | `str` | required | — | exact title of a live partner | Whose inbox to read. |
| `page` | `int` | optional | `1` | `≥ 1` | 1-indexed page number. |
| `page_size` | `int` | optional | `10` | `≥ 1`, no upper bound enforced | Rows per page. |

**Returns** `dict`: `{"partner_id": int, "title": str, "page": int, "page_size": int, "total": int, "messages": list[dict]}`, each message `{"id": int, "from_partner_title": str, "behavior": str, "body": str, "created_at": str}`. `total` counts every stored message ever received by this partner, independent of `page`. An empty `messages` list on a valid page is a normal, non-error result.

**Rejections**

| Code | Cause | Remediation |
|---|---|---|
| `unknown_requester` | `requester_uuid` not live. | Re-check the uuid. |
| `no_such_partner` | `partner_title` names no live partner. | Call `search_partner` to find the exact title. |
| `invalid_pagination` | `page < 1` or `page_size < 1`. | Supply both as integers ≥ 1. |

**Remote dependency.** None.

### search_partner

Searches live Partners by title, best matches first. A candidate is returned only if it clears the relevance test below; an empty list is a normal answer, not an error.

**Parameters**

| Name | Type | Required | Default | Allowed values | Description |
|---|---|---|---|---|---|
| `requester_uuid` | `str` | required | — | must name a live partner | Caller's identity. |
| `query_title` | `str` | required | — | any string | The (possibly inexact) title to match against. |
| `project_id` | `int \| None` | optional | `None` | an existing project id | If given, restrict the search to this project. |
| `limit` | `int` | optional | `3` | any non-negative int | Maximum matches returned. |

**Returns** `list[dict]`, best-first, each: `{"id": int, "title": str, "project_id": int, "orchestrator_type": str | None, "descr_preview": str, "score": float}`. `descr_preview` is `descr` truncated to 160 characters with a trailing `…` if cut. `score` is the raw `difflib` ratio (`0.0`–`1.0`); ties are broken by SQLite's row order, which is unspecified since neither query carries an `ORDER BY`.

**Relevance.** A candidate qualifies on **either** rule:

1. `query_title`, lowercased, is at least 3 characters and appears literally inside the candidate's lowercased title.
2. Its `difflib` ratio is at least `0.6`.

Two rules rather than one threshold, because one threshold cannot do it. `difflib` penalises length difference, so a short deliberate query against a long title scores low however exact it is — `worker` against `research-worker` is `0.571`, `res` is `0.333`. Lowering the floor to keep them does not work either: `xylophone`, which shares nothing with any of those titles, scores `0.345` — above `res` and level with `orch`. The two are not separable by ratio at all. What separates them is that a real query is a substring of its target and a nonsense one is a substring of nothing. The `0.6` floor then does the job it is actually good at, which is tolerating a typo (`reserch` → `research-worker` is `0.636`).

Sorting is unaffected: candidates are still ranked by `score` descending, so an exact match still outranks a substring match. Only the inclusion test changed. Without it, a query matching nothing returned the top `limit` rows anyway, with scores attached — and an agent, handed titles it asked for, addressed one of them.

**Rejections**

| Code | Cause | Remediation |
|---|---|---|
| `unknown_requester` | `requester_uuid` not live. | Re-check the uuid. |

**Remote dependency.** None.

### search_project

Fuzzy-searches Projects by title, identically to `search_partner` but with no project-scoping parameter (there is no narrower scope than "all projects").

**Parameters**

| Name | Type | Required | Default | Allowed values | Description |
|---|---|---|---|---|---|
| `requester_uuid` | `str` | required | — | must name a live partner | Caller's identity. |
| `query_title` | `str` | required | — | any string | The (possibly inexact) title to match against. |
| `limit` | `int` | optional | `3` | any non-negative int | Maximum matches returned. |

**Returns** `list[dict]`, best-first, each: `{"id": int, "title": str, "source_prefix": str, "project_system_id": str, "score": float}`.

**Rejections**

| Code | Cause | Remediation |
|---|---|---|
| `unknown_requester` | `requester_uuid` not live. | Re-check the uuid. |

**Remote dependency.** None.

### send

Pushes a message into another Partner's priority queue, then lets the queue run. Returns a receipt, never a reply — any reply arrives later as its own push.

**Parameters**

| Name | Type | Required | Default | Allowed values | Description |
|---|---|---|---|---|---|
| `requester_uuid` | `str` | required | — | must name a live partner | Caller's identity; the message's sender. |
| `queried_partner_title` | `str` | required | — | exact title of a live partner | The message's recipient. |
| `message` | `str` | required | — | any string | The message body. |
| `behavior` | `str` | required | — | `"[RESEARCH]"`, `"[QUERY]"`, `"[ERROR]"`, `"[MESSAGE-RESPONSE]"`, `"[TRUTHFUL-REPORT]"` | The behavior label. Every label is sendable. `"[QUERY]"` and `"[ERROR]"` additionally stop the requester — see `already_awaiting_an_answer`. Validated at runtime against `labels.BEHAVIORS`; the MCP tool declares this as a plain `str`, not a `Literal`, so nothing statically prevents an unrecognized value from being attempted. |

There is deliberately **no** `role` parameter and **no** path parameters. There is one queue, so there is no direction to choose; and permissions are configured in advance by `add_permissions`, because an approval prompt means the grant was already missing when the work started.

**Returns** `dict`: `{"message_id": int | None, "behavior": str, "queue_depth": int, "partner_id": int, "delivered": str | None, ...}`. `message_id` is `None` unless the label has `label_caps.stored = 1`. `queue_depth` is the recipient's queue depth immediately after the insert. `delivered` names the label that actually went to the remote — which may be this message, or `None` if it is waiting behind higher-priority work, or (rarely) a *different* label if this push allowed a paused task to be promoted instead. When something was delivered, `remote_call_id` and `displaced` are also present.

**Rejections**

| Code | Cause | Remediation |
|---|---|---|
| `unknown_requester` | `requester_uuid` not live. | Re-check the uuid. |
| `no_such_partner` | `queried_partner_title` names no live partner. | Call `search_partner` to find the exact title. |
| `unknown_behavior` | `behavior` not one of the five labels. | Use one of the five values. |
| `already_awaiting_an_answer` | `behavior` is `[QUERY]` or `[ERROR]` and the requester already has an unanswered question of its own. It is stopped until that is answered, so a second question is one it could not act on the answer to. | Wait for the answer. It arrives folded into whatever the queue holds next. |
| `source_cannot_send` | The requester's source has `can_send = 0` — today, `nlm_`. A notebook has no agent behind it, so nothing there ever decides to speak. | Nothing to do; a NotebookLM Partner is reachable but never a Caller. |
| `research_not_accepted` | `behavior` is `[RESEARCH]` and the target's source has `accepts_research = 0` — today, `nlm_`. It answers questions about what it holds; it does not go and do things. | Send `[QUERY]` instead. |
| `research_cannot_flow_upward` | `behavior` is `[RESEARCH]` and the requester's `agent_layers` layer is greater than the target's. Delegated work travels down or sideways; a lower agent handing it up would be reassigning its own director's work. | Use `[QUERY]` to ask, or `[TRUTHFUL-REPORT]` to report back. Every other label travels freely in both directions. |
| `research_needs_a_forward_handshake` | `behavior` is `[RESEARCH]`, and the only `handshakes` row for the pair points `target → requester`. Answers travel back along a handshake; delegated work only travels along it in the direction the orchestrator claimed. Without this, a plain worker could hand `[RESEARCH]` to its own project-orchestrator — they share a layer, so `research_cannot_flow_upward` does not fire. | Have an orchestrator `handshake` in that direction, if the delegation is genuinely intended. |
| `no_handshake` | Target's source has `needs_handshake = 1`, and no `handshakes` row exists in **either** direction for the pair. | Call `handshake` first. |
| `over_queue` | This caller already holds `label_caps.max_outstanding` tasks of this label against this partner — **counting the one in the working slot**, which is why the queue can look one short and still refuse. Three for `[QUERY]`, two for `[RESEARCH]`; the other four labels are uncapped and can never raise this. | Wait for one to complete; do not retry immediately. |

**Remote dependency.** Raises `NeedsRemote("deliver_message", ...)` if no extension is configured for the target's `source_prefix`. Admission is fully local and **already committed** by the time this can be raised — a `NeedsRemote` on `send` means the message is genuinely queued even though it was never handed to the remote.

### extend_project

Declares another Project an extension of the caller's own, so Partners under the two may handshake across the boundary.

**Parameters**

| Name | Type | Required | Default | Allowed values | Description |
|---|---|---|---|---|---|
| `requester_uuid` | `str` | required | — | must hold `project-orchestrator` | Caller's identity. |
| `project_title` | `str` | required | — | exact title of an existing project | The project to link. |

**Returns** `dict`: `{"project_a": int, "project_b": int, "created": bool, "already_linked": bool}`. The pair is always reported with the lower id first.

**What it grants, precisely.** A handshake between two Partners of the two linked Projects, **only** when both hold the same orchestrator role. It grants nothing between two Partners of a single Project — their project ids are equal, so the cross-project branch is unreachable for them, and one Project can hold only one Partner per role in any case.

**Rejections**

| Code | Cause | Remediation |
|---|---|---|
| `unknown_requester` | `requester_uuid` not live. | Re-check the uuid. |
| `requires_project_orchestrator` | The requester does not hold `project-orchestrator`. | Only that role may extend a project. |
| `no_such_project` | `project_title` names no project. | Call `search_project`. |
| `self_extension` | The named project is the requester's own. | — |
| `cross_source_extension` | The two projects have different `source_prefix` values. A cross-source pair already handshakes without an extension, so the row would grant nothing while implying it did. | — |

**Remote dependency.** None.

### get_permissions

Reports what an Antigravity conversation actually allows, beside what `partner_paths` says it should. Read-only.

**Parameters**

| Name | Type | Required | Default | Allowed values | Description |
|---|---|---|---|---|---|
| `requester_uuid` | `str` | required | — | must name a live partner | Caller's identity. |
| `partner_title` | `str` | required | — | exact title of a live `gemini_` partner | The conversation to inspect. |

**Returns** `dict`: `{"partner_id": int, "title": str, "allowed": list[str], "recorded": {"read": [...], "write": [...]}, "missing": list[str], "unrecorded": list[str]}`. `allowed` holds rule strings in the remote's own grammar (`read_file(/p)`, `write_file(/p)`). `missing` is what is recorded but not allowed; `unrecorded` is the reverse.

**Why both halves are always reported.** The two are allowed to differ, and the difference is the only thing a Caller can act on. One number alone leaves it unable to tell a missing grant from an unrecorded one — opposite problems with opposite fixes.

**Rejections**

| Code | Cause | Remediation |
|---|---|---|
| `unknown_requester` | `requester_uuid` not live. | Re-check the uuid. |
| `no_such_partner` | No live partner with that title. | Call `search_partner`. |
| `not_path_configurable` | The target is not a `gemini_` partner. A `nlm_` source never executes and a `science_` frame has no per-frame path concept, so there is nothing to report. | — |

**Remote dependency.** `NeedsRemote("get_permissions", ...)` without an Antigravity extension.

### add_permissions

Grants read/write paths to an Antigravity conversation, and records the intent in `partner_paths` — but only after reading the remote back and confirming the grant landed.

**Parameters**

| Name | Type | Required | Default | Allowed values | Description |
|---|---|---|---|---|---|
| `requester_uuid` | `str` | required | — | must name a live partner | Caller's identity. |
| `partner_title` | `str` | required | — | exact title of a live `gemini_` partner | The conversation to grant to. |
| `read_paths` | `list[str] \| None` | optional | `None` | filesystem paths | Paths it may read. |
| `write_paths` | `list[str] \| None` | optional | `None` | filesystem paths | Paths it may write — **including files that do not exist yet** but are expected to be created. A grant covering only what is already on disk guarantees a prompt the first time the partner writes something new. |

**Returns** `dict`: `{"partner_id": int, "title": str, "granted": list[str], "unchanged": list[str], "allowed": list[str]}`. Granting a path already held is not an error; it appears under `unchanged`.

**Rejections**

| Code | Cause | Remediation |
|---|---|---|
| `unknown_requester` | `requester_uuid` not live. | Re-check the uuid. |
| `no_such_partner` | No live partner with that title. | Call `search_partner`. |
| `not_path_configurable` | Target is not `gemini_`. | — |
| `no_paths` | Both lists empty. | Give at least one path. |
| `permission_not_applied` | The remote does not show the rule after the write. **Nothing is recorded locally** in this case. | Call `get_permissions` to see the conversation's current set, then retry or fix it in the UI. |

**Remote dependency.** `NeedsRemote("add_permissions", ...)` without an Antigravity extension.

### delete_permissions

Revokes paths from an Antigravity conversation, and drops them from `partner_paths`.

**Parameters**

| Name | Type | Required | Default | Allowed values | Description |
|---|---|---|---|---|---|
| `requester_uuid` | `str` | required | — | must name a live partner | Caller's identity. |
| `partner_title` | `str` | required | — | exact title of a live `gemini_` partner | The conversation to revoke from. |
| `paths` | `list[str]` | required | — | filesystem paths | The paths to withdraw. Each is revoked in **both** kinds — `paths` names filesystem locations, not individual grants. |

**Returns** `dict`: `{"partner_id": int, "title": str, "revoked": list[str], "unchanged": list[str], "recorded_rows_removed": int, "allowed": list[str]}`.

**Why this exists separately from `add_permissions`.** Granting alone cannot correct a permission set. A set that can only grow cannot be made to match one that shrank, so a path granted by mistake would outlive every attempt to withdraw it.

**Rejections**

| Code | Cause | Remediation |
|---|---|---|
| `unknown_requester` | `requester_uuid` not live. | Re-check the uuid. |
| `no_such_partner` | No live partner with that title. | Call `search_partner`. |
| `not_path_configurable` | Target is not `gemini_`. | — |
| `no_paths` | Empty list. | Give at least one path. |
| `permission_not_applied` | The remote still shows a revoked rule afterwards. Nothing is removed locally in this case. | Call `get_permissions`. |

**Remote dependency.** `NeedsRemote("delete_permissions", ...)` without an Antigravity extension.

### status

Reports the caller's own project, role, hierarchy layer, queue (broken down by label), working task, handshakes, and gemini budget if any. Read-only.

**Parameters**

| Name | Type | Required | Default | Allowed values | Description |
|---|---|---|---|---|---|
| `requester_uuid` | `str` | required | — | must name a live partner | Caller's identity; also the subject of the report. |

**Returns** `dict`:

| Field | Type | Description |
|---|---|---|
| `partner_id` | `int` | Requester's id. |
| `title` | `str` | Requester's title. |
| `project_id` | `int` | Requester's project id. |
| `project_title` | `str \| None` | The project's title (`None` only if the project row is somehow missing). |
| `orchestrator_type` | `str \| None` | Requester's role, if any. |
| `layer` | `int` | Position in `agent_layers`. Lower is higher up; governs only the `[RESEARCH]` direction rule. |
| `queue_depth` | `int` | Total rows in `message_queue` for this requester as target. |
| `queued` | `list[dict]` | Per-label breakdown, highest priority first: `{"behavior": str, "count": int, "paused": int, "priority": int}`. A single depth number says nothing useful; "three `[RESEARCH]`, one `[QUERY]`" says what happens next and why. |
| `working` | `dict \| None` | The task in the working slot, or `None`. `{"behavior": str, "caller_id": int, "resumed": bool, "enqueued_at": str, "started_at": str}`. **This is the only way to see the working slot** — it lives in process memory, so no query against the database can show it, and a `status` call from a different process will not see it either. |
| `handshakes_out` | `list[str]` | Titles of live partners this requester has handshaken toward. |
| `handshakes_in` | `list[str]` | Titles of live partners that have handshaken toward this requester. |
| `gemini_budget` | `dict \| None` | `None` unless the requester is a `gemini-orchestrator`; otherwise `{"budget_count": int, "used": int}` (`budget_count` is `0` if no grant row exists). |

Both handshake lists filter out archived counterparts explicitly — an archived partner never appears in another partner's `status`.

`enqueued_at` and `started_at` on `working` are the two halves of the latency measurement. The first comes from SQLite, the second from `MessagingCore._now`, and both use the same timestamp format deliberately so the subtraction is not a parsing problem discovered at the worst moment.

**Rejections**

| Code | Cause | Remediation |
|---|---|---|
| `unknown_requester` | `requester_uuid` not live. | Re-check the uuid. |

**Remote dependency.** None — `status` never touches the extension, by design of its own docstring.

## 3. Rejection code index

Every code raised by `Rejected(...)` in `messaging_core/core.py`, extracted directly from source.

Codes raised by more than one capability list every raiser. `unknown_requester` is raised by all fifteen capabilities that accept a `requester_uuid` (every capability except `create_project` and `create_partner`, which take none) and is omitted from capability-specific rows below to avoid repeating it fifteen times — see its own row.

| Code | Raised by | Remediation |
|---|---|---|
| `already_has_role` | `claim_orchestrator` | Do not retry; the requester's existing role is permanent. |
| `bridge_handshakes_orchestrator_or_code` | `handshake` | A `bridge-scientist` may only reach the `project-orchestrator` or a single `code_` partner. |
| `bridge_single_code_partner` | `handshake` | That bridge already holds a different `code_` partner; use it, or a different bridge. |
| `code_handshakes_bridge_only` | `handshake` | A `code_` partner's only legal counterpart is a `bridge-scientist`, in either direction. |
| `constraint_violation` | `create_partner` | An unpre-checked `IntegrityError` fired (likely a race); re-verify uniqueness and retry. |
| `cross_project_requires_same_role` | `handshake` | A handshake across a project extension needs both partners to hold the same orchestrator role. |
| `cross_source_extension` | `extend_project` | Two projects of different sources already handshake without an extension. |
| `descr_too_long` | `create_partner` | Shorten `descr` to ≤ 1200 characters. |
| `already_awaiting_an_answer` | `send` | This agent is stopped on its own unanswered `[QUERY]` or `[ERROR]`. Wait for the answer. |
| `different_project` | `handshake` | Share a project, or link the two with `extend_project`. |
| `duplicate_handshake` | `handshake` | Skip `handshake`; the direction already exists — call `send`. |
| `duplicate_partner_title` | `create_partner` | Choose an unused title (server-wide, archived included). |
| `duplicate_project_system_id` | `create_project` | Call `search_project` to find the existing project. |
| `duplicate_project_title` | `create_project` | Choose a different title. |
| `gemini_budget_exceeded` | `handshake` | Call `grant_gemini_budget` to raise the budget. |
| `orchestrator_requires_science_project` | `claim_orchestrator` | **All three** orchestrator roles are Claude Science roles. Claim any of them only from a `science_` partner. |
| `gemini_single_science_source` | `handshake` | Target a different `gemini_` partner. |
| `gemini_to_science_illegal` | `handshake` | Never succeeds; use `science_ → gemini_`. **Unreachable through the tools** — a `gemini_` partner can hold no role, so a pair that is not `gemini_ → gemini_` is refused at `requester_not_orchestrator` first. Kept as defence in depth against a role written straight into the database. |
| `grantee_not_gemini_orchestrator` | `grant_gemini_budget` | Have the grantee claim `gemini-orchestrator` first. |
| `handshake_not_needed` | `handshake` | Call `send` directly; `nlm_` needs no handshake. |
| `invalid_budget_count` | `grant_gemini_budget` | Supply `0`–`3` inclusive. |
| `invalid_orchestrator_type` | `claim_orchestrator` | Use one of the three role strings. |
| `invalid_pagination` | `read` | Supply `page` and `page_size` both ≥ 1. |
| `invalid_source_prefix` | `create_project` | Use one of the four recognized prefixes. |
| `live_partner_limit` | `create_partner` | Call `archive_sessions` to free a slot. |
| `no_gemini_budget` | `handshake` | Call `grant_gemini_budget` first. |
| `no_handshake` | `send` | Call `handshake` first (unless the target's source needs none). |
| `no_handshake_between_gemini` | `handshake` | Two Antigravity conversations in one Project. Inherit across a project extension instead. |
| `gemini_already_inherited` | `handshake` | That conversation is already continued by another. Inherit from one nothing has claimed. |
| `no_paths` | `add_permissions`, `delete_permissions` | Give at least one path. |
| `not_reportable` | `report_back` (queue machinery, §8 — not a client tool) | `[RESEARCH]` is delegation, not something a Partner reports. Delegating is what `send` is for, and `send` is where the hierarchy rules live. |
| `no_such_partner` | `delete_partner`, `handshake`, `send`, `read`, `get_permissions`, `add_permissions`, `delete_permissions` | Call `search_partner` to find the exact title. |
| `no_such_project` | `create_partner`, `delete_project`, `extend_project` | Call `search_project` to find or confirm the project. |
| `not_authorized` | `delete_partner`, `delete_project`, `grant_gemini_budget` | Only the relevant `project-orchestrator` may perform this action. |
| `not_path_configurable` | `get_permissions`, `add_permissions`, `delete_permissions` | Only Antigravity conversations carry path permissions. |
| `over_queue` | `send` | This caller's allowance for this label is full, counting the working slot. Wait for one to complete. |
| `partner_has_work_in_flight` | `delete_partner` | Archive it instead — archiving reports the loss to every caller waiting. |
| `partner_has_dependents` | `delete_partner` | Call `archive_sessions` instead of deleting. |
| `partner_has_work_in_flight` | `delete_partner` | Archive it instead; archiving reports the loss to every caller waiting. |
| `partner_id_in_remote_not_found` | `create_partner` | Confirm the id exists in the remote app. |
| `partner_id_in_remote_taken` | `create_partner` | Use the existing partner, or a different remote id. |
| `permission_not_applied` | `add_permissions`, `delete_permissions` | The remote does not show the change; **nothing was recorded locally**. Call `get_permissions`. |
| `project_system_id_not_found` | `create_project` | Confirm the id exists in the remote app. |
| `requester_not_orchestrator` | `handshake` | Claim an orchestrator role first — unless the requester is `code_`, which is exempt and checked earlier. |
| `requires_gemini_orchestrator` | `handshake` | Have the project's `gemini-orchestrator` initiate it. |
| `requires_project_orchestrator` | `handshake`, `extend_project` | Have the project's `project-orchestrator` do it. |
| `research_cannot_flow_upward` | `send` | `[RESEARCH]` travels down or sideways. Use `[QUERY]` or `[TRUTHFUL-REPORT]` instead. |
| `research_needs_a_forward_handshake` | `send` | Only the reverse handshake exists. Answers travel back along it; delegation does not. |
| `research_not_accepted` | `send` | That source does not take delegated work. Send `[QUERY]`. |
| `role_already_claimed` | `claim_orchestrator` | Use the existing holder, or claim a different role. |
| `self_extension` | `extend_project` | Name a different project. |
| `self_handshake` | `handshake` | Name a different target. |
| `source_cannot_send` | `send` | That source never originates a message. |
| `unknown_behavior` | `send` | Use one of the five sendable labels. |
| `unknown_requester` | every capability with a `requester_uuid` (all except `create_project`, `create_partner`) | Re-check the uuid returned at `create_partner` time. |
| `wrong_project` | `claim_orchestrator` | Pass the project the requester actually belongs to. |

Three further code families exist outside `core.py` and are listed here so a caller that sees one knows where it came from:

**`polling/server.py`** raises `unknown_partner` and `no_extension`.

**`extension/base.py`** raises `not_executable` (from `NonExecutingExtension.stop_remote_execution`) and `not_path_configurable` (from the three permission defaults). Both are the base class refusing on behalf of an adapter that has nothing to do.

**The adapters** raise their own, all of which mean "the remote could not do this, and I will not pretend otherwise": `antigravity_session_unreachable`, `antigravity_session_not_ready`, `antigravity_project_unknown`, `antigravity_project_unreadable`, `antigravity_session_stuck_in_editor`, `approval_is_an_error`, `permissions_view_did_not_open` (Antigravity); `no_adapter_for_code` and `unknown_source_prefix` (the registry).

`antigravity_session_not_ready` also deserves a note, and it was found live. A fresh `agy` session in a folder agy has not seen before paints a banner and then a **modal trust dialog** — while `tmux has-session`, the only thing `verify_partner_id_in_remote` checks, succeeds the instant the session exists. So a Partner could be registered against a conversation that could not receive anything, and the first delivery typed the Caller's message into that menu. `_await_busy` then timed out (a timeout there is deliberately not an error — a turn short enough to finish inside the window never shows a busy footer), `poll_completion` found no busy footer and reported the turn finished, and `read_remote_result` fell back to the whole pane. The Caller received agy's startup banner, stored as its answer, with nothing erroring anywhere.

`deliver_message` now waits for the idle footer before it types anything, refuses with `antigravity_session_not_ready` if the session never reaches an input prompt, and raises `approval_is_an_error` if a trust or permission dialog is what is blocking — which routes it into the path that already exists, where the Polling Server reports it to the Caller as an `[ERROR]` naming the remedy.

`antigravity_session_stuck_in_editor` deserves a note, because it reports something that is otherwise silent. If the permissions editor cannot be closed, everything sent to that session afterwards is read as editor input — so the next message is swallowed and no error appears anywhere. The refusal exists to make that visible.

---

## 4. Schema reference

All 12 tables in `schema/schema.sql`, in file order. `PRAGMA foreign_keys = ON` is set for every connection (`db.py`), so every `ON DELETE` clause below is live, not decorative. Five triggers are named where they apply.

### source_caps

Per-`source_prefix` capability data — "a new source is a row, not a code change."

| Column | Type | Constraints |
|---|---|---|
| `source_prefix` | `TEXT` | PK; `CHECK IN ('nlm_','code_','science_','gemini_')` |
| `max_live_partners` | `INTEGER` | `NOT NULL DEFAULT 10`, `CHECK > 0` |
| `can_execute` | `INTEGER` | `NOT NULL DEFAULT 1`, `CHECK IN (0,1)` |
| `needs_handshake` | `INTEGER` | `NOT NULL DEFAULT 1`, `CHECK IN (0,1)` |
| `can_send` | `INTEGER` | `NOT NULL DEFAULT 1`, `CHECK IN (0,1)` |
| `accepts_research` | `INTEGER` | `NOT NULL DEFAULT 1`, `CHECK IN (0,1)` |

Seeded rows: `nlm_` (`can_execute=0, needs_handshake=0, can_send=0, accepts_research=0`); `code_`, `science_`, `gemini_` (all four flags `1`). Every row has `max_live_partners=10`.

There is deliberately **no `max_queue`**. A queue limit is not a property of the source — it is a property of `(caller, label)`, which is `label_caps.max_outstanding`, because the thing worth limiting is how much of one *kind* of work one caller may have outstanding against one partner, not how many messages a partner can hold in total.

All five behavioral columns are read from the database rather than compared against literals: `can_execute` by `_require_executable`, `needs_handshake` by `_needs_handshake`, `can_send` and `accepts_research` by `send`, and `max_live_partners` inside the `partners_live_limit` trigger. A previous version of this document recorded `needs_handshake` as a dead column read nowhere, with the rule hardcoded as a literal `"nlm_"` comparison. That is fixed; the literal is gone.

### label_caps

Every per-label fact, in one place. The single authority for priority, caps, storage, and what a finished task replies with.

| Column | Type | Constraints |
|---|---|---|
| `behavior` | `TEXT` | PK; `CHECK IN` the six labels |
| `priority` | `INTEGER` | `NOT NULL`. Lower wins. |
| `max_outstanding` | `INTEGER` | `CHECK (IS NULL OR > 0)`. NULL means uncapped. |
| `stored` | `INTEGER` | `NOT NULL DEFAULT 0`, `CHECK IN (0,1)` |
| `reply_behavior` | `TEXT` | FK → `label_caps(behavior)`; `CHECK (IS NULL OR <> behavior)` |

Seeded rows:

| behavior | priority | max_outstanding | stored | reply_behavior |
|---|---|---|---|---|
| `[TRUTHFUL-REPORT]` | 1 | — | 1 | — |
| `[QUERY]` | 2 | 3 | 1 | `[MESSAGE-RESPONSE]` |
| `[ERROR]` | 2 | — | 0 | `[MESSAGE-RESPONSE]` |
| `[MESSAGE-RESPONSE]` | 3 | — | 1 | — |
| `[RESEARCH]` | 4 | 2 | 0 | `[TRUTHFUL-REPORT]` |

`[QUERY]` and `[ERROR]` sit at 2 and are never raised above it. That is what makes an unanswered question a blocker: only `[TRUTHFUL-REPORT]` at 1 outranks a waiting agent, and everything else queues behind the question.

`reply_behavior`'s NULLs are the load-bearing values: two labels reply with nothing, and without them every completed task would produce a message that produced a task. The self-reference `CHECK` forbids the same infinite exchange written more compactly.

### agent_layers

Where each kind of agent sits in the delegation hierarchy.

| Column | Type | Constraints |
|---|---|---|
| `source_prefix` | `TEXT` | PK part; FK → `source_caps(source_prefix)` |
| `orchestrator_type` | `TEXT` | PK part. A role name, or `'*'` for the source's default. |
| `layer` | `INTEGER` | `NOT NULL`, `CHECK >= 0`. Lower is higher up. |

`WITHOUT ROWID`. Seeded: `nlm_/*`=0, `code_/*`=0, `science_/bridge-scientist`=1, `science_/project-orchestrator`=2, `science_/*`=2, `science_/gemini-orchestrator`=3, `gemini_/*`=4.

A row naming the partner's own role wins over the source's `'*'` row. The lookup orders by `CASE orchestrator_type WHEN '*' THEN 1 ELSE 0 END` to make that happen — `'*'` sorts before every real role name alphabetically and would otherwise win by accident.

Governs exactly one rule: `[RESEARCH]` may not travel to a lower-numbered layer.

### projects

| Column | Type | Constraints |
|---|---|---|
| `id` | `INTEGER` | PK |
| `source_prefix` | `TEXT` | `NOT NULL`, FK → `source_caps(source_prefix)` |
| `project_system_id` | `TEXT` | `NOT NULL`; `UNIQUE (source_prefix, project_system_id)` |
| `title` | `TEXT` | `NOT NULL UNIQUE` |
| `created_at` | `TEXT` | `NOT NULL DEFAULT` current UTC timestamp |

### project_extension

Two Projects declared parts of one research effort.

| Column | Type | Constraints |
|---|---|---|
| `project_a` | `INTEGER` | PK part; FK → `projects(id) ON DELETE CASCADE` |
| `project_b` | `INTEGER` | PK part; FK → `projects(id) ON DELETE CASCADE` |
| `created_at` | `TEXT` | `NOT NULL DEFAULT` current UTC timestamp |

`WITHOUT ROWID`, `CHECK (project_a < project_b)`. Symmetric by construction: the pair is stored with the lower id first, so "is A an extension of B" has exactly one row to look at and cannot disagree with itself depending on which way it is asked.

### partners

| Column | Type | Constraints |
|---|---|---|
| `id` | `INTEGER` | PK |
| `uuid` | `TEXT` | `NOT NULL UNIQUE` |
| `project_id` | `INTEGER` | `NOT NULL`, FK → `projects(id) ON DELETE CASCADE` |
| `title` | `TEXT` | `NOT NULL UNIQUE` — server-wide, archived rows included |
| `partner_id_in_remote` | `TEXT` | `NOT NULL`; `UNIQUE (project_id, partner_id_in_remote)` |
| `descr` | `TEXT` | `NOT NULL`, `CHECK length <= 1200` |
| `orchestrator_type` | `TEXT` | `CHECK IN` the three roles, or NULL |
| `archived_at` | `TEXT` | NULL means live |
| `created_at` | `TEXT` | `NOT NULL DEFAULT` current UTC timestamp |

Indexes: `partners_by_project (project_id) WHERE archived_at IS NULL`; `one_orchestrator_per_project_role (project_id, orchestrator_type) UNIQUE WHERE orchestrator_type IS NOT NULL AND archived_at IS NULL` — a partial unique index, so a concurrent claim race has one winner at the database level rather than in a check-then-act.

Triggers: `partners_live_limit` (BEFORE INSERT) enforces `source_caps.max_live_partners`, which a `CHECK` cannot do because it cannot count rows. `partners_no_rename_archived` (BEFORE UPDATE OF title) refuses renaming an archived partner, so a spent title cannot be laundered back into use by a code path that bypasses the tools.

`partner_id_in_remote` is unique per *Project*, not globally: a remote id is only meaningful inside the remote app that issued it, and two Projects could coincidentally use the same string for genuinely different objects.

### handshakes

| Column | Type | Constraints |
|---|---|---|
| `id` | `INTEGER` | PK |
| `from_partner` | `INTEGER` | `NOT NULL`, FK → `partners(id) ON DELETE CASCADE` |
| `to_partner` | `INTEGER` | `NOT NULL`, FK → `partners(id) ON DELETE CASCADE` |
| `created_at` | `TEXT` | `NOT NULL DEFAULT` current UTC timestamp |

`UNIQUE (from_partner, to_partner)`, `CHECK (from_partner <> to_partner)`. One-way; a reply direction is a second row.

### partner_paths

The read/write grant a `gemini_` partner is **meant** to hold.

| Column | Type | Constraints |
|---|---|---|
| `partner_id` | `INTEGER` | PK part; FK → `partners(id) ON DELETE CASCADE` |
| `kind` | `TEXT` | PK part; `CHECK IN ('read','write')` |
| `path` | `TEXT` | PK part |

`WITHOUT ROWID`. Trigger `partner_paths_gemini_only` (BEFORE INSERT) refuses a row for any non-`gemini_` partner — a grant nothing will ever apply is indistinguishable, to a reader, from one that is being enforced.

Written only by `add_permissions` and `delete_permissions`, and only after the remote is confirmed to hold the change. **Not touched by `send`.** A permission prompt means the grant was missing before the work started, so configuring paths as a side effect of sending work would always be one step too late.

This table is the *intended* set; `get_permissions` reports the *actual* one. They are allowed to differ, and the difference is what a Caller acts on.

### budget_grants

| Column | Type | Constraints |
|---|---|---|
| `grantee_partner` | `INTEGER` | PK; FK → `partners(id) ON DELETE CASCADE` |
| `granted_by` | `INTEGER` | `NOT NULL`, FK → `partners(id)` |
| `budget_count` | `INTEGER` | `NOT NULL`, `CHECK BETWEEN 0 AND 3` |
| `granted_at` | `TEXT` | `NOT NULL DEFAULT` current UTC timestamp |

Trigger `budget_grants_roles_insert` (BEFORE INSERT) requires `granted_by` to hold `project-orchestrator` and `grantee_partner` to hold `gemini-orchestrator`. Neither end was constrained before, so a budget could be granted by anyone to anyone — and the handshake rule that spends it would then be metering a meaningless number. A `CHECK` cannot reach another table, so both ends are trigger conditions.

### messages

Persisted history — only for the labels `label_caps` marks as stored.

| Column | Type | Constraints |
|---|---|---|
| `id` | `INTEGER` | PK |
| `from_partner` | `INTEGER` | `NOT NULL`, FK → `partners(id) ON DELETE CASCADE` |
| `to_partner` | `INTEGER` | `NOT NULL`, FK → `partners(id) ON DELETE CASCADE` |
| `behavior` | `TEXT` | `NOT NULL`, FK → `label_caps(behavior)` |
| `body` | `TEXT` | `NOT NULL` |
| `created_at` | `TEXT` | `NOT NULL DEFAULT` current UTC timestamp |

Index: `messages_readable (to_partner, id DESC)` — backs `read`'s pagination query directly.

Trigger `messages_stored_labels_only` (BEFORE INSERT) refuses any behavior whose `label_caps.stored` is not 1. Note what it is **not**: a `CHECK` listing `('[QUERY]','[TRUTHFUL-REPORT]','[MESSAGE-RESPONSE]')`. Such a list would be a second copy of `label_caps.stored`, and two copies of one fact eventually disagree. With the trigger, flipping `stored` on a row changes what this table accepts — which is what "the table is the authority" has to mean to be worth saying.

### message_queue

One priority queue per Partner. A row is a QUEUED poll task.

| Column | Type | Constraints |
|---|---|---|
| `id` | `INTEGER` | PK |
| `partner_id` | `INTEGER` | `NOT NULL`, FK → `partners(id) ON DELETE CASCADE` |
| `caller_id` | `INTEGER` | `NOT NULL`, FK → `partners(id) ON DELETE CASCADE` |
| `behavior` | `TEXT` | `NOT NULL`, FK → `label_caps(behavior)` |
| `body` | `TEXT` | `NOT NULL` |
| `in_process` | `INTEGER` | `NOT NULL DEFAULT 0`, `CHECK IN (0,1)` — 1 means paused |
| `message_id` | `INTEGER` | FK → `messages(id) ON DELETE SET NULL`; set only for stored labels |
| `summary_phase` | `INTEGER` | `NOT NULL DEFAULT 0`, `CHECK IN (0,1)` — 1 means this row is a displaced `[RESEARCH]` summary phase |
| `origin_behavior` | `TEXT` | FK → `label_caps(behavior)`; the label the task was admitted under, when it differs from `behavior` |
| `awaiting_resolution` | `INTEGER` | `NOT NULL DEFAULT 0`, `CHECK IN (0,1)`; this row is an agent's own unanswered question, displaced out of its working slot |
| `enqueued_at` | `TEXT` | `NOT NULL DEFAULT` current UTC timestamp |

`CHECK (caller_id <> partner_id)`. Index: `message_queue_order (partner_id, behavior, in_process DESC, enqueued_at)`.

`summary_phase` and `origin_behavior` exist because a `[RESEARCH]` task changes label mid-flight without leaving the working slot: `begin_summary_phase` relabels it `[TRUTHFUL-REPORT]` so that nothing can interrupt the summary. If it *is* interrupted, the row that goes back into the queue would otherwise be indistinguishable from a `[TRUTHFUL-REPORT]` an agent sent directly — which owes nothing back, because it already **is** the report. The two columns carry the distinction across the interruption: `summary_phase` says the Caller is still owed a report, and `origin_behavior` says the work still counts against that Caller's `[RESEARCH]` cap.

`awaiting_resolution` exists for the mirror case: a `[TRUTHFUL-REPORT]` is the one thing that outranks an agent waiting on its own question, so a question really can be displaced out of a working slot and into this table. The question is not work. Without the marker it would come back indistinguishable from an ordinary `[QUERY]` a caller had sent, and be handed to the agent as something to answer — a question it asked. The column is read **first** in both `_HEAD_LABEL_SQL` and `_HEAD_ROW_SQL`, so a displaced question outranks everything else in that agent's queue and re-enters the wait rather than being delivered. It is also what scopes the "at most one paused row per label" property to work rows: a wait carries a label too, and can share one with a paused task, but it is never rendered and so is never what a resume prompt names.

The task actually being worked — the working slot — is deliberately **not** in this table. It is held in memory by the Polling Server: it is process state, it changes on every swap, and persisting it would invite a reader to believe it survives a restart when it does not.

There is deliberately **no `dequeued_at`**. A promoted row is DELETED, so such a column could only ever be written to a row about to disappear. The start time lives on the in-memory working slot beside the task it describes and is reported by `status`. A column with no surviving writer is worse than no column, because it reads like a measurement someone is taking.

`in_process` is a tie-break *within* a label, not a global override. `[QUERY]` and `[ERROR]` share a priority, so the difference is reachable: a global tie-break would have a paused `[QUERY]` beat a fresh `[ERROR]`, and the Caller's correction would never be delivered. `_HEAD_LABEL_SQL` and `_HEAD_ROW_SQL` are two statements for that reason.

### drain_threads

The drain-thread registry.

| Column | Type | Constraints |
|---|---|---|
| `partner_id` | `INTEGER` | PK; FK → `partners(id) ON DELETE CASCADE` |
| `thread_id` | `TEXT` | `NOT NULL` |
| `started_at` | `TEXT` | `NOT NULL DEFAULT` current UTC timestamp |

`WITHOUT ROWID`. A thread that drains its Partner's queue to empty with nothing in the working slot retires and **DELETES its own row**. `stop()` deliberately does not delete rows: it signals threads for a process that is going away with work possibly still queued, and the row is exactly what `start()` uses to bring that Partner's thread back.

## 5. The response body standard

`messaging_core/responses.py` defines the text shape every MCP tool wrapper (`mcp_server/server.py`) returns. Four helper functions, three of which produce one of the module's three documented markers, plus two fixed strings:

```python
ANTI_POLL = "Do not poll. The event will carry the output."
NOTHING_CHANGED = "Nothing was changed."
```

- **`ok(body, *, next_call=None, anti_poll=False)`** → `"[ok] {body}"`, followed by a blank line and, if given, `next_call` and/or `ANTI_POLL` on one line together. This is the marker every successful capability's tool wrapper emits — `[ok]` itself is not one of the three markers named in the module's own docstring (`[rejected]`, `[nothing new]`, `[still working ...]`), but it is the one used far more often than the other three combined, since it fires on every non-error response.
- **`rejected(reason, *, noop=NOTHING_CHANGED, next_call=None)`** → `"[rejected] {reason}"`, followed by a blank line, then `NOTHING_CHANGED` (or a caller-supplied `noop`) and, if given, `next_call` appended to the same line. Every `Rejected` exception is guaranteed to have changed nothing, and this function's default hard-codes that guarantee into the text rather than trusting each call site to restate it.
- **`nothing_new(what, *, next_call=None)`** → `"[nothing new] {what}"`, optionally followed by `next_call` on its own line. Used by `PollingServer.notify_partner_push` when a drain thread is already running for a partner.
- **`still_working(subject)`** → `"[still working - {subject}]"`, always followed by `ANTI_POLL`. Not called anywhere in the files read for this document; it exists in the module for an asynchronous in-progress case.

**The named-next-call convention.** Every `next_call` argument, wherever it appears (`ok`, `rejected`, `nothing_new`), is used verbatim — call sites pass a complete sentence naming the concrete next tool to call, e.g. `"Call search_partner to find the exact title."` This is also the convention `Rejected.next_call` follows in `errors.py`, and `mcp_server/server.py`'s `_rejected_body` passes it straight through: `responses.rejected(exc.message, next_call=exc.next_call)`.

**The anti-poll line.** `ANTI_POLL` always appears with `still_working`, and optionally with `ok` via `anti_poll=True` — the only tool in `mcp_server/server.py` that passes it is `send`, since `send` is fire-and-forget and its real answer, if any, arrives later as a push event rather than from this call.

**A fourth marker outside `responses.py`.** `mcp_server/server.py`'s `_needs_remote_body` renders a `NeedsRemote` as `"[needs remote] {exc.reason}"` followed by a two-sentence explanation naming `exc.capability`. This marker is hand-built in `server.py`, not defined in or emitted by any function in `responses.py` — a caller pattern-matching only on the three (or four, counting `[ok]`) markers documented in `responses.py`'s own docstring would miss it.

---

## 6. Extension interface

`extension/base.py` defines `RemoteExtension(ABC)`, with a class-level `source_prefix: str` every subclass must set to one of the four recognized prefixes.

**Four abstract methods** — every concrete subclass must implement all four:

| Method | Signature | Purpose |
|---|---|---|
| `verify_project_system_id` | `(self, project_system_id: str) -> bool` | Does this id name a real project on the remote? |
| `verify_partner_id_in_remote` | `(self, project_system_id: str, partner_id_in_remote: str) -> bool` | Does this id name a real session/partner under that project? |
| `deliver_message` | `(self, *, partner_id_in_remote: str, behavior: str, body: str) -> str` | Hand `body` to the remote partner; return an opaque remote-side identifier. **Must not return while the remote still looks idle** — see below. |
| `stop_remote_execution` | `(self, *, partner_id_in_remote: str, reason: str) -> None` | Stop whatever the remote partner is doing. |

`deliver_message` carries one obligation that is easy to miss and was missed: it must not
return until the remote has *visibly* started. The drain loop's contract is "deliver, then poll
until finished", and that is only sound if the first poll cannot observe the state from before
the delivery. For a TUI-driven remote it can — a pane repaints asynchronously — and an
Antigravity round trip was observed reporting complete in 0 seconds, with the agent answering
into a pane nobody was watching any more. `AntigravityExtension` waits for its busy footer,
bounded; a timeout is success, because a turn that fast has already answered. Read
`docs/05-invariants-and-constraints.md` §22.

**There is deliberately no fifth for resuming.** Correcting whatever blocked a partner and sending the work again *is* the resumption. A remote-side resume would be a second way to start work — one that skips the queue, and the queue is where priority is decided.

**Two concrete methods with a `NeedsRemote`-raising default** — a subclass overrides either only if its remote actually supports it:

| Method | Signature | Default behavior |
|---|---|---|
| `poll_completion` | `(self, *, partner_id_in_remote: str) -> bool` | Raises `NeedsRemote("poll_completion", ...)`. |
| `read_remote_result` | `(self, *, partner_id_in_remote: str) -> str` | Raises `NeedsRemote("read_remote_result", ...)`. |

**Three concrete permission methods with a `Rejected`-raising default.** Only a remote that executes against a filesystem has anything to grant, so a refusing default is what lets two of the three adapters stay honest without writing three stubs each:

| Method | Signature | Default behavior |
|---|---|---|
| `get_permissions` | `(self, *, partner_id_in_remote: str) -> list[str]` | Raises `Rejected("not_path_configurable", ...)`. |
| `add_permissions` | `(self, *, partner_id_in_remote: str, rules: list[str]) -> None` | Same. |
| `delete_permissions` | `(self, *, partner_id_in_remote: str, rules: list[str]) -> None` | Same. |

Rules are strings in the remote's own grammar — `write_file(/path)`, `read_file(/path)`. The core builds them and never parses them back apart, so a remote with a different grammar changes this contract rather than the caller.

`add_permissions` and `delete_permissions` return `None` and deliberately do **not** report success. The caller reads `get_permissions` back and checks (`MessagingCore._apply_and_verify`); a return value here would be the remote's opinion of its own success, which is the thing being verified.

**`NonExecutingExtension(RemoteExtension)`** overrides `stop_remote_execution` to raise `Rejected("not_executable", ...)` unconditionally, for a remote (like NotebookLM) that provides context but never runs anything. `verify_project_system_id`, `verify_partner_id_in_remote`, and `deliver_message` remain abstract on this base — a non-executing remote still has to be found, verified, and messaged.

**`StubExtension(RemoteExtension)`** is a concrete, in-memory fake for tests, `source_prefix` defaulting to `"code_"`. It implements every method (including `poll_completion`, `read_remote_result`, and the permission trio, unlike the base's refusing defaults), records every call as `(method_name, kwargs_dict)` on `.calls`, and exposes settable result attributes (`verify_project_system_id_result`, `verify_partner_id_in_remote_result`, `completed`, `read_remote_result_value`, `deliver_message_result`, `permissions`). One more deserves naming: **`.permissions_refuse`** is a set of rules the stub accepts and then silently does not apply. That is the failure `_apply_and_verify` exists for, and it cannot be reached with a stub that always cooperates.

**Which of the three real adapters implements what** (`adapters/*/adapter.py`; ground truth for each is documented at length in that adapter's own module docstring and is not repeated here):

| Adapter | `source_prefix` | Base class | Implements normally | Always rejects |
|---|---|---|---|---|
| `NotebookLMExtension` | `nlm_` | `NonExecutingExtension` | `verify_project_system_id`, `verify_partner_id_in_remote`, `deliver_message`, `poll_completion` (always `True` — synchronous from this side), `read_remote_result` (the only adapter that implements this for real) | `stop_remote_execution` (inherited); the permission trio (inherited — nothing to grant) |
| `ClaudeScienceExtension` | `science_` | `RemoteExtension` | `verify_project_system_id`, `verify_partner_id_in_remote`, `deliver_message`, `poll_completion`, `stop_remote_execution` (`POST /api/frames/{id}/cancel` — the route the UI's stop button calls; frame id only), `read_remote_result` | the permission trio (inherited — a frame has no per-frame path concept at all) |
| `AntigravityExtension` | `gemini_` | `RemoteExtension` | Everything: `verify_project_system_id`, `verify_partner_id_in_remote`, `deliver_message`, `stop_remote_execution` (sends `Escape` via `tmux`), `poll_completion` (also raises `Rejected("approval_is_an_error", ...)` if the pane shows an approval prompt — never answered), and all three permission operations | — |

Neither `ClaudeScienceExtension` nor `AntigravityExtension` overrides `read_remote_result` — both inherit the base's `NeedsRemote` default, so the Polling Server's `_read_result` falls back to a placeholder string for them.

**How Antigravity's permissions actually work**, since it is the only real implementation. All of the following was driven against a live `agy` session, not inferred.

`get_permissions` READS `~/.gemini/config/projects/<project-id>.json`, where the project id comes from `~/.gemini/antigravity-cli/cache/default_project_id.txt`. The allowlist is at **`permissionGrants.permissionGrants.allow`**, a list of rule strings under a doubly-nested key. It is *not* `projectResources`, which stays `{}`.

That distinction was got wrong once, and the way it was got wrong is the useful part. The adapter originally read `projectResources`, "confirmed" by observing it was `{}` while the TUI header read `allowlist (0)`. Empty matched empty, which is evidence of nothing — and the moment a real rule existed, the TUI read `allowlist (1)` and the adapter still returned `[]`. **A mapping is only confirmed by a non-empty value appearing on both sides.**

`add_permissions` and `delete_permissions` WRITE through the `/permissions` view over `tmux`. The asymmetry is deliberate: reading a file is exact, typing into a TUI is not, so the typed half is the half that has to prove it worked.

The view is **three screens**, and the count matters:

1. `/permissions` + Enter selects the command from the palette and lands on a scope selector (`Permission Config Editor`), with `Project` already highlighted.
2. A **second Enter** takes that default and lands on the rule list (`allowlist (N)`, footer `a Add rule  e Edit rule  d/⌫ Delete rule`).
3. `a` opens the rule input (`Format: action(target)`). The rule is typed with `send-keys -l` so its parentheses are not read as key names, then Enter.

Each screen is confirmed before the next key is sent; a screen that does not appear is `permissions_view_did_not_open` rather than a guess. Closing presses Escape until the idle chat footer returns — one Escape only reaches the scope selector, and a session left inside the editor reads the next `deliver_message` as editor input, so the message is swallowed with nothing reported anywhere.

`d` deletes the **selected** rule with no confirmation, so `delete_permissions` re-reads the list before every removal (a prior deletion renumbers it), moves the cursor onto the rule by name, and confirms the cursor is there before pressing `d`. A rule it cannot select is a refusal, not a best guess: deleting the wrong permission is worse than deleting none, because the caller is told the revocation succeeded while the grant is still live.

One real limit remains. Antigravity permissions are **project-scoped, not conversation-scoped** — every conversation under one project sees one list, so `partner_id_in_remote` is accepted for interface symmetry and is not used by `get_permissions`.

`code_` has no adapter. `adapters/registry.py`'s `build_extension(source_prefix)` raises `Rejected("no_adapter_for_code", ...)` for `source_prefix == "code_"` specifically (a Claude Code session is local and has no remote presence to build a client for) and `Rejected("unknown_source_prefix", ...)` for anything else unrecognized.

**A resolved finding, kept because the failure mode is instructive.** There used to be two functions named `build_extension` — one in `adapters/registry.py` returning the real adapter, one in `mcp_server/config.py` returning a `StubExtension` for every source. The server called the second, so every deployment silently talked to a stub: messages appeared accepted and went nowhere, and nothing errored, so nothing said so. `mcp_server/config.py` now delegates to `adapters.registry` and returns a `StubExtension` only under `MESSAGING_MCP_STUB=1`, covered by `tests/test_mcp_config.py`.

---

## 7. Configuration

### Environment variables

| Variable | Read by | Required | Effect |
|---|---|---|---|
| `MESSAGING_MCP_HOME` | `messaging_core/config.py: data_dir()` | No | Overrides the data directory (default `~/.messaging-mcp`). Only consulted when `MESSAGING_MCP_DB` is unset and no explicit `Database(path=...)` is given. |
| `MESSAGING_MCP_DB` | `mcp_server/config.py: build_stack_from_env()` | No | A filesystem path, or the literal `":memory:"`, passed straight to `Database(path=...)`. When set, `MESSAGING_MCP_HOME`/`data_dir()` are never consulted for the database's location. |
| `MESSAGING_MCP_SOURCE` | `mcp_server/config.py: source_prefix_from_env()` | Yes, for `mcp_server.server.main()` | Which of the four `source_prefix` values this one MCP server process speaks for; also names the built server (`f"messaging-{source_prefix.rstrip('_')}"`). Raises `ValueError` (not `Rejected`) if unset or not one of the four. |

### Database location, and why it must be on a native filesystem

The default on-disk path is `data_dir() / "messaging.sqlite3"`, i.e. `~/.messaging-mcp/messaging.sqlite3` unless `MESSAGING_MCP_HOME` overrides the parent directory. `config.assert_native_filesystem` is run against this path (and again, independently, inside `Database.__init__` for any explicitly-passed on-disk path — not only the default one) before it is used.

The guard exists because the schema turns on `PRAGMA journal_mode = WAL`, which depends on real POSIX file locking. It reads `/proc/mounts`, finds the longest matching mount point for the resolved path, and raises `RuntimeError` if that mount's filesystem type is one of `9p`, `drvfs`, `cifs`, `nfs`, `fuseblk`, `vboxsf` — the WSL2 passthrough types for non-native drives, common network filesystems, and a FUSE/VirtualBox shared-folder type, all of which emulate (or skip) locking in ways that can silently corrupt a WAL-mode database. If `/proc/mounts` cannot be read at all, or no mount matches, the guard does not raise — an absence of proof is not treated as proof of a problem. `:memory:` is never checked, since it has no filesystem path.

### `Database` construction options

`Database(path=None, schema=None)`. `path` accepts a filesystem path, `None` (falls back to `config.db_path()`), or the literal string `":memory:"`. `schema` accepts a path to a `.sql` file, or `None` (falls back to `config.schema_path()`, which resolves to the repository's own `schema/schema.sql` relative to this module — it is not itself environment-overridable). On construction, if the target database has zero tables, the schema file's full script is executed once; there is no migration mechanism, so a database file created under an older schema is never brought forward automatically.

The `:memory:` case is structurally different, not just a smaller default: a `:memory:` SQLite database exists only inside the single connection that created it, so `Database` routes both reads and writes through its one writer thread and its one shared connection for that case, with `PRAGMA query_only` toggled around each read job rather than opening a second connection. Every on-disk case instead gives each reading thread its own connection, opened with a `mode=ro` URI so the write permission is denied at the OS file-descriptor level — not by a togglable pragma a caller's SQL could flip back off — while a single dedicated writer thread is the only path that ever opens `BEGIN IMMEDIATE`/`COMMIT`.

---

## 8. The queue machinery

Six methods on `MessagingCore` are not client capabilities and have no MCP tool. They are what
the Polling Server drives, and they are documented here because a maintainer reading
`polling/server.py` will hit them immediately.

### advance

`advance(*, partner_id: int) -> dict | None`

The single implementation of the pushing mechanism. `send` and the drain thread both call it;
neither reimplements it.

Under the Partner's slot lock: read the queue head (two statements — the label, then the
row within it); promote it if the slot is empty or the head strictly beats the working
task; on a displacement, stop the remote *first*, then swap; render the promoted task
into a prompt and deliver it.

**Returns** `{"delivered": str, "resumed": bool, "displaced": str | None, "remote_call_id": str}`
or `None` if nothing moved. **Raises** `NeedsRemote` if no extension can deliver — the queue is
left untouched, because the extension is resolved before anything is written.

If delivery fails, the task is put back into the queue marked `in_process` and the exception
propagates. Silently dropping it is the one outcome nothing downstream could detect.

If the Partner has been archived, its queued work is deleted and the slot cleared.

### release

`release(*, partner_id: int) -> dict | None`

Empties the working slot because the remote finished its turn, returning what it held. Calling
it twice is harmless, which matters because a completion can be observed by a drain thread and
a push notification at the same moment.

### working_task

`working_task(*, partner_id: int) -> dict | None`

The task in the slot, or `None`. The only programmatic view of it; no query can show it.

### begin_summary_phase

`begin_summary_phase(*, partner_id: int) -> str | None`

Turns a working `[RESEARCH]` task into its own summary phase, in place. Returns the prompt to
deliver, or `None` if the slot is empty or holds a different label.

The task stays in the same slot; its `behavior` becomes `[TRUTHFUL-REPORT]` and its effective
priority is raised from 4 to 1, which blocks the queue: every arriving message waits, because
`advance` displaces only on a strictly lower priority number and nothing has one. Its
`body` stays the **original request** — if this phase is later displaced and resumed, the row
that goes back into the queue has to carry the request the summary is about, not the
instruction to summarize, which would summarize itself.

### reply_behavior

`reply_behavior(behavior: str) -> str | None`

`label_caps.reply_behavior` for a label. `None` means a finished task carrying it replies with
nothing, which is what makes an exchange terminate.

### report_back

`report_back(*, to_partner_id: int, from_partner_id: int, behavior: str, body: str) -> dict`

Pushes a finished Partner's answer into the Caller's own queue. Not `send`: there is no
requester holding a UUID here, and the handshake that made the exchange possible was
established in the other direction. It does keep the cap and the storage rule, because those
are about the Caller's queue rather than about who may talk to whom.

`behavior` must be something a Partner can **report**. `[RESEARCH]` is refused
with `not_reportable`: the first is delegation, and admitting it here would be a second door
into a superior's queue that skips the layer check `send` makes; the second is a hold and means
nothing in a queue. It is not reachable from the tool surface, but "not currently reachable" is
not a rule.

The other four all occur. `[MESSAGE-RESPONSE]` and `[TRUTHFUL-REPORT]` answer a finished task.
`[ERROR]` and `[QUERY]` are raised on a Partner's behalf by the Polling Server — those are the
cases a Partner cannot report itself, because an agent stopped on a permission prompt is not
running and nothing else is watching it.

### The prompt templates

`messaging_core/templates.py` renders what actually reaches a remote. A queue row holds the
Caller's raw text; the template names who is speaking, what the reply must contain, and — for
`[RESEARCH]` — exactly which paths the Partner may touch.

| Function | Used for |
|---|---|
| `research_dispatch` | A `[RESEARCH]` task. Inlines `partner_paths`, and says explicitly when there are none. |
| `truthful_report_request` | The summary phase. Quotes the original request back verbatim and excludes resumed-from work. |
| `resume_displaced` | A task returning to the slot. One line. |
| `relay` | Every other label, passed through with a `[Polling Server]` header rather than `[Polling Server messages you]` — the Server is showing the agent something, not telling it something. |
| `notebook_query` | A `[QUERY]` whose target is `nlm_`. Names the source it aims at, and carries no identity block. |
| `identity_block` | Not a template of its own — a section appended to `research_dispatch` and `relay`. |

Each mirrors a template in the project note "Prompt templates". Where the two disagree, the
note is the source of truth and the code is the bug.

**`notebook_query` exists because a notebook is not an agent.** `source_caps` says so in three columns — `can_execute = 0`, `can_send = 0`, `accepts_research = 0`. The generic `relay` announces a speaker, hands over a message, and closes with the call the recipient may answer with; only the middle third means anything to a source that holds documents and never acts.

It names the source it aims at, and that naming is the only aiming there is: the `nlm` CLI has no per-source query, so `deliver_message` asks the whole notebook and the prompt says where the answer should be drawn from. The template states that rather than implying a precision the remote does not enforce.

It carries no identity block, deliberately. With `can_send = 0` there is no agent behind the notebook to make that call, and an instruction nothing can follow is worse than none — it invites the reader to look for a capability that does not exist.

**`identity_block` exists because `send`'s first argument is `requester_uuid` — the agent's
own uuid — and no prompt ever stated it.** The research dispatch tells an agent, in so many
words, to message back a `[QUERY]` when it is missing context the Caller holds; without its
own uuid that is an instruction it has no credentials to carry out. The block states the
agent's title and uuid, and gives it the call already filled in.

The second half of the block is load-bearing in the opposite direction. Whatever the agent
produces is harvested by the Polling Server and delivered to the Caller when the turn ends,
so an agent handed only its identity would reasonably use it to send its answer — and the
Caller would receive the same work twice, once harvested and once sent. So the block says
plainly that answering is automatic, and that `send` is for the case where the turn is *not*
finishing: a `[QUERY]` for missing context, an `[ERROR]` when blocked.

It is appended to `research_dispatch` (long autonomous work, the case the dispatch itself
tells the agent to interrupt) and to `relay` (which is what hands an agent an `[ERROR]`
saying something is blocked). It is deliberately **absent** from `truthful_report_request`,
`truthful_report_request`, `notebook_query` and `resume_displaced`: a summary is harvested, a
notebook cannot send, and a resume line is one line on purpose.

**There is no template for a wait, and that is the point.** An agent stopped on its own
unanswered question is not being told anything. The remote is stopped by
`stop_remote_execution` before the slot changes hands, so the slot is taken and nothing is
rendered or delivered — handing a stopped agent a paragraph gives it something to act on when
the entire purpose of the wait is that it should be doing nothing until it hears back.
