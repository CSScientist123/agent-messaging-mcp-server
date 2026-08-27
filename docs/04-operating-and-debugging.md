# How to run and debug the messaging system

**Audience.** An engineer operating this system, or debugging it while something is broken.

**Scope.** Starting a server, verifying an install, and a runbook of the failures you are
likely to hit, each with the commands to run.

**Non-scope.** Why the system is shaped this way — that is
`docs/01-architecture-and-rationale.md`. How a message is routed — that is
`docs/03-message-lifecycle.md`. Parameter and rejection-code detail — that is
`docs/02-reference.md`. This document is action only; where you need a reason, follow the
link rather than expecting one here.

**Assumed prior knowledge.** Python, SQLite, and a shell.

All commands run from the repository root, `messaging-MCP/`.

## Configuration

Three environment variables control the server itself:

| Variable | Meaning |
|---|---|
| `MESSAGING_MCP_SOURCE` | Which source this server speaks: `nlm_`, `science_`, or `gemini_` |
| `MESSAGING_MCP_DB` | Database path. Defaults to `~/.messaging-mcp/messaging.sqlite3` |
| `MESSAGING_MCP_STUB` | Set to `1` to use a stub extension instead of the real adapter |

Each adapter then needs whatever reaches its own remote. These are **not** optional for a
real deployment, and none of them fails at start-up — an adapter builds fine without them and
the first delivery is what fails:

| Variable | Used by | Meaning |
|---|---|---|
| `CLAUDE_SCIENCE_BASE_URL` | `science_` | Where Claude Science is listening. Defaults to `http://127.0.0.1:8000` |
| `CLAUDE_SCIENCE_COOKIE` | `science_` | Session cookie for that instance. Defaults to empty, which authenticates as nobody |
| — | `nlm_` | The `nlm` CLI must be on `PATH` (or pass `nlm_path=`). A missing binary raises `NlmBinaryMissing` |
| — | `gemini_` | The `tmux` binary must be on `PATH` (or pass `tmux_path=`). A missing binary raises `TmuxBinaryMissing` |

An empty `CLAUDE_SCIENCE_COOKIE` is the one worth watching: it is a valid configuration that
produces authentication failures on every request rather than a refusal to start, so the
symptom shows up as `[remote failed]` on the first `send` rather than anywhere near the cause.

`code_` has no adapter. A Claude Code session is local and has no remote presence for an
adapter to reach; it participates by handshaking one `bridge-scientist` on the Claude Science
side. Asking for a `code_` server refuses by design.

The database must sit on a native filesystem. On WSL, `/mnt/c` is a 9p mount and SQLite in
WAL mode will corrupt there; `messaging_core.config.assert_native_filesystem` refuses such a
path rather than letting you find out later.

## To start a server

```bash
export MESSAGING_MCP_SOURCE=science_
python3 -m mcp_server.server
```

To run against a stub instead of the real remote, set `MESSAGING_MCP_STUB=1` first.

## To verify an install

Run all three. Expect exactly this:

```bash
python3 -m pytest -q
python3 tests/mutation_run.py                         # every mutant caught by a NAMED test
python3 tests/test_schema_constraints.py schema/schema.sql   # 63 passed, 0 failed
```

The counts for the first two move whenever tests are added, which is why only the schema
suite's is pinned here — that one is a fixed set of rules and a drop in it means a rule was
deleted. For the other two, what matters is `0 failed` and that no mutant survives; a
surviving mutant is a missing assertion, not a tolerable result.

**Run the mutation pass alone, and read its last two lines before its table.** It deliberately
breaks the source and reverts each change, so a second copy running at the same time will
restore to its own snapshot and silently overwrite the first one's — which leaves the tree
broken under a clean-looking report. It now refuses to start if `.mutation_running` exists,
snapshots to `.mutation_backup/` before touching anything, verifies every restore, and
self-heals at the start of the next run if it was killed.

What to check when it finishes:

- `tree restored: True` and `failing set back to baseline: True`. If either is false, or a
  `WARNING:` line names files that were still mutated, **every mutant after the one that failed
  to restore was measured against broken code** — re-run before trusting anything.
- No `SURVIVED` rows. A surviving mutant is a missing assertion, not a tolerable result.

If a run was killed and you want to check the tree by hand, two spot-checks catch the mutants
that hurt most: `_HEAD_LABEL_SQL` in `messaging_core/core.py` must read `MIN(c.priority) ASC`,
and `_ADMIT_SQL` must still contain `+ :working`. Both have been left inverted by a killed run,
and both leave the suite green while the system is wrong.

## Runbook

Each entry runs symptom, verification, mitigation, diagnosis, resolution, escalation. Stop
the bleeding before you diagnose.

### `send` keeps returning `[rejected] over_queue`

**Verify.** The cap is per `(partner, caller, label)`, so query it that way — a total depth
tells you nothing about whose allowance is full:

```sql
SELECT q.caller_id, q.behavior, COUNT(*) AS queued, c.max_outstanding
  FROM message_queue q
  JOIN label_caps c ON c.behavior = q.behavior
 WHERE q.partner_id = :partner_id
 GROUP BY q.caller_id, q.behavior;
```

**Mitigate.** Do not retry, and do not raise the cap. A full allowance means that Caller
already has that much of that kind of work outstanding; retrying adds load and changes
nothing.

**Diagnose.** The count above can look one short of the cap and still refuse, and that is
correct rather than a bug: **the working slot counts toward the cap and is not in the
database.** Ask the Partner itself what it is working on:

```
status(requester_uuid=<the partner's own uuid>)
```

The `working` field names the label and the Caller. If that Caller and label match the ones
being refused, the allowance is genuinely full.

If the queued count alone is already at the cap and not falling, nothing is picking work up.
Check for a live drain thread:

```sql
SELECT * FROM drain_threads WHERE partner_id = :partner_id;
```

**Resolve.** If no row exists, call `notify_partner_push` with the Partner's UUID. If a row
exists but the depth is not falling, the drain thread is alive and its remote is not
finishing — follow the stuck-slot entry below.

**Escalate.** If queued rows alone exceed the cap, the single-statement admission has been
bypassed. That is a correctness bug, not an operational one.

### A Partner never receives work that was accepted

**Verify.** Confirm the message was admitted and is still waiting:

```sql
SELECT id, behavior, in_process, enqueued_at FROM message_queue WHERE partner_id = :partner_id;
```

**Mitigate.** None available without diagnosis — do not re-send, or you will queue it twice.

**Diagnose.** Work in `message_queue` has been admitted but not promoted. Three causes, in
the order worth checking:

1. **Something higher-priority holds the slot.** Call `status` on the Partner and look at
   `working`. A `[RESEARCH]` waits behind a `[QUERY]` by design, and behind an `[IDLE]`
   indefinitely — an `[IDLE]` hold is released by the next arrival, not by time.
2. **No drain thread is running** (see `drain_threads` above).
3. **The adapter's `deliver_message` is failing.** Check whether the server is
running against a stub:

```bash
echo "$MESSAGING_MCP_STUB"
```

A stub accepts every delivery and sends nothing anywhere. This is the most common cause of
"accepted but never arrived."

**Resolve.** Unset `MESSAGING_MCP_STUB` and restart the server, then confirm the extension is
real:

```bash
python3 -c "from mcp_server.config import build_extension; print(type(build_extension('science_')).__name__)"
```

**Escalate.** If the extension is real and delivery still fails, the remote is refusing.
Check the adapter's own error; each raises a named exception rather than failing silently.

### A Partner is stopped and never starts again

**Verify.** Ask it what it holds:

```
status(requester_uuid=<the partner's own uuid>)
```

An `[IDLE]` in `working` means the Partner was interrupted. Its previous task is in the
queue, paused:

```sql
SELECT id, caller_id, behavior, in_process, enqueued_at
  FROM message_queue WHERE partner_id = :partner_id AND in_process = 1;
```

**Mitigate.** None needed — the Partner is stopped, which is what was asked for. Nothing is
being lost.

**Diagnose.** An `[IDLE]` hold is released by the next message that arrives, whatever its
label. It is not released by time and there is no timeout. So the question is not "why is it
stuck" but "who was supposed to send the next thing" — and that is the Caller who called
`interrupt_partner`. Its id is the `caller_id` on the `[IDLE]`'s displaced sibling, or the
`caller_id` shown in `working`.

**Resolve.** That Caller sends what the Partner was stopped for: an `[ERROR]` naming what
went wrong, or a `[MESSAGE-RESPONSE]` answering it. Either displaces the hold, and the paused
task resumes after it, with a one-line "resume your previous …" prompt.

If the interruption was caused by a permission prompt, correct the grant first — that is what
the doctrine in `docs/05` §20 requires, and sending work again without correcting it produces
the identical prompt:

```
get_permissions(requester_uuid=…, partner_title=…)
add_permissions(requester_uuid=…, partner_title=…, write_paths=[…])
send(…)
```

**Escalate.** If the queue holds a paused task and `working` is empty and no drain thread
row exists, nothing will ever pick it up. Call `notify_partner_push`. If the row exists and
the task still does not move, that is a correctness bug in `advance`.

### A task completed instantly and the answer is missing

**Verify.** Look at what came back:

```sql
SELECT behavior, created_at, substr(body, 1, 60) FROM messages
 WHERE to_partner = :caller_id ORDER BY id DESC LIMIT 5;
```

The signature is a reply whose `created_at` is within a second or two of the send, carrying
`[result reported by the remote through its own channel]`. That placeholder is **legitimate for
Claude Science and Antigravity** — neither implements `read_remote_result` — so it is the
*timing*, not the body, that tells you something went wrong.

**Mitigate.** Re-send. The message was delivered; only the waiting was skipped, so the partner
may well have already answered it in its own transcript.

**Diagnose.** The drain loop's first `poll_completion` observed the remote's state from
*before* the delivery, decided the turn was finished, and closed the task. For a TUI-driven
remote this means the pane had not repainted out of its idle footer yet. Check the partner's
transcript directly:

```bash
tmux capture-pane -t agy-<first 8 chars of the conversation id> -p | tail -30
```

An answer sitting there that never reached the caller confirms it.

**Resolve.** `AntigravityExtension.deliver_message` waits for the busy footer before returning
(`_await_busy`). If this recurs, that wait is either too short for the machine or has been
removed — `docs/05-invariants-and-constraints.md` §22 states the rule and the two regression
tests that pin it.

**Escalate.** If it happens against a remote whose completion is read from an **API** rather
than a pane, this is a different bug: an API returns the state as of the request, so it cannot
be stale in the same way. Look instead for a status value missing from the adapter's busy set.

### A capability returns `NeedsRemote` in production

**Verify.** The response names the capability and the extension method it needed.

**Mitigate.** The local work already happened; only the remote step did not. Do not retry
blindly — for `create_partner` or `create_project`, retrying can leave a second registration.

**Diagnose.** Either no extension is configured, or the configured one speaks a different
source. A `MessagingCore` speaks exactly one source, so a cross-source flow needs two cores
over one shared database.

**Resolve.** Confirm `MESSAGING_MCP_SOURCE` matches the Project's `source_prefix`:

```sql
SELECT title, source_prefix, project_system_id FROM projects;
```

**Escalate.** Claude Science's `stop_remote_execution` raises a refusal
that no configuration fixes — no remote implements those. Read the closing section of
`docs/05-invariants-and-constraints.md` before hunting further.

### `create_partner` fails with `live_partner_limit`

**Verify.**

```sql
SELECT COUNT(*) FROM partners WHERE project_id = :pid AND archived_at IS NULL;
```

**Mitigate.** Archive a Partner you have finished with. That frees a slot immediately.

**Diagnose.** The ceiling is `source_caps.max_live_partners`, ten by default, enforced by the
`partners_live_limit` trigger.

**Resolve.** Call `archive_sessions` as the Project's `project-orchestrator`. Archiving
spends the Partner's title permanently — there is no rename capability — so choose new titles
accordingly.

**Escalate.** If a project legitimately needs more than ten, change the row in `source_caps`
rather than the trigger. The limit is data.

### A handshake is refused and the caller believes it should be allowed

Read the rejection code first — there are eleven distinct ones and each names a different
rule. `docs/02-reference.md` indexes them, and `visualizations/07-handshake-legality.png` is
the decision tree in the order `handshake` actually evaluates it.

Three that are commonly misread:

- `cross_project_requires_same_role` — the two Projects ARE linked by a `project_extension`
  row, but the Partners hold different roles. An extension branches an effort sideways; it is
  not a second chain of command.
- `code_handshakes_bridge_only` — a `code_` Partner has exactly one legal counterpart, a
  `bridge-scientist`. Nothing else, in either direction.
- `gemini_single_science_source` — the Antigravity Partner is already taking direction from a
  different Claude Science session. Check `handshakes` for an existing row pointing at it.
- `requester_not_orchestrator` **from a `gemini_` or `nlm_` partner** — expected, and not
  fixable by claiming a role. All three orchestrator roles are Claude Science only, so
  `claim_orchestrator` will refuse with `orchestrator_requires_science_project`. A `gemini_`
  partner never initiates a handshake; a `science_` gemini-orchestrator initiates it toward
  the `gemini_` partner.


**Verify.** The rejection code names the rule. Check both ends:

```sql
SELECT p.title, p.orchestrator_type, pr.source_prefix, pr.title AS project
  FROM partners p JOIN projects pr ON pr.id = p.project_id
 WHERE p.title IN (:a, :b);
```

**Mitigate.** None — a refused handshake changed nothing.

**Diagnose.** The most common surprises: a `code_` Partner can hold no handshake in any
direction and reaches only `nlm_`, which needs none. `gemini_ → science_` is never legal, but
`science_ → gemini_` from the `gemini-orchestrator` is — the direction is asymmetric. That
role is held by a **Claude Science** agent, not an Antigravity one. Two same-source Partners
must share a Project; a cross-source pair is necessarily in different Projects.

**Resolve.** Grant the gemini budget if the code was `no_gemini_budget`; claim the right role
if it was `requires_gemini_orchestrator`.

**Escalate.** If a rule looks wrong rather than misapplied, read the handshake section of
`docs/02-reference.md` before changing anything — the asymmetries are deliberate.

### The database is locked, or a write hangs

**Verify.** Confirm the path is on a native filesystem:

```bash
python3 -c "from messaging_core.config import db_path; print(db_path())"
df -T "$(python3 -c 'from messaging_core.config import db_path; print(db_path().parent)')"
```

**Mitigate.** Stop the process. A hung writer holds a transaction open.

**Diagnose.** All writes funnel through one writer thread inside `BEGIN IMMEDIATE`, so
writers do not contend with each other. Lock errors usually mean a second process is writing
the same file, or the file is on a 9p or network mount where WAL does not work.

**Resolve.** Move the database to a native path, or stop the second writer.

**Escalate.** If a *read* reports a lock, something has obtained a writable connection
outside the writer thread. Reader connections are opened `file:...?mode=ro` and cannot write.

### An MCP server starts but every remote call fails

**Verify.**

```bash
python3 -c "from mcp_server.config import build_extension; e=build_extension('science_'); print(type(e).__name__, e.source_prefix)"
```

**Mitigate.** Set `MESSAGING_MCP_STUB=1` to keep the local surface working while you fix the
remote, accepting that nothing is delivered.

**Diagnose.** Each adapter depends on something external: NotebookLM needs `nlm` on `PATH`,
Antigravity needs `tmux` and a live session, Claude Science needs its local API reachable and
authenticated. Each raises a named error rather than a generic one.

**Resolve.** Restore the dependency the named error identifies.

**Escalate.** `adapters/live_smoke.py` exercises each adapter against its real remote and
prints a per-step result. It is manual and is never collected by pytest.

## Useful queries

Queue depth per Partner, in pop order — which is what actually happens next:

```sql
SELECT q.partner_id, q.behavior, c.priority, q.in_process, COUNT(*) AS n
  FROM message_queue q JOIN label_caps c ON c.behavior = q.behavior
 GROUP BY q.partner_id, q.behavior, q.in_process
 ORDER BY q.partner_id, c.priority, q.in_process DESC;
```

Paused work, per Partner — a task displaced from the working slot and waiting to resume:

```sql
SELECT partner_id, caller_id, behavior, enqueued_at
  FROM message_queue WHERE in_process = 1 ORDER BY partner_id;
```

Which Partners have a drain thread. Note what this does NOT tell you: the working slot is in
process memory, so a Partner with a thread and an empty queue may still be mid-task. `status`
is the only way to see that.

```sql
SELECT d.partner_id, p.title, d.thread_id, d.started_at
  FROM drain_threads d JOIN partners p ON p.id = d.partner_id;
```

A Partner's INTENDED path grants. What the conversation actually allows is a different
question — `get_permissions` answers that one, and the two are allowed to differ:

```sql
SELECT kind, path FROM partner_paths WHERE partner_id = :pid ORDER BY kind, path;
```

Project extensions, and which Partners could handshake across one:

```sql
SELECT e.project_a, e.project_b, p.title, p.orchestrator_type
  FROM project_extension e
  JOIN partners p ON p.project_id IN (e.project_a, e.project_b)
 WHERE p.archived_at IS NULL AND p.orchestrator_type IS NOT NULL
 ORDER BY e.project_a, p.orchestrator_type;
```

What a Partner can actually read back — remember `[RESEARCH]`, `[ERROR]` and `[IDLE]` are
never stored (`label_caps.stored`):

```sql
SELECT behavior, created_at, substr(body, 1, 80) FROM messages
 WHERE to_partner = :pid ORDER BY id DESC LIMIT 20;
```
