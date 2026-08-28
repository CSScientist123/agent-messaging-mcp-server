# Three: Nobody polls — the drain thread

## The component that exists so agents do not have to

The first note ended on a constraint: an agent delegates and moves on, so something else has to do the waiting. This note is about that something.

It is called the Polling Server, and the name is a small joke at its own expense. It exists so that **no agent ever polls.** It does the polling, once, in one place, so that the polling never has to happen inside anyone's reasoning.

Its unit of work is the **drain thread**: one daemon thread per partner. That thread does three things in a loop. It asks `advance` to make progress on the queue. It waits for the remote to finish whatever ended up in the working slot. And it pushes the answer into the caller's queue when there is one.

Notice what is *not* in that list. There is no state machine. An earlier design had a five-state table recording where each task was, and the problem with such a table is that it can disagree with reality — the row says *delivering* and the delivery finished a minute ago. The queue and the working slot already answer "where is this task," and they answer it in one place each. What remains of "state" is exactly three possibilities: a task is queued, or it holds the working slot, or it is neither.

## What one pass actually does

A single pass through the loop is worth walking through, because each step handles a case the others cannot.

It calls `advance`, which may promote something, may displace something, may deliver something, or may do nothing at all. If `advance` reports that it needs a remote extension this process does not have, that is recorded and retried rather than treated as a lost message — the queue is untouched, because `advance` resolves the extension before it writes anything.

It then looks at the working slot. If the slot is empty and the queue is empty, the thread has nothing left to do, and that is the signal to retire.

If the slot holds an `[IDLE]`, the pass ends without polling anything. A hold is not work: there is nothing to ask the remote and nothing to report. The partner is stopped and stays stopped until something displaces the hold. Retiring here would be wrong, because the queue may hold the very task the interruption displaced — so the thread waits instead.

Otherwise the slot holds real work, and the thread asks the remote whether the turn is finished. That question has three possible answers and only two of them are obvious.

**Finished** means close out the task: read the result, release the slot, push a reply if one is owed.

**Not finished** means wait and ask again.

The third answer is the one that makes this component non-trivial: **blocked on a permission prompt.** The agent on the far side has stopped and is waiting for someone to approve something. It is neither busy nor finished, and no amount of asking will change that. Left alone, the slot stays held forever, the thread never retires, and nobody is ever told — because the partner cannot report it (an agent sitting on a prompt is not running) and the caller has no reason to look.

So the Polling Server reports it, on the partner's behalf. It stops the remote so it is not left sitting on the prompt, releases the slot, and pushes an `[ERROR]` into the caller's queue naming the conversation and what was asked for.

At priority 2 that `[ERROR]` outranks a good deal of what a caller might be doing — an answer it is processing, a research task, a hold — and displaces it. **That displacement is the interruption**, and there is no separate notification mechanism, because a priority queue already has one. It does not displace everything, though: displacement requires a *strictly* lower priority number, so a caller already working a `[QUERY]`, another `[ERROR]`, or a summary will finish that first and take the `[ERROR]` next. It is the front of the queue, not a signal that pre-empts unconditionally.

There is also the possibility that a remote cannot be asked about completion at all. A remote with no notion of turn completion refuses the question, and that refusal is treated as "finished" — because a remote that cannot be asked has already given the only answer it has. Polling it forever would hold the working slot against a partner that is perfectly free.

## The ways a thread comes to exist

A drain thread is not created when a partner is created. Partners outnumber active work enormously, and a thread per registered partner would mean hundreds of threads asking nothing.

Instead, a thread is **armed** — brought into existence when there is reason for one. There are several ways that happens, and each covers a case the others miss.

**Sending arms one.** When a message is admitted for a partner, that partner now has work, so the tool arms its thread directly — and the turn starts being watched immediately rather than at the next scan.

The arming is deliberately **best-effort**: it is wrapped so that a failure cannot turn a successful send into a reported failure. The receipt has already been earned by the time arming is attempted, and — because of the supervisor described below — arming only makes the answer arrive *sooner*, not at all. Reporting a committed message as rejected would make the caller retry and double-send, which is a real cost paid to avoid a delay of about a second.

**A push notification arms one.** A remote that can call back does so, and that endpoint arms the thread. This is not a tool an agent calls to get work done; it is the remote's own notification path.

**Completing a task arms one.** When a partner finishes and a reply is pushed to its caller, the caller now has something queued and needs a thread of its own. The same happens when an approval error is raised on a blocked partner's behalf. Without these the answer would wait for the supervisor's next scan rather than being picked up at once.

**A restart arms them.** Coming back up, a process resumes a thread for every partner still registered as having had one — which is what a surviving registration row means.

**And a supervisor arms one.** This is the interesting one, and it exists because of the gap the first note ended on.

## The gap only a supervisor can close

Recall the shape of the problem. A Claude Science process holds only the Claude Science extension. When one of its agents sends to an Antigravity partner, the row is admitted — it is a local write to a table everyone shares — but delivery cannot happen there. The message sits queued.

Nobody has told the Antigravity process anything. There is no notification. There is no bus. The row simply exists.

So each Polling Server runs one extra daemon thread that periodically **scans** for exactly this: partners of its own sources that have queued work, or that hold a task in its own working slot, without a live thread serving them. For each, it arms one.

That scan is the mechanism that makes a message sent by *any* process end up drained by the process that owns the target's remote. It is the same architectural move as everything else here: nobody is told, the fact is written where both can see it, and the one that can act on it looks.

The scan covers the working slot as well as the queue, and that second half is easy to miss. A same-process send *deletes* the queue row as it promotes the task into the slot — so a partner whose remote is mid-turn has an **empty queue**. If the arming that should have followed that send did not happen, a queue-only scan would look straight past a remote that is working with nobody watching it.

## Retirement, and the race it opens

A thread that has nothing to do should not exist. When a pass finds the queue empty and the slot empty, the thread retires — and deletes its own registration row.

Be precise about what that row is for, because it is easy to assume it does more than it does. **No arming path reads it.** Duplicate threads are prevented by an in-process dictionary of live threads, not by a database lookup. The row is read in exactly one place: on start-up, to bring back a thread for a partner that had one when the process went down.

So a row left behind by a retired thread does not strand a message — it causes a restart to spawn a thread for a partner with nothing to do, which then discovers as much and retires again. Deleting it on retirement keeps the registry meaning what it says.

But the retirement itself opens a window, and this part is a genuine liveness concern.

Picture it: a message is admitted and an arming attempt arrives in the gap between the loop deciding it is idle and the thread actually exiting. The arming logic sees the thread still *alive* in the dictionary, reports that one is already serving this partner, and spawns nothing. The thread then exits. The message is left with no live thread, and until the next supervisor scan nothing is watching it.

The fix is that the decision to retire is made **under the same lock** the arming decision is made under, and re-checked after taking it. That makes the two mutually exclusive: either the push gets in first and the thread sees the new work, or the thread retires first and the push finds no live thread and spawns one. There is no third outcome.

One further subtlety: the row is deliberately **not** deleted at shutdown. Stopping signals threads for a process that is going away with work possibly still queued, and the row is what a restart uses to bring that partner's thread back. A row deleted at shutdown would strand exactly the work it exists to protect.

## A process refuses to drain what it cannot serve

Here is a rule that reads like a limitation and is actually a correctness guarantee: **a process will not arm a thread for a partner whose source it holds no extension for.**

Consider what happens without it. Completing a task arms a thread for the caller — and a caller is routinely of a different source than the partner that answered it. A Claude Science process finishes an Antigravity partner's work and arms a thread for its Claude Science caller, which is fine. But an Antigravity process finishing work for a Claude Science caller would arm a thread it cannot possibly serve. That thread would fail on every single pass, forever. It would never retire, because failing is not the same as having nothing to do. And it would write a registration row that survives the process and re-arms the same doomed thread at the next start.

So the check happens before anything is spawned or written. If this process cannot serve that source, it declines — no thread, no row — and says so.

Restarting applies the same filter, with one addition that matters: rows belonging to another source are **left in place**, untouched. That process's own restart is what needs to find them still there. Deleting them would strand exactly the work they exist to protect.

`code_` is the cleanest case of this rule. There is no Claude Code adapter in any process, because a Claude Code session has no remote presence for an adapter to reach. A drain thread for one could never poll anything and could never stop anything. Its messages are stored, and they wait to be read. The same guard that stops a process draining another process's partners is what stops any process draining a `code_` one.

## Waiting at three different speeds

The loop waits between passes, and it waits by three different amounts depending on what it is waiting *for*. Each interval encodes a claim about the world.

**A quarter of the poll interval, when there is work in flight.** The remote is running; the answer could arrive at any moment; asking often is what "not polling" costs someone.

**Longer, when the slot holds a hold.** A held partner is deliberately stopped and has nothing to poll. Waking sixteen times a second to confirm that a stopped thing is still stopped buys nothing but wakeups.

Why is polling a hold slowly safe? Largely because **an agent's own message ends the hold as it is sent.** The `send` path calls `advance` directly, which displaces the hold and delivers the new task synchronously in the sender's own call — so on that path the thread is not racing to notice a resume, it is re-checking a slot that already changed.

The Polling Server's own replies are the exception worth knowing. When it pushes an answer to a parked caller, it does not advance that caller's queue itself; the caller's existing thread picks the swap up on its next pass. So a hold ended that way clears within one hold interval rather than instantly. That is the reason the interval is kept small rather than backed off indefinitely — and the reason it is an interval at all rather than an indefinite sleep waiting to be woken.

**And an exponentially growing wait, after a failure.** A failure that repeats is usually one that will keep repeating — an unreachable session, a refusing remote — and polling it at full rate buys nothing while costing a request every interval. The backoff is capped so a transient failure still recovers promptly, and it resets the moment a pass succeeds.

## Swallowing exceptions without swallowing the evidence

A drain thread is a daemon thread. An exception that escapes it kills it silently, and a silently dead thread is indistinguishable from a thread that retired properly — same absence, same missing row.

So exceptions are caught and the loop continues. That is right. But catching an exception and doing nothing else creates a subtler version of the same problem: **a thread failing on every single pass looks exactly like a thread with nothing to do.** Same silence, same absence of activity.

Two things prevent that. Every swallowed exception is logged at warning level, and every one is kept in a bounded record the server can be asked about.

Bounded matters. A repeating failure appends once per interval, and an unbounded list is a slow memory leak that only manifests in the situation where something is already wrong. The newest entries are the ones worth keeping, so the oldest are dropped.

That record is one of three the system keeps and can report: what a drain loop swallowed, which displacements went through against a remote that refused to be cancelled, and whatever an adapter collected while closing something. All three used to be write-only — written faithfully and read by nothing — which is a particular kind of uselessness: the information exists, and reaches no one.

## When a remote cannot be cancelled

Displacement stops the remote before the swap. But not every remote *can* be stopped.

NotebookLM is the clear case: it never executes anything, so there is no turn to stop, and an attempt to stop one is refused as a rule about what that kind of remote is.

Treating such a refusal as an error would mean no partner on that remote could ever be displaced, and the refusal would propagate all the way back to whoever called `send`, after their message was already committed.

So a **designed refusal** is told apart from a **failed cancellation**. The first is a fact about the remote: recorded, and the swap proceeds. The second is an error and stops the swap.

The consequence of proceeding is real and the system does not pretend otherwise: the old turn keeps running on the remote while the new one is delivered, and the agent sees both. That is the honest behaviour for a remote with no cancel. The alternative — refusing to displace anything on that remote — would be a worse system pretending to be a safer one. And because it is recorded rather than merely tolerated, an operator can find out that it happened.

## Closing out a task

When a turn finishes, the closing sequence has an ordering that is easy to get wrong.

If the task was a `[RESEARCH]`, it is not finished when the work stops — it owes a summary, which is a second exchange against the same remote inside the same slot. That is the subject of the next note.

Otherwise: the system looks up what the label replies with. If nothing, the slot is released and that is the end. If something, the result is read from the remote and pushed to the caller.

**The result is read before the slot is released,** and the order is not arbitrary. The slot is what stops another task being promoted and delivered to this same remote. Releasing first opens a window where the next turn can start against a remote whose previous output has not been fetched — and what comes back then belongs to neither turn.

Then the caller is armed, so the answer it just received is actually picked up rather than waiting for an unrelated push.

## Two error records that need each other

Two failures in this closing sequence are worth naming, because each is silent on its own.

If requesting a summary fails to reach the remote, the slot has already been relabelled. Left alone, the partner holds a task nobody asked it to do, forever: it is not working, so the remote reports finished, and the next pass reports a summary that was never requested. So the slot is released and the work is handed back to the caller as an `[ERROR]` naming what failed. The research itself is done — only the summary is lost — and that is precisely what the caller needs to know.

And if the recipient of a reply has been deleted or archived since the work started, pushing to it would fail on a foreign key **after** the slot was released. Both a crash and unrecoverable. So the recipient's liveness is checked, and a vanished recipient produces a quiet non-delivery rather than an exception thrown from a background thread into nobody's hands.

---

*Next: one message, followed all the way through — admitted, promoted, rendered, delivered, harvested, reported — and what a research task does differently.*
