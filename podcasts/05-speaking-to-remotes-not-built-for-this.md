# Five: Speaking to remotes not built for this

## Three different kinds of problem

The first four notes described a system with a clean interior: one queue per partner, one swap point, one authority table, one writer thread. This note is about the edge, where that interior meets three applications that were built for humans and know nothing about any of it.

None of the three offers an API designed for this. They offer, respectively: a command-line tool, an HTTP interface built for a web client, and a terminal user interface. Each is a different kind of problem, and the differences are not superficial — they change what the system can *know* about a turn.

The interface they are all forced through is deliberately small. Four methods every adapter must implement: verify a project exists, verify a partner exists within it, deliver a message, stop whatever is running. Everything beyond that has a refusing default, and the refusals are how the system stays honest about what each remote genuinely cannot do.

Three shapes of failure run through this boundary, and keeping them distinct is what lets a caller act on any of them:

A **rule saying no** — this system itself refuses, regardless of the remote's state.
A **capability that does not exist** — nothing is wrong; the extension was simply never taught to do that.
A **remote that was supposed to work and did not** — a missing binary, a refused connection, an HTTP error.

Collapsing those three loses exactly the information a caller needs. "It didn't work" sends you looking for a permission when the binary is missing, or looking for a binary when a rule refused you.

## NotebookLM: a remote that only answers

A notebook is the simplest of the three and the most different.

A project is a notebook. A partner is a *source* inside that notebook — a document, a paper, a URL. The adapter drives the `nlm` command-line tool: it verifies a notebook by asking for it, verifies a source by listing the notebook's contents and looking for the id, and delivers a message by firing a query.

Four things follow from what a notebook *is*, and all four are declared as data rather than discovered in code. It never executes, so there is no turn to stop and an attempt to stop one is refused as a rule. It needs no relationship established first, because a handshake authorises one agent to direct another and there is no agent here. It never originates a message. And it does not take delegated work.

Two details are worth dwelling on because they are properties of the tool rather than choices.

**The CLI has no per-source query.** A query is addressed to a whole notebook. So when the system asks a specific source a question, the source is named *in the prompt* — an instruction about where to look, not a filter the remote enforces. The template says so rather than implying a precision that is not there.

**And a notebook is the one remote whose result must be pulled.** Every other remote is an agent that could, in principle, be told to report back. A notebook cannot: it is a passive knowledge base with nothing inside it that decides to call anything. So the system reads the answer out directly, by asking the notebook for its latest conversation and taking the most recent turn.

There is a bounded wait built into delivery for exactly this reason. The query is fired, and the adapter waits before returning, to give the notebook real time to finish before anything tries to harvest an answer. Without it the harvest reads the state from before the question.

## Claude Science: an API, and one thing it will not do

Claude Science is the most conventional of the three: a local HTTP API, cookie-authenticated, with real endpoints.

A project is a project. A partner is a *frame* within it. Delivery posts into the frame. Completion is read from the frame's trace — the status field tells you whether it is running, queued, in progress, streaming, or processing, and anything outside that set means the turn is done.

Reading a result means fetching the frame's messages and taking the trailing run of assistant messages: walking backwards and stopping at the first message that is not the assistant. That run is precisely the answer to the last thing sent, because a single reply can span several messages — tool calls interleaved with prose — before the frame yields back. Anything at or before the last user message belongs to a previous exchange.

Messages flagged as harness notices are dropped first. Those are Claude Science's own runtime context injection — skill-discovery dumps, memory recall blocks — not either side of the conversation actually speaking. Returning one as the result hands the caller the application's bookkeeping in place of its answer.

Now the interesting part: **Claude Science has no usable interrupt.** The only route needs an execution identifier that no other call returns. So the adapter refuses to stop a turn, by design, every single time.

That refusal ripples. Displacement stops the remote before the swap. If a designed refusal counted as a failure, no Claude Science partner could ever be displaced at all — and the refusal would propagate back to whoever called `send`, after their message was already committed.

So the system distinguishes **"this remote has no cancel"** from **"the cancel failed."** The first is a fact about the remote: recorded, and the swap proceeds. The second is an error and stops the swap.

Proceeding has a real consequence and the system does not pretend otherwise: the old turn keeps running while the new one is delivered, and the agent sees both. That is the honest behaviour for a remote with no cancel. Refusing to displace anything on it would be a worse system pretending to be a safer one — and because the occurrence is recorded rather than merely tolerated, an operator can find out it happened.

The adapter also caches which project each frame belongs to, because every post needs it. That cache is populated when a partner is verified — which happens at creation, in whichever process did the creating. After a restart it is empty.

The fix is not to pre-populate it. The cache is given a **backing store**: on a miss it asks the database, which has held the answer all along as the partner's remote id joined to its project's system id. It caches what comes back and fails only when the answer genuinely is not there. A resolver rather than pre-population, because it self-heals wherever the adapter is used — including on paths that never go through the component that watches remotes at all.

The lookup is filtered by source, and that is not incidental: a remote id is unique only *within* a project. Unfiltered, a notebook adapter could be handed a Claude Science project id that happened to share an id string, and would address a query at a container that is not a notebook.

## Antigravity: reading a terminal

Antigravity is where the interior meets something genuinely hostile to being automated, and it is worth the detail because everything difficult about this boundary shows up here at once.

A project is a folder on disk. A partner is a conversation, which maps onto a `tmux` session named from the first eight characters of the conversation id. Delivery types text into that session and presses Enter — two separate calls, literal text first so that nothing in the message is read as a key name, then Enter on its own.

There is no API. **The only readable surface is the pane**, captured with `tmux capture-pane`.

That means every question the system wants to ask has to be answered by looking at a screen. Is the turn running? Is it finished? What did it say? Is it stuck? And a screen is a *rendering*, not a state — which is the root of everything that follows.

The adapter reads two footer markers that the client paints in every state. One means busy. One means ready for input. Those two strings are the entire completion signal.

## Three ways a screen lies, and what each costs

Reading state off a rendering fails in three distinct ways, and each has its own guard. Taken together they are the clearest illustration in this codebase of a general problem: **a remote whose state you observe rather than ask can report the state from before your action.**

**A screen repaints after the fact.** Type into a session and read immediately, and you get the screen from *before* the keystroke. The pane still shows an idle footer. Completion is then reported instantly, and the task closes with a placeholder answer while the agent goes on to produce the real one into a pane nobody is watching. So delivery waits for the pane to actually go busy before returning.

**A session may not be ready to receive at all.** A fresh workspace shows a modal trust dialog before it will accept any input — and the only check available before typing is whether the `tmux` session exists, which succeeds the instant it is created. Type into that dialog and the message goes into a menu. Then every check downstream agrees it worked: the busy wait times out, completion reports finished, and the result harvest returns the application's startup banner as the agent's answer. Nothing errors anywhere.

So delivery waits for the ready footer *before* it types anything, with a budget sized as a cold-start allowance rather than a guess — a cold start behind a trust dialog measured over ninety seconds. If a permission or trust dialog is what is blocking, it raises the approval error instead, which routes it into the path that already exists for one.

**And "not started yet" looks exactly like "already finished."** Both are an absent busy footer. If the model has not produced its first token when the busy wait expires, completion reads the idle pane as *done* — the caller receives an empty body and the slot is released while the agent is still about to answer.

The fix is not a longer wait before returning: delivery runs under the partner's lock, so waiting longer there stalls the queue. Instead, completion asks for **evidence that the turn started**. It records whether the busy footer was ever seen, and refuses to call an idle pane finished while the turn has not been seen running and a settle window since delivery has not elapsed. On a turn that does start, the footer clears the check immediately and this costs nothing.

## Finding an answer in a transcript

Harvesting a result from a pane is the hardest of the three, because a transcript has no structure marking where an answer begins.

The one anchor available is that the client **echoes back what was typed**. So the adapter remembers the last body it delivered to each session and uses that echo as a start marker — the *last* occurrence of it, since the same text can appear earlier, quoted back by the client itself.

Finding where the echo starts is not enough, though. A rendered prompt is many lines long. Slicing one line into it returns the rest of the instructions with the real answer buried at the end — for a question that is noise, and for a summary it would bury the report inside the request that asked for it.

So the echo is consumed **sequentially**: walking the pane forward alongside the prompt's own lines, consuming each as it matches, and stopping at the first line that does not continue it. Sequential rather than by membership, and the distinction is not academic. Testing whether a pane line appears *somewhere* in the prompt eats the answer whenever the prompt happens to contain it — and a prompt saying "reply with exactly the word PROVEN" contains the answer PROVEN.

Consuming in order also handles re-wrapping. The client reflows long lines, so one line of the prompt arrives as several pane lines matching no single line exactly — but each is a prefix of what remains of the line being consumed.

Two stopping conditions, both load-bearing. Running out of prompt lines is the ordinary end of a fully echoed prompt. A line that does not continue the echo stops it too, which bounds the skipping to the run immediately after the anchor — so an answer that legitimately quotes its own instructions later keeps them.

And the fallbacks are split rather than conflated. With **no recorded delivery** — a process restarted mid-turn — the whole pane is returned, because there is genuinely nothing to anchor on and a poor answer beats none. But when a body *was* recorded and its echo is absent, that is positive evidence the pane does not hold this turn, and returning it anyway is fabricating a remote's answer. It returns nothing instead.

## An approval is an error, never a question

Antigravity can raise permission and trust prompts, and this system's stance on them is absolute and worth stating as a rule rather than a behaviour:

**An approval prompt is an error. It is never a question this system answers.**

There is no method anywhere that types a response into one, and none should ever be added.

The reasoning is that a prompt means the grant was missing *before the work started*. Answering it papers over a configuration error at the worst possible moment — mid-turn, from a component with no way to judge whether the request is reasonable. And a system that can approve its own agents' requests has no permission boundary at all; it has a permission-shaped delay.

So a prompt is detected and reported. The turn is stopped, the slot is released, and an `[ERROR]` reaches the caller naming the conversation, the permission asked for, and the path requested. Where a prompt names neither — a trust dialog names no path — the fields say so explicitly rather than being omitted, because a reader cannot tell "not applicable" from "we failed to read it."

The message is deliberately short and prescriptive. It names the two capabilities that fix it and says to send the work again. A caller handed a full incident report starts debugging instead of granting.

This is also why there is no resume operation anywhere in the interface. **Correcting the grant and sending again *is* the resumption.** A remote-side resume would be a second way to start work, one that skips the queue — and the queue is where priority is decided.

## Configuring permissions through a screen

Antigravity is the only source that carries path permissions, and the way the adapter handles them shows the asymmetry of a screen-scraped remote in miniature.

**Reads are exact.** The current grants live in a project configuration file on disk, and the adapter reads them from there.

**Writes are not.** Granting a path means driving the client's permissions view: opening it, checking that what appeared is actually the editor, typing the rule, and confirming. A pane that does not say what it should is not the editor, and a rule typed into it would be posted into the chat instead — to an agent that would then try to act on it.

So every write is verified afterwards against the file, which is the exact half. **Reading a file is exact and typing into a TUI is not, so the typed half is the half that has to prove it worked.**

And the local record is written only *after* the remote is confirmed to hold the grant. The other order would leave the system claiming a grant that does not exist, which is the one direction of drift a caller cannot recover from by reading — it would send work that stops on a prompt for a permission it believes it has.

Removal exists for the same reason granting alone is not enough: a set that can only grow cannot be made to match one that shrank. A path granted by mistake would outlive every attempt to withdraw it.

## What the local record is, and is not

Worth being precise, because the two are deliberately allowed to differ.

The local table records the **intended** set of paths. The remote holds the **actual** one. The permission-reading capability reports both, plus both directions of drift: what is granted but not recorded, and what is recorded but not granted.

A report of one number would leave a caller unable to tell a missing grant from an unrecorded one — and those need opposite responses. One means work will stop on a prompt; the other means the bookkeeping is behind.

The difference between the two is not a bug to be eliminated. It is exactly what a caller needs to see in order to correct it.

---

*Next: the rules that hold all of this together, where each is actually enforced, and what the system can tell you about itself when something has gone wrong.*
