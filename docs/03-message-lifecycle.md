# The life of a message

**Audience.** An engineer who will maintain and debug this system without its author
present, and who needs to predict where a given message goes and why.

**Scope.** What the six behavior labels mean; how one priority queue per Partner replaced
two directional ones; what the working slot is and why it is not in the database; how a task
is displaced and resumed; which messages are stored and which are not; the two-phase
`[RESEARCH]` round trip; and the one rule that stops an exchange from running forever.

**Non-scope.** Parameter-level detail for each capability, which is
`docs/02-reference.md`. How to run or troubleshoot a deployment, which is
`docs/04-operating-and-debugging.md`. The reasoning behind the overall architecture, which
is `docs/01-architecture-and-rationale.md`.

**Assumed prior knowledge.** Python, SQLite, and roughly what MCP is.

## The six labels

Every message carries one behavior label. They describe what a message is *about*.

| Label | What it is |
|---|---|
| `[IDLE]` | stop and wait — the vehicle for a forced interruption |
| `[TRUTHFUL-REPORT]` | a summary of work that has finished |
| `[QUERY]` | a request for context the sender does not have |
| `[ERROR]` | something went wrong and the sender is stopped |
| `[MESSAGE-RESPONSE]` | the answer to a question that was actually asked |
| `[RESEARCH]` | delegated work that will take a while |

A label does not tell you who is speaking. Any party may send any label in either direction.
A Caller sends `[ERROR]` to tell a Partner what it got wrong; a Partner sends `[ERROR]` to
report it is stuck. Same label, opposite directions, both legitimate.

What a label *does* decide is how urgently the message is taken up relative to whatever its
recipient is already doing. That is the whole of its mechanical meaning, and it is the one
thing to hold onto before reading further.

## One queue, ordered by priority

There is **one queue per Partner** — `message_queue` — and every message is a push into it.
There is no reply channel, no second table, and so no routing decision to get wrong.

This replaced an earlier design with two queues split by *causal role*: a message that
opened a chain of work went to a capped forward queue, one that continued an admitted chain
went to an uncapped backward one. That model is gone. It was not wrong about the problem it
was solving — capping a continuation really can deadlock a pair, and the old design's
uncapped backward queue really did prevent it — but it solved it by asking the caller to
classify its own message, and a caller that classifies wrongly gets a deadlock back. The
priority queue solves the same problem structurally: an answer outranks the work that is
waiting for it, so it cannot be stuck behind it.

Priority lives in `label_caps.priority`, and lower wins:

| Priority | Label | Why here |
|---|---|---|
| 0 | `[IDLE]` | An interruption must always win, or it is not an interruption |
| 1 | `[TRUTHFUL-REPORT]` | A summary must be written without other traffic contaminating the context it summarizes |
| 2 | `[QUERY]`, `[ERROR]` | Both stop work; neither is more urgent than the other |
| 3 | `[MESSAGE-RESPONSE]` | The answer that unblocks — outranks the work that is blocked |
| 4 | `[RESEARCH]` | Delegated work, the patient kind |

The table is data, not code. A deployment that wants `[ERROR]` to outrank `[QUERY]` changes
a row.

## The cap counts work in flight, not work waiting

`label_caps.max_outstanding` limits how many tasks of one label one Caller may have
outstanding against one Partner: three for `[QUERY]`, two for `[RESEARCH]`, and NULL —
uncapped — for the other four.

Two things about the key are worth being precise about, because both were wrong in an
earlier version.

**It is keyed `(partner_id, caller_id, behavior)`, not per Partner.** A cap of "one message
per Partner" limits how much a Partner can be *asked*, which is the wrong quantity; what is
worth limiting is how much of one kind of work one Caller may have outstanding against it.
Two different Callers do not consume each other's allowance.

**It counts the working slot too.** A caller allowed three `[QUERY]` tasks has three in
flight, not three waiting plus one running. Without that term the fourth arrives the moment
the third starts, and the cap means one more than it says.

Admission is a single statement (`_ADMIT_SQL` in `messaging_core/core.py`) so that being
over cap shows up as `rowcount == 0`:

```sql
INSERT INTO message_queue (partner_id, caller_id, behavior, body, message_id)
SELECT :pid, :cid, :behavior, :body, :mid
 WHERE (SELECT max_outstanding FROM label_caps WHERE behavior = :behavior) IS NULL
    OR (SELECT COUNT(*) FROM message_queue
         WHERE partner_id = :pid AND caller_id = :cid AND behavior = :behavior) + :working
     < (SELECT max_outstanding FROM label_caps WHERE behavior = :behavior)
```

One statement, not a read followed by a write. Two concurrent callers can both pass a
check-then-insert and both insert, which is precisely how a cap of three admits four.
`:working` is the working slot's contribution, which lives in process memory and so cannot
be counted by SQL alone — `MessagingCore._admit` supplies it under the Partner's slot lock.

## The working slot

A queue row is a task **waiting**. The task a Partner is actually **being worked on** sits in
an in-memory working slot (`messaging_core/slots.py`), one per Partner, and never in SQLite.

That is a decision rather than an omission. A working slot is process state: it changes on
every swap, it is meaningless to a second process, and it does not survive a restart because
the remote's own turn does not survive one either. A row for it would be something a reader
would reasonably take for durable truth, and that reader would be wrong in exactly the
expensive case — after a crash, when the row says a Partner is mid-task and no thread is
driving it.

When a task is promoted into the slot, its `message_queue` row is **deleted**. The queue
holds what is waiting; the moment work starts it is no longer waiting.

## Advancing: the one place the swap happens

`MessagingCore.advance(partner_id)` is the single implementation of "compare the head
against the working slot and act". `send`, `interrupt_partner`, and the Polling Server's
drain thread all call it. None of them reimplements it — an earlier version had two copies
of this logic, in the core and in the polling server, and keeping two copies in step is a
bug waiting for the next person.

Under the Partner's slot lock, in order:

1. Read the queue head. That is two questions, not one — see below.
2. If the slot is empty, promote the head into it.
3. If the head's priority **strictly** beats the working task's, displace: stop the remote,
   mark the working task `in_process = 1`, push it back into the queue, and promote the head.
4. Otherwise nothing moves.
5. Render the promoted task into a prompt and hand it to the remote.

Two details in there carry real weight.

**Strictly, not "or equal".** An arriving `[QUERY]` must not displace a `[QUERY]` that is
already being answered. If it did, two Callers could ping-pong a Partner between their
questions and neither would ever get an answer.

**The remote is stopped BEFORE the queue is touched.** A stop that fails then leaves
everything exactly as it was, and the next `advance` simply tries again. Doing it after the
swap would leave the displaced task in the queue *and* in the slot — one task in two places,
with no way for a later read to tell which is real.

## Reading the head is two questions

Which **label** runs next, and then which **row** of that label. It takes two statements
because one `ORDER BY` cannot express the rule, and the rule is worth the second statement.

**Which label:** lowest `priority`; then a label with any unpaused work beats one whose rows
are all paused; then earliest arrival.

**Which row of it:** paused first, then earliest arrival.

## Paused tasks, and the one-line prompt

`in_process = 1` marks a task that was displaced from the working slot. It is paused, not
new.

It outranks other queued tasks carrying **the same label**, and only those. A paused
`[RESEARCH]` resumes before a fresh `[RESEARCH]`, so a Partner finishes what it started
before starting something else of that kind; but it still waits behind a `[QUERY]`, so an
interrupted Partner answers the question that interrupted it rather than wandering back.

The scoping cost a real bug to get right, and the bug is worth stating because the shape
recurs. `[QUERY]` and `[ERROR]` share priority 2 deliberately. While the tie-break was global,
a paused `[QUERY]` beat a fresh `[ERROR]`: a Partner interrupted mid-question was handed
"resume your previous `[QUERY]`" instead of the `[ERROR]` its Caller had just sent explaining
what went wrong. The correction was never delivered — and that is precisely the flow the
approval doctrine depends on, since an `[ERROR]` naming a missing permission is how a blocked
Partner gets unblocked.

A task returning to the slot is delivered a deliberately short prompt:

```
[Polling Server messages you]

Resume your previous [RESEARCH].
```

One line, and the argument for it is worth stating because the instinct is to send more.
Among the queued tasks carrying one label, at most one is marked `in_process`, and it is
always picked before the others of that label — so "your previous `[RESEARCH]`" resolves to
exactly one thing, which the agent is still holding in its own context. Restating the work
would hand back a worse copy of something it has not forgotten, and would invite it to start
over instead of continue.

The row's `body` stays the **original request** throughout, precisely so this holds.

## A Partner that cannot finish on its own

A Partner working a `[RESEARCH]` sometimes hits something only the agent that sent it can
resolve — a path it was not granted, a question about what was actually meant. It raises a
`[QUERY]` or an `[ERROR]` **upward**, along the reverse handshake, and then stops.

The stopping is the part worth explaining. Without it the next queued message reaches an
agent that is blocked on an unanswered question, and the two interleave in one context with
nothing marking where either begins. So `send` parks the sender: an `[IDLE]` takes its
working slot, and the unfinished work sits paused in its own queue.

Three conditions, all required. The label is `[QUERY]` or `[ERROR]`; the sender holds a
working slot; and the message travels back **along** a handshake rather than out along one.
That last condition is what makes it precise. A Caller dispatching a routine `[QUERY]` down
to a worker holds a slot too, and an orchestrator that stopped itself every time it asked a
worker anything would halt everything it was driving.

On the receiving side nothing new is needed. The `[QUERY]` or `[ERROR]` arrives at priority
2 and goes to the front of everything below it, displacing a running task unless that task ties or outranks it — that displacement *is* the interruption. The
Caller resolves it and answers; the `[MESSAGE-RESPONSE]` lands in the Partner's queue and
displaces the hold, because anything displaces a hold. The paused work resumes behind it.
Nothing has to remember to release anything.

## Interruption is a normal push

`interrupt_partner` does not have a mechanism of its own. It pushes a dummy `[IDLE]` into
the target's queue and calls `advance`. Because `[IDLE]` holds priority 0, it takes the
working slot by construction.

That is the point: interruption and ordinary delivery are the same code path, so there is no
second implementation to keep in step with the first, and no way for the two to disagree
about what happens to the displaced task.

An `[IDLE]` sitting in the working slot is a **hold**, not a task, and it is handled
differently in exactly two places:

- **Anything displaces it**, regardless of priority. Comparing priorities would make the
  interruption permanent, since nothing outranks `[IDLE]`. The Partner is stopped and
  waiting, and the next thing to arrive is what it was waiting for.
- **It is discarded rather than requeued** when displaced. It has already done its whole
  job, which was to stop the Partner. Requeuing it would stop the Partner again the moment
  it resumed.

The drain thread also never polls an `[IDLE]` for completion and never reports anything back
for it.

`send` refuses `[IDLE]` outright (`idle_not_sendable`). Accepting it would be a second route
to interrupting a Partner — one that skips the same-project and can-execute checks and never
stops the remote, leaving it working on something it has just been told to abandon.

## What is stored, and what is only transported

`label_caps.stored` decides. Three labels are written to `messages` and are readable later
through `read`: `[QUERY]`, `[TRUTHFUL-REPORT]`, `[MESSAGE-RESPONSE]`. The other three —
`[RESEARCH]`, `[ERROR]`, `[IDLE]` — are transport: they travel in a queue, are acted on, and
are never written down.

The rule is enforced by a trigger (`messages_stored_labels_only`) that reads `label_caps`,
rather than by a `CHECK` listing the labels. A list in the `CHECK` would be a second copy of
`label_caps.stored`, and two copies of one fact eventually disagree. With the trigger,
flipping `stored` on a row changes what `messages` accepts — which is what "the table is the
authority" has to mean to be worth saying.

Why these three and not the others: `read` exists so an agent can recover context it has
lost. A stored `[RESEARCH]` would replay delegated work into that context on every call; a
stored `[ERROR]` would replay failures that have already been resolved. Neither is context;
both are noise that grows without bound.

## The `[RESEARCH]` round trip

A `[RESEARCH]` task is not finished when the work stops. It owes a summary, and the summary
is a **second exchange against the same remote in the same working slot** — not a new
message in the queue.

```
send([RESEARCH])  ->  admitted, promoted, delivered as a research_dispatch prompt
                          (request + references + the Partner's configured paths)
                  ->  remote works; drain thread polls
                  ->  begin_summary_phase(): SAME slot, behavior becomes
                      [TRUTHFUL-REPORT], effective priority raised 4 -> 1,
                      body stays the ORIGINAL request
                  ->  delivered as a truthful_report_request prompt
                  ->  remote summarizes; drain thread polls
                  ->  read_remote_result()
                  ->  report_back(): [TRUTHFUL-REPORT] pushed into the CALLER's queue
```

Two decisions in that sequence are load-bearing.

**The summary request is not a queued message.** If it were, `[TRUTHFUL-REPORT]` would mean
"summarize this" travelling one way and "here is the summary" travelling the other, and a
queue row carries nothing that says which. Keeping the request out of the queue leaves the
label with one meaning everywhere it can be seen: a `[TRUTHFUL-REPORT]` in a queue is always
a summary being delivered.

**The priority is raised for the second phase, and that is what blocks the queue.** Once the
slot reads `[TRUTHFUL-REPORT]` at priority 1, every arriving message **queues instead of
reaching the agent** — `[QUERY]` and `[ERROR]` at 2, `[MESSAGE-RESPONSE]` at 3, `[RESEARCH]`
at 4 — because `advance` displaces only on a strictly lower number, and only `[IDLE]` has one.

The blocking is the point, and it is worth stating as the counterfactual: a summary written
while other traffic *was* reaching the same context would summarize the traffic.

### Aiming the summary at the right section

This is the hardest prompt in the system and its failure mode is quiet. A long work session
holds far more than the work: false starts, tooling detours, the task the agent was displaced
from and later resumed, and intermediate results that later ones replaced. An agent asked to
"summarize your work" summarizes all of it, weighted by recency, and its most confident
paragraphs end up being about whatever it touched last.

`templates.truthful_report_request` aims it with two devices. It **quotes the original
request back verbatim**, because "that request" has to resolve to something and by
construction the agent has been holding more than one. And it **excludes resumed-from work
explicitly**, because otherwise a displaced-then-resumed task is reported twice, once in each
report, with the two copies disagreeing.

## What comes back, and what stops the exchange

When a working task finishes, what goes back to the Caller is `label_caps.reply_behavior`:

| Finished task | Reply pushed to the Caller |
|---|---|
| `[QUERY]` | `[MESSAGE-RESPONSE]` |
| `[RESEARCH]` | `[TRUTHFUL-REPORT]` (after the summary phase) |
| `[ERROR]` | nothing |
| `[MESSAGE-RESPONSE]` | nothing |
| `[TRUTHFUL-REPORT]` | nothing |
| `[IDLE]` | nothing |

**The NULL is the important value in that column.** Two labels ask for something; the other
four *are* answers, or are not messages at all. Without a label whose reply is nothing, every
completed task would produce a message that produced a task that produced a message, and two
agents would talk to each other until one of them was archived. A `CHECK` on `label_caps`
additionally forbids a label replying with itself, which is the same infinite exchange
written more compactly.

After `report_back`, the Polling Server ensures the Caller has a drain thread of its own —
otherwise the answer would sit in the Caller's queue until it happened to be pushed to for
some other reason.

## Delegated work only travels downward

`[RESEARCH]` is the one label with a direction rule, and it is not about queues. A Partner
may only delegate to another at the same or a lower position in the hierarchy:

```
NotebookLM, Claude code  >  project-orchestrator  >  gemini-orchestrator  >  Antigravity
        0          0                2                       3                    4
```

Layers come from `agent_layers` (a `bridge-scientist` sits at 1, between Claude code and the
project-orchestrator). A lower agent handing `[RESEARCH]` upward would be reassigning its own
director's work, so `send` refuses with `research_cannot_flow_upward`.

Sideways is deliberately allowed: two Partners at the same layer in Projects linked by
`project_extension` are branches of one research effort, not a chain of command.

Every other label travels freely in both directions. That is what lets an answer, an error,
or a report come back at all.

## The answer direction

A handshake row is directional, and the direction is not arbitrary: `handshake` refuses
`requester_not_orchestrator`, so `from_partner` is always the orchestrator and `to_partner`
always the worker it directs. A worker therefore has no row of its own pointing back, and no
way to create one.

So `send` accepts the **reverse** row as well. Nothing new becomes reachable — it is the same
pair, already joined by the same orchestrator — and what it adds is the only thing a Partner
could not otherwise do: say something. Without it, the `[RESEARCH]` dispatch's own instruction
to "message back a `[QUERY]` and idle" named an action the system refused.

`[RESEARCH]` is the exception, and needs to be. The layer rule does not cover this case: a
project-orchestrator and the plain `science_` worker it directs sit at the *same* layer, so
`research_cannot_flow_upward` — which refuses only a strictly higher target — would not fire.
The forward-row requirement was what prevented a worker delegating back to its own director,
and accepting the reverse row for every label would have given that away as a side effect. So
delegation still requires the handshake to point at its target: `research_needs_a_forward_handshake`.

Answers travel back along a handshake. Delegated work only travels along it in the direction
the orchestrator claimed.

## Telling an agent who it is

`send`'s first argument is `requester_uuid` — the caller's own uuid — and a Partner running
inside a remote has never been told what its own is. `templates.identity_block` puts it in the
prompt, alongside the Caller's title and the call already filled in, and is appended to the
`[RESEARCH]` dispatch and to every relay.

It also says the opposite thing, which matters just as much. Whatever the agent produces is
read back off the session by the Polling Server and delivered when the turn ends, so an agent
handed only its identity would reasonably use it to send its answer — and the Caller would
receive the same work twice, once harvested and once sent. The block states that answering is
automatic, and that `send` is for the case where the turn is *not* finishing: a `[QUERY]` for
context only the Caller holds, an `[ERROR]` when blocked.

## When delivery fails

If the remote raises while a promoted task is being delivered, the task is put **back** into
the queue, marked `in_process`, and the exception propagates. From the queue's point of view
it is a task that started and stopped, which is exactly what happened. Silently dropping it
is the one outcome nothing downstream could detect.

If the Partner has been archived, queued work is discarded rather than delivered — and
discarded rather than left in place, since leaving it would mean every later `advance` hit
the same dead end forever.

---

For the parameter-level contract of any capability named here, see `docs/02-reference.md`.
For the invariants this lifecycle rests on and where each is enforced, see
`docs/05-invariants-and-constraints.md`.
