# Feature report — 2026-08-28 (diagram sync)

## All ten diagrams synced to the codebase, and the drift made mechanical

The `[IDLE]` removal earlier today updated five diagrams — the five that happened
to mention `[IDLE]` or `interrupt_partner`. Nobody audited the other five, or
re-checked the five that *were* touched for anything else that had gone stale.
This pass audited all ten against the code.

### Why the drift was invisible

`tests/doc_consistency.py` read all ten diagrams into `ALL_MMD` at line 48 and
**never used the variable**. Only `03-schema-er.mmd` was checked, and only for
its table and column lists. The `GONE` dead-reference sweep that exists
specifically to catch deleted identifiers scanned `DOCS` and never `MMD`.

Nine of ten diagrams were mechanically unverified. Compounding it, `docs/02:274`
and `docs/04:350` both linked `visualizations/07-handshake-legality.png`, which
did not exist — so nobody rendered the diagrams either, and nobody read them.

### The defects found

**`07-handshake-legality.mmd` was structurally wrong**, and this was the worst.
`MessagingCore.handshake` evaluates the `gemini_ → gemini_` inheritance branch
between the `code_` branch and the orchestrator gate (`core.py:1315`), for the
same reason the `code_` branch comes first: neither participant can hold an
orchestrator role, so a rule about roles could never be satisfied. The diagram
had no such branch. Consequences:

- `gemini_already_inherited` — the "a lineage is a line, not a fork" rule — was
  drawn nowhere at all.
- `no_handshake_between_gemini` was drawn unconditionally in the wrong place; it
  fires only for a *same-project* pair.
- That branch's own `different_project` refusal was missing.
- As drawn, a `gemini_` requester reached `requester_not_orchestrator` — making
  the diagram's entire gemini path unreachable.
- `OK_CODE` and `OK_EXT` were drawn as terminal; both actually fall through to
  the `duplicate_handshake` gate, so a repeat handshake is refused, not created.

The file was rewritten. All 17 codes `handshake` can raise are now drawn, and
nothing is drawn that it cannot raise.

**`05-working-slot.mmd` had the worst single factual error.** It claimed
`[QUERY] -> [MESSAGE-RESPONSE]` and "everything else -> NOTHING". Three labels
have a non-NULL `reply_behavior`: `[ERROR] -> [MESSAGE-RESPONSE]` and
`[RESEARCH] -> [TRUTHFUL-REPORT]` were both wrong. The file contradicted itself —
its own note two lines below correctly said "the reply for two of the five labels
is NOTHING". Also corrected: "never before a different label" (false for a
displaced wait, which outranks across labels), "prompt is one line" (false for a
resumed wait, which renders nothing, and for a summary phase, which renders the
full report request), and "a stop that fails leaves everything as it was" (false
for a designed refusal, which is recorded and the swap proceeds).

**`04-priority-queue.mmd`** omitted `awaiting_resolution` as the leading sort key
of *both* head statements — the fact the whole wait mechanism rests on, in the
diagram that owns queue ordering. Its single `SENDER --> CAP` edge also asserted
that `send` touches only the target's queue, which stopped being true when
blocking labels began stopping their sender. Both added.

**`06-research-round-trip.mmd`** attributed the summary reply to
`label_caps.reply_behavior`, which is NULL for `[TRUTHFUL-REPORT]` and cannot be
the cause; the `summary_phase` marker is (`polling/server.py:678`). It also
omitted the mid-task question flow entirely — although `templates.research_dispatch`,
the prompt this very diagram delivers, instructs the agent to "message back a
`[QUERY]` and idle", and ships its uuid for that purpose alone.

**`02-module-map.mmd` was the most stale**, never having been audited: "the six
labels" (five), "the 15 capabilities" (23 public methods, 17 with MCP tools),
"PollingServer: state machine" (`polling/server.py:15` says there deliberately is
none), `slots.py` and `templates.py` missing entirely though both are runtime
dependencies of `core.py`, two phantom edges (polling imports neither `db` nor
`labels`; the NotebookLM adapter does not import `errors`), and five missing
edges including `POLLING -> CORELOGIC`, which polling both imports and constructs.

**`01-system-context.mmd`** named two servers that do not exist —
`mcp_server/server.py:869` builds names from `source_prefix.rstrip('_')`, so they
are `messaging-nlm` and `messaging-gemini`, not `messaging-notebooklm` and
`messaging-antigravity`. It inverted the code/bridge cardinality (a *bridge*
holds at most one `code_` partner, not the reverse), drew one shared Polling
Server when there is one per MCP process holding exactly one extension, and
attributed the three permission calls to the Polling Server when they are made by
`MessagingCore` in the MCP process.

**`09`** claimed the same-project rule is lifted "only between identical roles" —
false for the `gemini_` pair, which inverts it. **`10`** mislabelled the job
queue as "one item per write" when `:memory:` reads and a `_STOP` sentinel ride
it too. **`03`** said "the six labels".

### Two stale claims in the code itself

- `messaging_core/core.py` still explained `_UNCANCELLABLE` by saying Claude
  Science "has no usable interrupt at all" — repudiated by
  `adapters/claude_science/adapter.py:405`, which posts to
  `/api/frames/{id}/cancel`. The mechanism is still needed (NotebookLM never
  executes); only the example was wrong.
- `tests/test_core_capabilities.py:7` still named `interrupt_partner`.

### The durable half

Six checks added to `tests/doc_consistency.py`, taking it from 146 to 186:

| Check | Rule |
|---|---|
| `mmd/labels` | every `[LABEL]` token in any diagram is a live `label_caps` row |
| `mmd/handshake-branches` | every code `handshake` raises is drawn in 07, and nothing else is |
| `mmd/extension-coverage` | every `RemoteExtension` method is drawn in 08 |
| `mmd/triggers` | every schema trigger is named in the ER diagram |
| `mmd/rendered` | no `.mmd` is newer than its `.png` |
| `dead-ref` | extended to scan diagrams, and to know the removed names |

Each was verified to fail by name before being trusted. A blanket "every
identifier in a diagram must exist" was prototyped and rejected — Mermaid's `\n`
escapes manufacture tokens like `nlabel_caps`, giving 60+ false positives.
Coverage checks in the inverted direction have none.

### Rendered images

All ten `.mmd` now render to `.png` via `./render-diagrams.sh`, so the two doc
references resolve without editing them, and the diagrams are something a person
can actually look at. `mmd/rendered` is what stops the image becoming a third
drifting surface; it skips when a `.png` is absent, because git does not preserve
mtimes and a check that cries wolf on every clone is one people learn to ignore.
