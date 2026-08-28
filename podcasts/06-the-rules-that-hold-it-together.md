# Six: The rules that hold it together

## Where a rule lives is part of the rule

The previous five notes described mechanisms. This one is about the rules those mechanisms enforce — and, more importantly, about *where* each one is enforced, because that turns out to be as much a part of the design as the rule itself.

A rule can live in three places here.

**A schema constraint** — a `CHECK`, a `UNIQUE`, a foreign key. The database refuses, always, regardless of which code path issued the statement.

**A trigger** — used where a constraint cannot reach, because it needs to count rows or consult another table.

**Application code** — a check inside a capability, which holds only for callers that go through that capability.

The system is consistently explicit about which of the three applies to any given rule, and that honesty is worth more than it sounds. A rule enforced in application code and *described* as though the database guaranteed it is how someone later adds a second code path and quietly bypasses it. "Currently unreachable" describes today's call graph, not a rule.

## The rules the database itself refuses to break

Some rules are worth the trip to the schema.

**Titles are unique server-wide, and archived titles stay spent.** A plain `UNIQUE` handles the first. The second needs a trigger, because it is about a state transition: renaming an archived partner is refused outright. The reason is that renaming would *free* the title, and the next partner to take it would silently inherit an address other agents may still be holding — pointing at something that is not what they think it is. The refusal belongs in the database and not only in the tool, because a rule that lives solely in application code is bypassed by anything that issues the `UPDATE` directly.

**A partner's remote id is unique within its project, not globally.** One remote object maps to one partner — but a remote id string is only meaningful inside the application that issued it. Two different projects, even two different remotes, can coincidentally share an id string without naming the same object. A global uniqueness constraint would reject that as a collision when it is none.

**A project holds a limited number of live partners.** The number is data, in `source_caps`. The enforcement is a trigger, because a `CHECK` cannot count rows — but it is still the database refusing, rather than whichever caller remembers to check.

**A role is claimed once.** A partial unique index over project and role, scoped to live partners holding one. Two callers racing to claim the same role both issue the update; the index decides, and one of them is told the role is taken. The scoping to live partners is why archiving a holder frees the role.

**Only labels marked as stored reach durable history.** A trigger, deliberately not a `CHECK` listing the labels — a list would be a second copy of `label_caps.stored`, and two copies of one fact eventually disagree.

**Path permissions belong only to sources that execute against a filesystem.** A trigger, mirrored by an application-level refusal that says the same thing readably. Granting a path to a notebook would record an intention nothing will ever apply, which reads exactly like a grant that is being enforced.

**A budget is granted by the right role to the right role**, both ends enforced by a trigger — because a `CHECK` cannot reach another table.

**A message cannot name its own recipient as its sender.** A `CHECK` that a queue row's caller and partner differ. Small, and it shapes real decisions: it is why a notice about a vanishing partner is attributed to the partner rather than to whoever removed it, since the remover is usually the same agent as the one being told.

## The rules that live in code, and are honest about it

Others cannot be database rules, and the system says so rather than implying more safety than exists.

**Priority decides the working slot, and only a strict win displaces.** Pure application logic, in one place. Strictly, not "or equal" — otherwise two callers at equal priority ping-pong a partner between them and neither ever gets an answer.

**A paused task outranks only within its own label.** Two statements rather than one ordering, because it is genuinely two comparisons over two groupings.

**A cap counts work in flight.** Half the count is in SQL and half is in memory, so no constraint could express it. It is one statement rather than a read-then-write, which is what makes it safe under concurrency.

**Delegated work never travels upward.** Read from a table rather than compared against literals, so a deployment that adds a tier adds a row. But the check itself is in code — and it is worth knowing that the layer rule alone is not sufficient. It refuses only a *strictly* higher target, and a project-orchestrator and the plain worker it directs sit at the same layer. What actually prevents a worker delegating back to its own director is the requirement that delegation travel along a handshake in the direction the orchestrator claimed.

## Authorization before existence

One rule is about what a refusal *reveals*, and it is the most carefully constructed check in the system.

Granting budget takes another partner's uuid — the only capability that does. A uuid is an identity credential that must never leak, so the shape of a refusal must not become an oracle for whether one exists.

The check therefore runs in a specific order: authorization is tested **before anything that depends on the named partner existing**. A requester that does not hold the required role is refused without a single query ever touching the uuid it supplied.

And past that gate, two different situations are given the **same** refusal: a uuid naming a partner in a different project, and a uuid naming nothing at all. Deliberately not distinguished, for the same information-flow reason. Only once the requester has proved entitled to ask does the system tell it something specific.

## Relationships, and what an extension is for

Handshakes carry most of the structural rules, and they are worth walking as a shape rather than a list.

A notebook needs none — there is no agent to direct.

Claude Code has exactly one legal counterpart, a bridge-scientist, and that bridge may hold exactly one Claude Code partner. A bridge holding two would make "the caller" ambiguous for every message reaching it.

Within Claude Science there are two legal initiators, and the second is narrow. Ordinarily the project-orchestrator pairs two partners. But a bridge-scientist may also initiate one — toward the project-orchestrator specifically, and nowhere else. That is the bridge doing the only thing it exists to do: wiring the seam it holds into the chain of command above it.

Only the gemini-orchestrator may reach an Antigravity conversation, and its reach is metered by a budget counted live — one unit per outgoing connection to an Antigravity partner. A conversation serves exactly one Claude Science master, so "who directs this" has one answer.

**And a project extension branches sideways, never downward.** A project holds a limited number of live partners, and that ceiling is deliberate: research at scale needs more projects, not a larger ceiling. An extension declares two projects parts of one effort.

Across it, a same-source pair must hold the **same** role. The point of an extension is a research effort branching sideways, not a second chain of command — requiring identical roles is what stops an orchestrator in one project inheriting a superior it was never given from another.

The link is stored once, with the lower project id first, so "is A an extension of B" has exactly one row and cannot answer differently depending on which way it is asked.

## One conversation continuing another

Antigravity conversations are the exception to the same-role rule, and following why shows how the pieces constrain each other.

An effort can outlast a single conversation. When it does, a new conversation picks up where the last left off — and to do that it needs to reach its predecessor.

But every orchestrator role is a Claude Science role. An Antigravity partner holds none and never will. So the general requirement that a handshake be initiated by an orchestrator could never be satisfied, and the cross-project rule past it would demand a matching role that neither side holds.

So a pair of Antigravity conversations is decided **before** the orchestrator gate — exactly as a Claude Code pair already is, and for the same reason: a participant that holds no role cannot satisfy a rule about roles.

The rules for that pair are narrow. Inside one project, refused — two conversations under the same orchestrator are peers, with nothing for one to inherit from the other. Across two projects with no extension, refused. Across a linked pair, legal, with no role required of either side.

And a predecessor may be inherited from **once**. A lineage is a line, not a fork, so that "which conversation continues this one" has exactly one answer — the same reasoning that gives a bridge exactly one Claude Code partner.

Inheriting carries nothing across. No permissions move; no queued work moves. It is a handshake and only that.

One further consequence had to be handled for this to work at all: the rule limiting a conversation to one Claude Science master counts inbound relationships. Counting *every* inbound relationship would make an inherited conversation permanently unreachable by the orchestrator that pays budget for it — the successor's link would read as a second master. It counts Claude Science sources specifically, which is what it always said it did.

## Losing a partner, and who is owed a word

Two ways a partner goes away, and they behave differently for a reason that is entirely about what can be *reported*.

**Archiving** leaves the row in place and marks it archived. Afterwards the queue is dropped — correctly, since an archived partner can never be messaged again. Every caller waiting on that work would simply never hear back.

So archiving reports first. Every caller with something queued for that partner, plus the caller of whatever sits in its working slot, receives an `[ERROR]` saying the partner was archived and the work is gone rather than delayed. At priority 2 that goes to the front of everything below it — displacing what the caller is doing unless that ties or outranks it — which is the interruption.

The notice is attributed to the vanishing partner. The schema would permit any sender other than the recipient, so this is a choice between the two candidates actually available — and only one of them works. The vanishing partner's row survives archiving, so it remains a valid, permanent sender. Attributing it to whoever performed the archive would usually name the notice's own recipient as its sender, which the constraint refuses outright. Note also that the caller told is normally the same agent that called archive, and that is the point rather than a redundancy: within a project only the orchestrator may direct a plain worker, so it is the only caller there is — and an orchestrator archiving a list of titles in bulk is exactly who does not realise one of them had work in flight.

**Deletion** cannot report itself at all, and so it refuses.

The reason is structural. Queue rows cascade on the deletion of the partner that sent them — so a notice written to warn a waiting caller is destroyed by the very deletion it warns about. Attributing it to the requester instead collides with the constraint above in the normal case where the requester *is* the caller waiting.

There is no shape of message that survives. So deletion refuses when work is in flight, and names the route that does report. The same applies to deleting a whole project, where the cascade is wider.

## What the system can tell you about itself

When something has gone wrong, there are two places to look, and neither of them is the database.

**Logging.** An admission and a delivery are logged as they happen; a swallowed exception is logged as a warning; a declined drain thread is logged at debug level, because in a three-process deployment that is a normal outcome rather than a problem.

One rule about logging worth stating: **a rule-based refusal is never logged as an error.** A refusal is a rule working correctly. Logging it as an error fills an operator's log with correct behaviour and trains them to ignore the log — which is how the one real error gets scrolled past.

Nothing here configures the root logger. A library that does that steals the decision from whoever embeds it, so a bare run shows nothing until someone asks.

**The diagnostic report.** Three records the system keeps that nothing else surfaces: what a drain loop swallowed, which displacements went through against a remote that refused to be cancelled, and whatever an adapter collected while closing something.

All three matter for the same reason: **swallowing an exception to keep a daemon thread alive is right, and swallowing it without a trace is how a permanently failing thread becomes invisible.** From outside, a thread retrying the same failure forever produces no result, no progress, and no complaint — the same nothing a healthy idle system produces. These records are what tell the two apart, and the warning log is what makes the failure audible at all.

The report is built field by field so that a broken one degrades to empty rather than taking the others down, because it is reached for when something is already wrong.

**And a partner's own status** computes how long its current task waited before it started. That number can exist at all only because the enqueue time is carried onto the working slot when the queue row is deleted — a promoted row is gone, so nothing else could reconstruct it afterwards. The two timestamps are written in the same format deliberately, so that subtracting them is arithmetic rather than a parsing problem discovered at the worst moment.

Worth knowing precisely: the capability returns it, and the rendered status an agent reads does not yet show it. The measurement is available to anything calling the library; an agent asking about itself currently sees what it is working on and when that started, not how long it waited first.

Status is first-person only. An agent asks about itself. It learns its own queue broken down by label — because a depth of four says nothing useful while "three delegated tasks and one question" says what the partner is about to do next and why — its own working task, its own relationships by title, and its own budget. It learns nothing about anyone else's queue, role, or identity.

## Reading the shape whole

Step back, and the same instinct shows up in every one of these rules.

**One authority per fact.** Priority, caps, storage and replies live in one table. Storage is enforced by a trigger reading that table rather than a list that would be a second copy of it. The swap has one implementation, called by everything that swaps.

**State that cannot lie.** The working slot is in memory precisely because persisting it would produce a row that reads as durable truth and is wrong exactly after a crash. A column with no writer is worse than no column, because it reads like a measurement someone is taking.

**Refusals that carry information.** A rule saying no, a capability that does not exist, and a remote that failed are three different shapes, and they stay distinct because a caller acts differently on each. A refusal that reveals whether a uuid exists is not a refusal at all.

**And an honest account of what is not guaranteed.** A remote with no cancel is displaced anyway, with the consequence recorded rather than hidden. A screen-scraped result degrades to nothing rather than to a fabricated answer. A permission prompt is reported rather than answered, because answering it would dissolve the boundary it exists to mark.

The system is not large. What it is, is consistent about where its truth lives — and consistent, too, about saying so when the truth is only as good as the code that remembers to check it.

---

*That is the whole system: a shared database that is the integration layer, one queue per partner with a table deciding what its labels mean, a slot in memory that cannot outlive the turn it describes, a thread per partner that watches so no agent has to, three adapters holding the line against three very different remotes, and a set of rules that are honest about where each of them lives.*
