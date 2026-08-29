# Four: The journey of one message

## Following one thing all the way through

The previous notes built the machinery in pieces: a shared database, a queue with one authority table behind it, a slot that lives in memory, a thread that watches. This note follows a single message through all of it, in order, and then follows the one label that behaves differently.

The value of doing it in sequence is that the seams show. Most of the interesting behaviour in this system is not inside any one component — it is in the handoffs, where one thing finishes and another has to pick up without being told.

## Admission: one statement, and what it refuses

An agent calls `send` with four things: its own identity, the title of the recipient, the message body, and the label.

Before anything is written, a series of checks runs, and the order is not arbitrary — each depends on the last having passed.

The requester is resolved from its uuid to a live partner. The target is resolved from its title. The label is checked against the five that exist. All five are sendable — there is no label that exists only to be refused here.

Then the source rules, which come from `source_caps` rather than from conditionals. A source that cannot send is refused — a notebook has no agent behind it that decides to speak. A `[RESEARCH]` aimed at a source that does not accept delegated work is refused with a different code, because a notebook answers questions about what it holds rather than going and doing things, and those are different refusals for different reasons.

Then the direction rule, which applies to exactly one label. `[RESEARCH]` travels down the hierarchy or sideways, never up. A lower agent handing delegated work to a higher one would be reassigning its own director's work. Every other label travels freely in both directions, and that freedom is what lets an answer, an error, or a report come back at all.

Then the relationship check. If the target's source requires a handshake, one must exist — and here the system looks in **both** directions, which is worth pausing on.

A handshake row is directional, and the direction is not arbitrary: only an orchestrator may claim one, so the row always points from the orchestrator to the worker it directs. A worker therefore has no row of its own pointing back, and no way to create one. If only the forward row counted, a partner could be given work and have no way to say anything about it — not the mid-task question the research prompt explicitly tells it to send, not the error that says it is blocked.

So the reverse row is accepted too. Nothing new becomes reachable: it is the same pair, already joined by the same orchestrator. What it adds is the only thing a partner could not otherwise do, which is speak.

With one exception. `[RESEARCH]` still requires the **forward** row. The layer rule does not cover this case: a project-orchestrator and the plain worker it directs sit at the *same* layer, so "never travels up" does not fire between them. The forward-row requirement was what prevented a worker delegating back to its own director, and accepting the reverse row for every label would have given that away as a side effect of letting answers home. Answers travel back along a handshake; delegated work only travels along it in the direction the orchestrator claimed.

Finally, admission itself: a single `INSERT ... SELECT ... WHERE`, with the cap in the `WHERE` clause. Being over cap means the insert simply does not happen, and the caller is told which cap and why. One statement rather than read-then-write, because two callers can pass a read-then-write check simultaneously and a cap of three then admits four — under concurrency only, which is to say in production only.

## The receipt, and one thing it must never say

`send` returns a **receipt**, not a reply. It says the message was accepted and how deep the queue now is. The answer, if there is one, arrives later as a push.

The receipt ends with a line telling the agent not to poll for the result. That line is not decoration — an agent that has just been told its message was accepted will otherwise reasonably check back, which is the exact behaviour the whole system exists to make unnecessary.

There is one failure mode here worth naming because it is invisible. Admission commits, and *then* the queue is advanced — and advancing can fail in three ways. A rule can refuse it. The process can turn out to have no extension for the target's source. Or the remote itself can fail — a missing binary, a refused connection, an HTTP error.

If any of those were reported the way an ordinary refusal is, the caller would be told nothing changed. It would retry. And the retry would double-send, burning its cap on work the system already accepted.

So a failure raised *after* admission is marked as such, and the response says the message is queued rather than saying nothing happened. The third case is the one that bites hardest, because the ordinary way to report a failed remote ends with "send the work again" — which is exactly right when the send never landed, and a double-send when it did.

The distinction is between "the request was refused" and "the request was accepted and something later went wrong," and a caller needs to act differently on each.

## Promotion, rendering, delivery

`advance` compares the head against the slot, and if the head wins, the row is deleted and the task takes the slot. That comparison and its rules were the subject of note two.

What happens next is **rendering**, and it is easy to underrate.

A queue row holds the caller's raw text. What reaches the agent is that text wrapped in a prompt, and the wrapping carries information the raw text cannot.

Every prompt opens with a provenance header, and there are two of them. One means *the Polling Server is instructing you*. The other means *the Polling Server is showing you something a partner said*. An agent that cannot tell an instruction from a quotation answers the quotation — it treats the relayed message as the thing to act on, rather than as context for the thing to act on.

The label decides the shape. A `[RESEARCH]` gets a dispatch that inlines exactly which paths the partner may read and write, states plainly when it has none, and closes by telling it to begin. A `[QUERY]` aimed at a notebook gets a shape built for a notebook: the source it targets, context, and the question. Everything else gets a plain relay. A task returning from paused gets one line.

Then delivery hands the rendered prompt to the remote, and the adapter's own concerns take over — which is note five.

If delivery raises, the task goes **back** into the queue marked paused, and the error propagates to whoever triggered the advance. From the queue's point of view it is a task that started and stopped, which is exactly what paused means. Silently dropping it is the one outcome nothing downstream could detect: no error, no row, no task, no trace.

## Telling an agent who it is

There is a piece of the rendered prompt that exists to solve a problem you would not predict from the architecture.

`send`'s first argument is `requester_uuid` — the calling agent's own identity. A partner running inside a remote has never been told what its own uuid is. It was minted at creation and shown once, to whoever created it, which is not the same as being known by the agent now living in that session.

So the research dispatch and every relay carry an **identity block**: the agent's own title, its own uuid, and the call already filled in — its uuid as the sender, the caller's title as the recipient. Without it, the dispatch's own instruction to "message back a `[QUERY]` if you are missing context" names an action the agent has no credentials to perform.

The block says something else too, and that half is equally load-bearing in the opposite direction. **Answering is automatic.** Whatever the agent produces in its session is read back and delivered to the caller when the turn ends, whether or not it ever calls `send`. An agent handed only its identity, with no word that delivery already happens, would reasonably use that identity to send its answer — and the caller would receive the same work twice, once harvested and once sent, with no way to tell they were the same.

So the block states that `send` is for the case where the turn is *not* finishing: a question about missing context, or an error saying it is blocked. Then stop and wait.

Three templates carry no identity block, and only one of the three is an argued omission. A summary request and a resume line do not need it — the first is harvested, and the second is one line by design.

The notebook template is the deliberate one. A notebook has `can_send = 0`: there is no agent behind it to make that call. An instruction nothing can follow is worse than no instruction, because it invites the reader to look for a capability that does not exist.

## Harvesting, and what is kept

When the remote finishes, the result is read back and the reply is pushed. Note three covered the ordering — read before releasing the slot, so the next turn cannot start against a remote whose output has not been fetched.

What happens to that reply afterwards depends on one column.

`label_caps.stored` decides whether a message is written to durable history. Three labels are stored: `[QUERY]`, `[TRUTHFUL-REPORT]`, and `[MESSAGE-RESPONSE]`. Two are not: `[RESEARCH]` and `[ERROR]`.

The unstored two are **transport**. They travel in a queue, are acted on, and are never written down. A `[RESEARCH]` is an instruction whose *result* is what matters, and the result comes back as a `[TRUTHFUL-REPORT]`, which is stored. An `[ERROR]` is a condition to resolve; resolving it is all that is kept.

The rule is enforced by a database trigger rather than by a list of labels in a `CHECK`, and the reason is the same one that put priority and caps in a table: a list in the trigger would be a second copy of `label_caps.stored`, and two copies of one fact eventually disagree.

This is why the read capability shows some things and not others. It pages through stored history — questions asked, answers given, reports delivered. It cannot show you the `[RESEARCH]` that produced a report, because that instruction was transport.

## The research round trip

`[RESEARCH]` is the one label whose completion is not the end, and following it shows most of the system working at once.

**A research task is two exchanges against one remote inside one working slot.** Do the work, then report on it.

When the work finishes, the second exchange is *not* pushed into the queue as a new message. It is the same task, still in the slot, asked a second thing. Two reasons that matters.

A queued `[TRUTHFUL-REPORT]` would be ambiguous. The label would mean "summarise this" travelling one way and "here is the summary" travelling the other, and a queue row carries nothing that says which. Keeping the request out of the queue leaves the label with exactly one meaning everywhere it can be seen.

And the summary is protected while it is being written. The task's priority is raised to `[TRUTHFUL-REPORT]`'s for the second phase, and the effect is that from then on arriving messages **queue rather than reach the agent** — everything, because displacement needs a strictly lower priority number and nothing has one.

That blocking is the entire point of the raise, and it is worth stating as the counterfactual: a summary written while other traffic was reaching the same context would summarise the traffic.

## Aiming a summary at the right work

The prompt that asks for the summary is unusually specific, and each device in it prevents a particular way of getting a vague answer.

A work session holds far more than the work: false starts, tooling detours, the task the agent was displaced from and later resumed, intermediate results that later ones replaced. An agent asked to "summarise your work" summarises all of it, weighted by recency, and its most confident paragraphs end up being about whatever it touched last.

So the original request is quoted back **verbatim**. "That request" has to resolve to something, and by construction the agent has been holding more than one.

Resumed-from work is excluded explicitly, because otherwise a displaced-then-resumed task is reported twice — once in each report, with the two copies disagreeing.

And the shape of the answer is named: what was asked, what was found, the evidence for it, what remains. Uncertainty is to be stated in the same sentence as the claim rather than in a closing caveat, because a caveat at the end is read as politeness rather than as a qualification of anything specific.

## What survives an interruption

Now the failure this design has to survive, because it is the one that is silent.

The summary phase changes the task in place: it relabels it and marks it as owing a report. Both of those live on the in-memory slot.

If the summary is displaced — and only another summary can displace it — the task goes back into the queue. If the marker lived only in memory, the row that comes back is indistinguishable from an ordinary `[TRUTHFUL-REPORT]`, which is a *delivered* summary and owes nothing to anyone. On resume, the system would look up what a `[TRUTHFUL-REPORT]` replies with, find nothing, release the slot, and push nothing. **The research would be done and its answer discarded, with no error anywhere.**

It would also be rendered wrong. The general resume path hands back "resume your previous `[TRUTHFUL-REPORT]`" — one line, naming nothing the agent is holding, when what it actually needs is to be asked for the summary again.

So both facts travel on the queue row: that this is a summary phase, and what label the task was admitted under. A resumed summary is re-asked, against the original request the row still carries — which is why the body stays the original request rather than the summarise instruction. Summarising an instruction is not a thing anyone wants.

## Interruption, and who it happens to

There is one more flow, and it is where the system's refusal to add mechanisms shows most clearly.

An agent sends a **request** — a `[RESEARCH]` dispatching work, an `[ERROR]` reporting it is blocked, a `[QUERY]` asking for context it lacks. **That act interrupts the sender.** Its remote is stopped, whatever it was working on is pushed back into its own queue marked paused, its working slot is emptied, and its drain thread is stopped and deregistered.

Notice who is stopped. Not the recipient. The recipient finds a job in its queue and drains it in priority order like anything else — it is not disturbed at all. **There is no capability anywhere for one agent to stop another.** Being stopped is something you do to yourself by asking.

There is exactly one condition: the label is a request. Not the direction, not whether the sender held work.

Direction is worth pausing on, because an earlier shape of this rule stopped only an agent answering *upward*, on the reasoning that a caller dispatching work should keep going. That does not survive contact with what the labels mean. An orchestrator that keeps driving other work while its workers run is an orchestrator producing work it will have to redo the moment their answers land. Whoever handed work away is waiting on it. That is the whole rule.

An agent with nothing in flight is marked too. There is nothing to push back, but it has still said it is waiting on something, and the next arrival must not be handed to it as though it were free.

**And nothing takes the emptied slot.** An interrupted agent holds nothing at all — no placeholder, no synthetic task marking the state. A placeholder would be a row a reader could mistake for work, and one every count, cap and prompt would then have to exclude by hand.

Which leaves a question the design has to answer: an empty slot also describes an agent simply between two tasks. The difference is a column, `partners.interrupted`. Between tasks the drain thread is still running and should promote the next row; interrupted, there is no thread and nothing should be promoted. The slot looks the same in both. The flag is what tells them apart.

## What restarts it

A **response**, and the split is not a second list to keep in step with anything: it is exactly `reply_behavior IS NULL`. A label that expects an answer is a request; a label that *is* an answer is a response. The two that terminate exchanges are the two that restart agents.

The response takes the empty slot, clears the flag, and a thread is armed. It is delivered as an ordinary message — no special prompt, because there is nothing to fold it into. The slot it lands in is empty by construction.

Two details in the selection, each closing a different failure.

**Chosen by label, not taken from the head.** A response does not necessarily outrank what is queued: `[MESSAGE-RESPONSE]` is second, so an agent holding a `[TRUTHFUL-REPORT]` has a head that is not the answer. Reading the head would find that work, refuse to displace, and return nothing on every pass forever — waiting for an answer that had already arrived.

**Required to be fresh.** Interrupting pushes the agent's own working task back into its queue, so an agent interrupted while working a response now has a response row sitting there. A rule accepting any response would fire on that — the act of interrupting would supply the thing that undoes it.

## The one route with no response

An approval `[ERROR]` replies with nothing, deliberately: a reply to it would carry nothing the sender could act on.

So when a Partner stops on a permission it does not hold, and the Polling Server reports that upward as an `[ERROR]`, no response will ever come back to restart it. The Caller corrects the permissions, and **that correction is itself the signal** — the Polling Server clears the flag and arms a thread, and the Partner resumes with the work it was already holding still queued.

Which is why the prompt that Caller receives ends where it does. It says to investigate the permissions and fix them. It does *not* say to message the Partner back, and it does not say to resend the work, because both would duplicate something the system already does.

## What ends an exchange

Trace the labels one more time and the termination becomes visible as a property of the table rather than a property of any code path.

A `[RESEARCH]` is answered with a `[TRUTHFUL-REPORT]`, which is answered with nothing. Two hops, then silence.

A `[QUERY]` is answered with a `[MESSAGE-RESPONSE]`, which is answered with nothing. One hop, then silence.

An `[ERROR]` is answered with a `[MESSAGE-RESPONSE]`, which is answered with nothing. One hop — so the agent that sent a correction learns it landed, and the pair does not then continue congratulating each other.

Three labels reply with nothing, and they are the terminators. Every conversation in this system is a short walk that ends at one of them, and it ends there because a column says so — not because a code path remembered to stop.

---

*Next: the three remotes, and what it takes to talk to systems that were never built to be talked to this way.*
