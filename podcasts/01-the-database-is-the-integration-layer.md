# One: The database is the integration layer

## The constraint that shapes everything

Start with the sentence the whole system is built to make true: **an agent hands work to another agent and moves on, and no agent ever polls for the result.**

That sounds like a convenience. It is not. It is a constraint that reaches into every table and every thread in this codebase, and almost every design decision you will meet later is downstream of it.

Consider what happens without it. An agent delegates a research task to another agent living inside a different application. It now has to know when that work is done. The obvious way is to ask — repeatedly, on some interval, until the answer changes. Every one of those asks is a turn the agent spends not thinking. Worse, the loop has to live somewhere: inside the agent's own reasoning, where it competes for attention with the actual work, and where the agent must remember it is waiting at all. An agent that gets distracted mid-loop simply never comes back. An agent that polls too eagerly burns its context on "not yet." And an agent that delegates three things at once has three loops interleaved in one head.

So the waiting is moved out. Something else watches the far side, notices when it finishes, notices when it gets *stuck*, and pushes the answer back only once there is one. That something is this system.

Everything else follows. Because the waiting is external, the state it waits on has to be somewhere both the waiter and the delegator can see. Because the delegator moved on, the answer has to arrive as a push rather than a return value. Because pushes can arrive while an agent is busy, there has to be a queue. Because a queue can hold several things at once, something has to decide what runs next. And because the far side is a real application that can hang, refuse, or ask a question of its own, the waiter has to be able to distinguish "still working" from "stuck" from "finished" — three states that, as you will see in a later note, look identical if you only glance once.

## Three processes and one file

Three applications sit behind this system, and each is a different kind of thing.

**NotebookLM** is a source of context. You load material into a notebook and ask it questions about that material. It answers; it never acts. There is no agent inside a notebook that decides to do something.

**Claude Science** runs frames of agentic work against a local HTTP API. A frame is a unit of work inside a project, and it genuinely executes — it runs tools, writes files, produces results.

**Antigravity** runs conversations inside `tmux` sessions, driven by the `agy` command-line client. It also genuinely executes, but through a terminal user interface rather than an API — a distinction that turns out to matter enormously, and gets its own note.

Each of these gets its own MCP server process. Three processes, one per remote that has an adapter. And here is the part that surprises people: **there is no fourth process coordinating between them.** There is no message broker, no bus, no orchestrating service. There is no network protocol between the three at all.

What makes them behave as one system is that they all point at the same SQLite file, governed by the same schema.

Say that plainly, because it is the load-bearing idea of the entire architecture: **the database is the integration layer.**

When a Claude Science partner establishes a relationship with an Antigravity partner, the row that authorizes it lands in the one `handshakes` table that both of those processes read from. When one process admits a message into `message_queue`, that row is visible — immediately, without anyone being told — to whichever process is watching the target. Nobody sends a notification. Nobody needs to. The fact is simply *true* in a place both can see.

This is why there is no fourth process. A coordinator would exist to tell process B what process A did. But process B can look.

## A logic class, not a server

The name `MessagingCore` invites a misreading worth heading off immediately.

`MessagingCore` has no transport. No port. No listening socket. No process of its own. It is an ordinary Python class holding a database handle and, optionally, one remote extension. Each of the three MCP server processes constructs its own instance and exposes that instance's seventeen capabilities as MCP tools.

So when you read "the messaging server," do not picture a service somewhere that requests travel to. Picture a library, loaded three times, in three processes, each talking to the same file.

This has a consequence worth stating because it is easy to get wrong when reasoning about the system: **there is no such thing as one `MessagingCore` calling another.** They never communicate. Three instances of a class, each reading and writing the same rows, is the entirety of the coordination mechanism. When you find yourself wondering how process A "tells" process B something, the answer is always the same: it does not. It writes a row, and B reads it.

## Everything writes through one thread

If three processes share one SQLite file, the obvious worry is concurrency. The answer is more disciplined than you might expect.

Inside a single process, **every write goes through one dedicated writer thread.** Not one connection per caller with locking hoped for — one thread, holding one connection, consuming a job queue. Callers submit a function that takes a connection and returns a value; the writer thread runs it inside `BEGIN IMMEDIATE` and hands back the result through a future.

`BEGIN IMMEDIATE` matters. SQLite's default transaction is deferred: it takes a read lock first and upgrades to a write lock when a write actually happens. Two transactions that both read and then both try to upgrade will deadlock, and SQLite resolves that by failing one of them with "database is locked" *after* it has already done work. `BEGIN IMMEDIATE` takes the write lock up front. Contention becomes waiting, which is boring and correct, rather than a failure partway through.

Reads do not go through the writer thread. Each thread that reads gets its own connection, opened as a **read-only URI** — `file:...?mode=ro`. Not a convention, not a promise: the connection is physically incapable of writing. A `PRAGMA` cannot re-enable it. This means a bug that tries to write through a read path fails immediately and loudly, at the wrong-connection boundary, rather than succeeding and quietly bypassing the single-writer discipline that everything else depends on.

There is one exception, and it is instructive. An in-memory database (`:memory:`) has no file for a second connection to open — each connection would get its own private, empty database. So for in-memory use, reads are routed through the writer thread's own connection with `PRAGMA query_only` set for the duration. The invariant is preserved; the mechanism differs, because the mechanism *has* to differ.

Across processes, SQLite's own locking does the rest, with WAL mode and a busy timeout. And the database must sit on a native filesystem — WAL mode over a network mount will corrupt, so a path on one is refused outright rather than allowed to fail later, mysteriously, under load.

## Titles address, UUIDs identify

Two kinds of name run through this system and they answer different questions.

A **title** is a human-readable address. It is what one agent types to name another — `handshake` takes a `partner_title`, `send` a `queried_partner_title`. Titles are unique server-wide — not per project, server-wide — and they are resolved to a row **immediately, at the boundary**, the moment a capability looks one up.

A title carries no type information at all. Nothing about the string tells you whether it names a Claude Science partner or an Antigravity conversation. That is deliberate. Agents address each other by name; the system resolves the name into something typed and then works with that.

A **UUID** is an identity credential. It is minted once, at partner creation, shown exactly that one time, and used as `requester_uuid` for every subsequent call that partner makes. Everywhere inside the system — every join, every check, every foreign key — identity travels as an internal row id or a UUID. Titles exist for humans and agents to type, and internal plumbing never touches them except to translate one into the other.

There is one sanctioned exception to never returning a UUID: creation itself returns the new partner's uuid, because that is the identity being minted for whoever will *become* that partner. It is not a leak of someone else's identity to a third party. Every other capability treats a UUID as something to check, never something to disclose.

Now the part that catches people out. **Archiving spends a title permanently.** It leaves the partner's row in place and its title taken, and renaming an archived partner is refused by a database trigger rather than merely by application code.

Deletion is the opposite: it removes the row, and the title becomes available again. That asymmetry is deliberate and is why the two capabilities are scoped differently — archiving frees a live-partner slot and leaves the row and its spent title behind, and deletion does not.

The reason is worth sitting with, because it is the shape of reasoning this whole codebase uses. If an archived partner could be renamed, its title would be freed. The next partner to take that title would silently inherit an address that other agents may still be holding — pointing at something that is not what they think it is. A message meant for the old partner would reach the new one, and nothing anywhere would look wrong.

Note also *where* that rule is enforced. It could have lived in the archiving capability. It lives in a trigger instead, because a rule that exists only in application code is bypassed by any path that issues the `UPDATE` directly. The distinction between "this rule is enforced by the database" and "this rule is enforced by whichever function remembers it" runs through the whole system, and the system is consistently honest about which is which.

## Four sources, and capability as data

Every project declares exactly one **source prefix**, and every partner inherits its project's. There are four: `nlm_`, `code_`, `science_`, and `gemini_`.

What each source can do is not a branch in code. It is a row in `source_caps`, with four flags: `can_execute`, `needs_handshake`, `can_send`, `accepts_research`.

For three of the four sources, every flag is 1. NotebookLM is the exception, and every one of its zeros says something specific:

- **`can_execute = 0`** — a notebook never runs anything. There is no turn to stop, which is why an attempt to interrupt one is refused as `not_executable` rather than quietly doing nothing.
- **`needs_handshake = 0`** — a notebook may be messaged directly, with no relationship established first. A handshake exists to authorize one agent to direct another; there is no agent here to direct.
- **`can_send = 0`** — a notebook never originates a message. There is nothing behind it that decides to speak.
- **`accepts_research = 0`** — delegated work asks its recipient to go and do something. A notebook answers questions about what it already holds.

Keeping these as data rather than conditionals has a concrete payoff, and it is worth stating precisely rather than generously. It does **not** mean a new source is one row — a source prefix is also named in a schema constraint, in the core's own list of valid prefixes, in the server's configuration, and in the adapter registry, and all four would need it.

What it does mean is that no *rule* branches on a source name. There is no list of `if source == "nlm_"` scattered through the codebase deciding who may send, who needs a handshake, who executes, and who takes delegated work — so there is no possibility of finding four of those five places and missing the fifth. The behaviour is read from the columns, every time.

`code_` deserves separate mention, because it is the odd one out in a different way: **it has no adapter at all.** A Claude Code session runs locally and has no remote presence for an adapter to reach. There is nothing on the other end of that prefix to build a client for. Its partners exist in the database, its messages are stored for it to read, and no process ever spawns a thread to poll it — a point that becomes concrete in the note on drain threads.

## Two chains of command

Partners are not peers. There is a hierarchy, and it too is data — rows in `agent_layers` mapping a source and a role to a layer number, where lower is higher up.

Claude Code and NotebookLM sit at layer 0. A `bridge-scientist` sits at 1. A `project-orchestrator` at 2, a `gemini-orchestrator` at 3, and a plain Antigravity partner at 4. A `science_` partner with no role defaults to 2 — the same layer as the project-orchestrator, which is a detail that matters later.

Two management relationships run through this:

**Claude Code manages Claude Science**, through the `bridge-scientist` role. That pairing is the entire reason the role exists: it is the seam between a human's Claude Code session and the research project. A `code_` partner has exactly one legal counterpart, a bridge-scientist, and a bridge-scientist may hold exactly one `code_` partner. A bridge holding two would make "the Caller" ambiguous for every message that reached it.

**Claude Science manages Antigravity**, through the `gemini-orchestrator` role. Only that role may connect a Claude Science partner to an Antigravity conversation, and its reach is metered: a `project-orchestrator` grants it a budget, and each outgoing connection to an Antigravity partner spends one unit.

All three roles are Claude Science roles, enforced rather than merely intended — a role can only be claimed inside a `science_` project. The roles are named after what they orchestrate, not what holds them. A gemini-orchestrator is a Claude Science agent that directs Antigravity, never an Antigravity agent that directs itself.

That enforcement is recent enough to be worth a word on how it was reasoned about. An earlier arrangement restricted only the gemini-orchestrator, on the grounds that no other path could reach the others anyway. That is a statement about today's call graph, not a rule — and "currently unreachable" is exactly the kind of safety that stops being true when someone adds a caller.

## Where the remote boundary falls

A capability either needs a remote extension or it does not, and the dividing line is narrower than it first appears.

The tempting rule is "anything involving a remote application needs an extension." That rule is wrong, and `handshake` is the counterexample that shows why. A handshake can connect partners in two entirely different remote applications — Claude Science to Antigravity, and Claude Code to a bridge-scientist, are the two sanctioned cross-source pairings — and yet `handshake` never touches an extension at all. Every fact it needs (the two partners' types, roles, project memberships, existing rows, budget) already lives in the local schema.

The real boundary is this: **an extension is needed exactly where the data or the effect lives on the remote side.**

Creating a project needs one, because "does this project exist over there" is a fact only the remote can answer. Sending needs one, because advancing the queue means literally handing a prompt to a remote counterpart. The permission capabilities need one, because a read or write grant is held by the remote — the local table records what the grant is *meant* to be, and only the remote knows what it actually is.

The interface itself is small. Four methods every adapter must implement: verify a project exists, verify a partner exists within it, deliver a message, and stop whatever is running. Beyond those, a handful of methods with **refusing defaults** — and the refusals are the interesting part, because they are how the system stays honest about what a given remote cannot do.

The permission methods default to refusing with a rule-shaped error: only a remote that executes against a filesystem has paths to grant. Granting one to a notebook would record an intention nothing will ever apply, which reads exactly like a grant that is being enforced.

The completion and result-reading methods default to refusing with a *different* shape of error — not "this is forbidden" but "this capability was never taught to me." The distinction is deliberate and runs through the whole error vocabulary: a rule saying no, a capability that does not exist, and a remote that exists and was supposed to work and did not, are three different things, and collapsing them loses the information a caller needs to act.

There is deliberately no fifth abstract method for resuming. Correcting whatever blocked a partner and sending the work again *is* the resumption. A remote-side resume would be a second way to start work — one that skips the queue, and the queue is where priority is decided.

## One extension per process, and what that costs

A single `MessagingCore` holds **at most one** extension. Not a registry keyed by source: one.

That follows from the boundary above. An extension speaks for one family of remote, verified against one source prefix. A core holding several would need some rule for choosing which applies to a given partner — and the schema's one-source-per-project design already makes that unnecessary.

The strictness is real: a core configured for the *wrong* source is treated exactly like a core with no extension at all. Both refuse. Calling the wrong remote's extension would produce an answer from the wrong system, and an answer from the wrong system is worse than no answer, because nothing downstream can tell.

And this is where the three processes stop being an implementation detail and become something you have to reason about. A `science_` process holds only the `science_` extension. When a Claude Science agent sends a message to an Antigravity partner, the row is committed — a local write to a table both processes can see. But the delivery cannot happen in that process, which has no way to reach an Antigravity conversation, so the call does not come back as an ordinary receipt. It comes back saying a remote capability is missing, and carrying the fact that admission nevertheless stands.

That distinction matters to the caller: the message is queued, and re-sending it would double-send. So the message sits there, admitted and undelivered, waiting for the process that *can* reach it to notice.

How it notices — reliably, without anyone telling it — is the subject of note three. But the shape of the answer is already visible from here, and it is the same shape as everything else in this architecture: nobody is told. The fact is written somewhere both can see, and the one that can act on it looks.

---

*Next: what a queue is for when there is only one per partner, and why the single most important piece of state in the system is deliberately not in the database at all.*
