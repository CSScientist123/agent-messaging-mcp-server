# Two: One queue, and the slot that is not in it

## Five labels, and one table that means all of them

Every message carries a label. There are five, and they are not a taxonomy for its own sake — each says something about how urgently the message should be taken up and what, if anything, comes back.

- **`[RESEARCH]`** delegates work. "Go and do this."
- **`[QUERY]`** asks a question about something the recipient already holds.
- **`[ERROR]`** says something went wrong and names it.
- **`[MESSAGE-RESPONSE]`** is an answer to a question.
- **`[TRUTHFUL-REPORT]`** is a summary of work done.

There is deliberately no sixth label meaning *stop*. Two of these five already stop somebody, and that turns out to be enough — it gets its own treatment later.

Here is the part that matters more than the list. Everything a label *implies* lives in one table, `label_caps`, with one row per label and four columns:

| label | priority | cap | stored | replies with |
|---|---|---|---|---|
| `[TRUTHFUL-REPORT]` | 1 | — | yes | nothing |
| `[QUERY]` | 2 | 3 | yes | `[MESSAGE-RESPONSE]` |
| `[ERROR]` | 2 | — | no | `[MESSAGE-RESPONSE]` |
| `[MESSAGE-RESPONSE]` | 3 | — | yes | nothing |
| `[RESEARCH]` | 4 | 2 | no | `[TRUTHFUL-REPORT]` |

Read that table as four separate rules that happen to share a home.

**Priority** decides what runs next. Lower wins.

**Cap** limits how many of that label one caller may have outstanding against one partner. Two of the five are capped — the two that ask a partner to go and do something. The other three carry no limit.

**Stored** decides whether the message is written to durable history. Three labels are; three are not.

**Replies with** decides what the system sends back when a task carrying that label finishes — and, crucially, whether the exchange ends. Three labels reply with nothing.

The reason to put all four in one table rather than four branches in code is not tidiness. It is that a rule expressed twice eventually disagrees with itself. If storage were a list of labels in a trigger *and* a set of conditionals in the application, the day would come when a label was added to one and not the other, and the resulting behaviour would be neither what the trigger said nor what the code said. There is one place, and everything reads it.

## Why an exchange ends

Look again at the "replies with" column, because it encodes something the system genuinely could not survive without.

When a task finishes, the system looks up its label's `reply_behavior`. If there is one, it pushes a message of that label back to whoever sent the original. If the value is NULL, it pushes nothing.

Now imagine every label had a reply. A `[RESEARCH]` completes and sends a `[TRUTHFUL-REPORT]`. The report completes and sends something. That completes and sends something. Two agents would talk to each other, forever, until one of them was archived — each message perfectly correct, the pair as a whole useless.

**The NULL is what stops it.** `[MESSAGE-RESPONSE]` replies with nothing, so a question is asked, answered, and done. `[TRUTHFUL-REPORT]` replies with nothing, so work is delegated, summarised, and done.

There is a database constraint forbidding a label from replying with itself, which is the same infinite exchange written more compactly and therefore easier to introduce by accident.

Notice how `[ERROR]` sits in this. It is answered — with a `[MESSAGE-RESPONSE]` — because a caller that corrects a blocked partner otherwise has no way to know the correction landed. It sends a fix into the dark and never learns whether anything changed. But the answer to an `[ERROR]` is a label that itself replies with nothing, so the correction terminates one hop later. Answered, and finite.

## One queue per partner, and every message is a push

There is exactly one `message_queue` per partner. Not one per direction, not one per relationship, not separate inbound and outbound queues: one.

That single decision removes a whole category of question. There is no direction parameter on `send`. There is no queue to choose. Every message is a push into the recipient's queue, and the label — not a routing decision made by the sender — determines how urgently it is taken up relative to whatever that partner is already doing.

Consider what the alternative looks like. If messages were routed by the *role* of the sender — work flowing one way, answers flowing another — then every message needs a direction, every direction needs a rule, and the rules have to agree with the hierarchy, the handshake table, and each other. And when they disagree, a message goes into a queue nobody is draining.

With one queue, "where does this message go" has exactly one answer: into the recipient's queue. What happens *next* is a separate question with its own separate answer, and that separation is what makes both tractable.

## Reading the head is two questions, not one

Given a queue with several messages in it, which one runs next?

The naive answer is an `ORDER BY`: sort by priority, then by whether the row is paused, then by arrival, and take the first. That is wrong, and the way it is wrong is instructive enough to be worth the detail.

Paused rows are the complication. When a task is displaced from the working slot, it goes back into the queue marked `in_process = 1`. It has started and stopped, and it should be resumed before the system starts anything else *of its kind*.

Of its kind. That qualifier is the whole problem.

`[QUERY]` and `[ERROR]` share priority 2, deliberately — both stop work, neither is more urgent than the other. Now suppose a partner is interrupted mid-`[QUERY]`, so that `[QUERY]` sits paused in its queue. Its caller then sends the `[ERROR]` explaining what went wrong. Under a single ordering with "paused first" applied globally, the paused `[QUERY]` outranks the fresh `[ERROR]` — same priority, and paused sorts first. The partner is handed *"resume your previous `[QUERY]`"* and the correction is never delivered.

That is precisely backwards. The `[ERROR]` is the thing that unblocks it.

So the head is read as **two questions**.

The first asks which *label* runs next: lowest priority, then a label with any unpaused work over one whose rows are all paused, then its earliest fresh arrival, then arrival.

The second asks which *row* of that label: paused first, then arrival.

`in_process` appears only in the second question. It breaks ties *within* one label and never across two. That is the rule, and one `ORDER BY` cannot express it — not because SQL is limited, but because it is genuinely two different comparisons over two different groupings.

The third key in the first question deserves its own moment, because it closes a gap the first two leave open. "Has any unpaused work" separates a wholly paused label from one with something fresh, and nothing more. A label holding a paused row *and* a fresh one ties `[ERROR]` on that key — and then loses on arrival, because its paused row is, by construction, the oldest thing in the queue. The same bug walks back in through the tie-break. Ranking each label by its earliest *fresh* arrival is what closes it: a label is judged on when it last had something new to say, not on how long its paused row has been sitting.

And a consequence falls out of the second question that the system leans on heavily: among the rows carrying one label, **at most one is ever paused**, and it is always picked first. That is what lets the resume prompt be a single line. *"Resume your previous `[RESEARCH]`"* has exactly one referent, so the system does not need to restate the work — the agent is still holding it.

## The slot that is deliberately not in the database

Every other piece of queue state is in SQLite. The single most important one is not, and that is a decision rather than an oversight.

The **working slot** holds the one task a partner is actually being worked on. It lives in memory, in a plain dictionary keyed by partner id, inside the process that owns that partner's remote.

Why not persist it? Because it would be a lie in exactly the situation where being lied to is most expensive.

A working slot is process state. It changes on every swap. It is meaningless to a second process — only the one driving the remote can act on it. And it does not survive a restart, **because the remote's own turn does not survive one either.** Persisting it would put a row in the database that any reasonable reader would take for durable truth. That reader would then be wrong precisely after a crash: the row says a partner is mid-task, and there is no thread anywhere driving it.

An in-memory slot cannot tell that lie. If the process is gone, the slot is gone, and the queue — which *is* durable — is the only claim about what is outstanding. There is nothing to reconcile because there is nothing that could disagree.

This is a recurring shape in the codebase, and it is worth naming: **a column with no writer is worse than no column,** because it reads like a measurement someone is taking.

## Draining deletes

When a task is promoted from the queue into the working slot, its queue row is **deleted**.

The queue holds what is waiting. It does not hold what is running.

Without that, a busy partner would hold its own cap slot twice — once as a queue row and once in the working slot — and a cap of two would admit one. The deletion is what keeps the queue's meaning single.

It also means the row id a slot carries is a breadcrumb rather than a live key. The row is gone. The id says where the task came from, not where it is.

## The cap counts work in flight

A cap limits **work in flight**, not work waiting, and the distinction is the entire point.

A caller allowed three `[QUERY]` tasks against a partner has three *including the one the partner is answering right now*. Otherwise the fourth arrives the moment the third starts, and a cap of three quietly means four.

So the count has two parts: queued rows, plus the working slot's contribution. And the working slot is in memory while the rows are in SQL, so the check cannot be one query.

It is one *statement*, though, and that matters. Admission is an `INSERT ... SELECT ... WHERE`, with the cap condition inside the `WHERE` and the slot's contribution passed in as a bound parameter. Being over cap is `rowcount == 0` — the insert simply does not happen.

The alternative — read the count, decide, then write — can be passed by two concurrent callers simultaneously. Both read two, both conclude there is room, both insert. A cap of three admits four, and it does so only under concurrency, which is to say only in production and never in a test that runs one thing at a time.

The slot's contribution is read under that partner's lock, which is what stops a swap landing between counting the queue and asking about the slot.

## A label that changes mid-flight

There is one place where a task's label changes while it sits in the slot, and it complicates the cap in a way worth following.

A `[RESEARCH]` task is two exchanges against one remote: do the work, then report on it. When the work finishes, the task is *relabelled* `[TRUTHFUL-REPORT]` in place, without leaving the slot, and its priority is raised to that label's.

The priority raise is the point. `[TRUTHFUL-REPORT]` sits at 1, at the top of the table. From the moment the relabelling happens, **arriving messages queue rather than reach the agent** — `[QUERY]`, `[ERROR]`, `[MESSAGE-RESPONSE]` and `[RESEARCH]` all wait, because displacement requires a strictly lower priority number and nothing has one.

That blocking is the whole reason for the raise. A summary written while other traffic was reaching the same context would summarise the traffic.

But now consider the cap. If the cap counts the slot by its *current* label, the moment that task becomes a `[TRUTHFUL-REPORT]` it stops counting against `[RESEARCH]` — and the same caller can put one more `[RESEARCH]` in flight than the cap allows, for exactly as long as the summary takes. Which is exactly when the partner is least able to take more.

So the task carries the label it was **admitted under** alongside the one it is running as, and the cap reads that.

The two halves of the count express it slightly differently, and it is worth knowing which. The SQL side counts a row under its admitted label *instead of* its current one. The slot side counts a task if *either* label matches. So a summary phase sitting in the slot counts against both `[RESEARCH]` and `[TRUTHFUL-REPORT]`, while the same task displaced into the queue counts against `[RESEARCH]` only. The two never disagree in practice — but only because `[TRUTHFUL-REPORT]` is uncapped, so the extra count on the slot side can never refuse anything.

The same marker travels on the queue row if the summary is displaced, which matters for a reason covered in the next note: without it, a summary that gets interrupted comes back looking like an ordinary report and quietly owes nobody anything.

## One place where the swap happens

`advance` is the single implementation of "compare the head against the working slot and act." Sending calls it. The background thread that watches remotes calls it. Neither reimplements it.

That is not merely good hygiene. An earlier arrangement had two implementations — one in the core, one in the component that watches remotes — and two implementations of a swap rule are two chances to answer differently about which task is running.

Its logic, in order, under the partner's lock:

Read the head. If the slot is empty, the head takes it. If the head's priority **strictly beats** the working task's, the working task is marked paused and pushed back, and the head takes the slot. Otherwise nothing moves.

**Strictly**, not "or equal," and the word is load-bearing. If equal priority displaced, an arriving `[QUERY]` would push out a `[QUERY]` already being answered. Two callers asking questions could ping-pong a partner between them indefinitely and neither would ever get an answer, with every individual step following the rules.

Then the promoted task is rendered into a prompt and handed to the remote. A task returning from paused gets the one-line resume prompt instead of its original body — it never left the agent's head, and restating the work would hand back a worse copy of something it has not forgotten, while inviting it to start over.

Two details in the ordering carry weight.

When a task is displaced, the remote is stopped **before** the queue is touched. A stop that fails then leaves everything exactly as it was, and the next attempt simply tries again. Doing it after the swap would leave the displaced task in the queue *and* in the slot — one task, two places, and no later read able to tell which is real. It must also happen before delivery either way: an agent handed a second instruction while still executing the first interleaves them.

And if delivery fails after the row has already been removed, the task is put **back**, marked paused, and the error propagates. From the queue's point of view it is a task that started and stopped, which is exactly what paused means. Silently dropping it is the one outcome nothing downstream could detect.

## The other thing that happens to a slot

Almost everything in the slot got there by being promoted out of the queue. There is one thing that takes a slot *away*, and it happens to an agent because of something it did itself.

When an agent sends a **request** — a `[RESEARCH]`, an `[ERROR]` or a `[QUERY]` — that act interrupts **the agent that sent it**. Its remote is stopped, whatever it was working on is pushed back into its own queue marked paused, and its working slot is emptied. Its drain thread is stopped, and the row claiming that thread exists is deleted.

Notice who that happens to. Not the recipient — the recipient just finds a job in its queue and drains it in priority order like anything else. Interruption is never a decision one agent makes about another. There is no capability anywhere that stops somebody else. It is something you do to yourself by asking.

The reasoning is the same for all three labels. An agent that sends a request has handed work away and is waiting on the outcome. Continuing would mean producing work it will have to redo the moment the answer changes what it knows.

And nothing takes the emptied slot. **An interrupted agent holds nothing at all.** That is worth dwelling on, because the obvious alternative — parking a placeholder task there to mark the state — is exactly what the design refuses. A placeholder is a row a reader could mistake for work, and one that every count, every cap and every prompt would then have to exclude by hand.

So the state is the *absence* of a task. Which raises the question the design has to answer: how do you tell an interrupted agent from one that is simply between two tasks? Both have an empty slot.

The answer is a column: `partners.interrupted`. It is not redundant with the emptiness — it is what makes the emptiness legible. Between two ordinary tasks the drain thread is still running and should promote the next row. Interrupted, there is no thread and nothing should be promoted. The slot looks identical in both; the flag is the difference.

## What ends it

A **response** — and the split between a request and a response is not a new list to maintain. It is exactly `reply_behavior IS NULL`. A label that expects an answer is a request. A label that *is* an answer is a response. `[MESSAGE-RESPONSE]` and `[TRUTHFUL-REPORT]`, the two that terminate exchanges, are the two that restart agents.

The response takes the empty slot, clears the flag, and a drain thread is armed again. It is delivered as an ordinary message with no special prompt, because there is nothing to fold it into — the slot it lands in is empty by construction.

Two details in how that response is chosen, and each closes a different failure that a simpler rule walks straight into.

**It is chosen by label, not taken from the queue head.** The tempting version is "if the head is a response, promote it". But a response does not necessarily outrank what is queued — `[MESSAGE-RESPONSE]` sits at 2, so an agent holding a `[TRUTHFUL-REPORT]` at 1 has a head that is *not* the answer, however long the answer has been sitting there. Reading the head would find that work, refuse to displace an equal-or-better slot, and return nothing on every pass forever: an agent waiting for an answer that had already arrived.

**And it must be fresh.** This one is subtler and it is the more interesting of the two. Interrupting pushes the agent's own working task back into its own queue. So if that task happened to carry a response label — an agent interrupted while working a `[MESSAGE-RESPONSE]` — the queue now holds a response row. A rule that accepted *any* response would fire on the agent's own pushed-back work. The act of interrupting would supply the very thing that undoes it, and the agent would restart itself on the next pass.

Only a response that genuinely *arrived* counts, which is why the lookup is scoped to unpaused rows.

While interrupted, a request arriving is admitted and left in the queue. It waits, and it is drained after the restart in priority order like everything else.

---

*Next: who actually watches the remote, how a thread knows to exist, and why a process refuses to drain a partner it can perfectly well see.*
