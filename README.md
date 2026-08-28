# messaging-MCP

Agents delegate work to agents in three remote applications, and no agent ever polls for a result.

## Install

Python 3.12+, no third-party runtime dependencies beyond the MCP SDK.

```bash
pip install mcp
```

## Usage

Each MCP server process speaks for exactly one remote. Start one per source:

```bash
export MESSAGING_MCP_SOURCE=science_        # nlm_ | science_ | gemini_
export MESSAGING_MCP_DB=~/.messaging-mcp/messaging.sqlite3   # optional
python3 -m mcp_server.server
```

Set `MESSAGING_MCP_STUB=1` to run against an in-memory fake instead of the real remote.

The database must sit on a native filesystem. On WSL, `/mnt/c` is a 9p mount where SQLite in
WAL mode can corrupt; `messaging_core.config.assert_native_filesystem` refuses such a path up
front rather than letting you find out later.

Verify an install:

```bash
python3 -m pytest -q
python3 tests/mutation_run.py                                 # every mutant caught by a NAMED test
python3 tests/test_schema_constraints.py schema/schema.sql    # 63 passed, 0 failed
```

## What it is

`MessagingCore` (`messaging_core/core.py`) is a **logic class, not a server** — no port, no
socket, no process. Three MCP server processes each construct their own instance and expose its
seventeen capabilities as tools. What makes them behave as one system is not a protocol between
them: they all point at the same SQLite file. The database *is* the integration layer.

Each Partner has **one priority queue** and one in-memory working slot. Every message is a push;
the label decides how urgently it is taken up relative to whatever the Partner is already doing.

```
[TRUTHFUL-REPORT] > [QUERY] = [ERROR] > [MESSAGE-RESPONSE] > [RESEARCH]
```

There is no label for "stop". An agent is stopped by what it sends: a `[QUERY]` or an `[ERROR]`
says it cannot continue without an answer, so its remote is stopped, its work is paused back
into its own queue, and the question takes its working slot until the answer arrives. Nothing
below `[TRUTHFUL-REPORT]` can reach it while it waits — an unanswered question is the blocker.

Everything a label implies — its priority, its cap, whether it is stored, what a finished task
carrying it replies with — is a row in `label_caps`, not a branch in code.

## Layout

| Path | What is in it |
|---|---|
| `messaging_core/` | The capabilities, the queue, the working slot, prompt templates, the SQLite wrapper |
| `schema/schema.sql` | 12 tables, 5 triggers. The authority for every rule that can live in the database |
| `extension/base.py` | The boundary to a remote: four abstract methods, and refusing defaults for the rest |
| `adapters/` | One per remote — NotebookLM, Claude Science, Antigravity |
| `polling/server.py` | One drain thread per Partner. Polls remotes so no agent has to |
| `mcp_server/` | The MCP tool surface |
| `docs/` | Architecture, reference, lifecycle, runbook, invariants |
| `visualizations/` | Ten mermaid sources and their rendered PNGs (`./render-diagrams.sh`) |
| `tests/` | Unit, boundary, adapter and schema suites, plus a mutation pass |

## Documentation

Read in this order depending on why you are here:

- **Why is it shaped this way** — `docs/01-architecture-and-rationale.md`
- **What does this call do** — `docs/02-reference.md`
- **Where does a message go** — `docs/03-message-lifecycle.md`
- **Something is broken** — `docs/04-operating-and-debugging.md`
- **Can I change this rule** — `docs/05-invariants-and-constraints.md`

## Contributing

Docs change in the same commit as the code they describe. That is the only mechanism that
reliably prevents staleness; every other approach relies on someone remembering.

Two rules worth stating before you touch anything:

**A surviving mutant is a missing assertion.** `tests/mutation_run.py` breaks the source on
purpose and requires a *named* test to fail for each break. If you add a rule, add a mutant.

**A column with no writer is worse than no column**, because it reads like a measurement
someone is taking. The schema audit in `docs/05` §"Capabilities no remote fully implements" is
where honest gaps are recorded rather than smoothed over.
