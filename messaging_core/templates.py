"""What the Polling Server actually says when it hands a task to a remote.

A queue row holds a Caller's raw message. What reaches the remote is that
message wrapped in a prompt, and the wrapping is not decoration: it tells the
agent who is speaking, what persona to answer in, what the reply must contain,
and -- for a `gemini_` partner -- exactly which paths it is allowed to touch.

Every template here mirrors one in the project note "Prompt templates". Where
the two disagree, the note is the source of truth and this file is the bug.

Two rules the shapes encode, both of which exist because their absence caused
a real failure:

**A provenance header on every prompt.** `[Polling Server messages you]` when
the Server is instructing the agent, `[Polling Server]` when it is relaying a
Partner verbatim. An agent that cannot tell an instruction from a quotation
answers the quotation.

**A resume line at the end of every interruption.** An interrupted agent with
no closing instruction treats the interruption as its new task and never goes
back.

**And nothing at all for an agent waiting on its own question.** The wait is
not a message. The remote is already stopped before the slot changes hands, so
there is no prompt to render and none is rendered -- handing a stopped agent a
paragraph gives it something to act on when the entire point is that it should
be doing nothing until it hears back.

**A resolution is never delivered bare.** When the answer arrives it is folded
into whatever the agent should do next, because a response on its own leaves an
agent holding a fact and no instruction. Only when the queue is empty behind it
does the answer stand alone.
"""

from __future__ import annotations

INSTRUCTS = "[Polling Server messages you]"
RELAYS = "[Polling Server]"


def identity_block(*, partner_uuid: str, partner_title: str, caller_title: str) -> list[str]:
    """Lines telling the agent who it is and how a reply actually leaves it.

    Every call into `send` takes `requester_uuid` -- the agent's OWN uuid --
    and no prompt ever states it. Without this block an agent told to
    "message back a [QUERY]" holds an instruction it has no credentials to
    carry out. `partner_uuid` and `partner_title` are what let it fill in
    that call itself instead of guessing at an identity it was never given.

    The "answering is automatic" sentence is the other half, and it is not
    optional framing -- it is the fix for a distinct failure from the one
    above. This session's result is harvested and delivered to the Caller
    the moment the turn ends, whether or not the agent ever calls `send`. An
    agent that was only handed its own identity, with no word that delivery
    already happens, would reasonably use it to send its answer -- and the
    Caller would receive that answer twice, once harvested and once sent.
    Stating plainly that `send` is for the turn NOT finishing -- a [QUERY]
    for missing context, an [ERROR] when blocked -- is what keeps the two
    delivery paths from overlapping.
    """
    return [
        f"You are {partner_title}. Your own requester_uuid, for any `send` call you make, is:",
        "",
        f"  {partner_uuid}",
        "",
        f"Answering is automatic: whatever you produce in this session is read back and "
        f"delivered to {caller_title} when this turn finishes. Do NOT send your answer "
        "yourself -- doing so delivers it twice.",
        "",
        "`send` is for one thing only: something that cannot wait for the turn to end. Use "
        "[QUERY] when you are missing context only the Caller holds, or [ERROR] when you are "
        "blocked and cannot continue. Then stop and wait.",
        "",
        "The call, with your real identity already filled in:",
        "",
        f'  send(requester_uuid="{partner_uuid}", queried_partner_title="{caller_title}", '
        'behavior="[QUERY]", message="...")',
    ]


def research_dispatch(*, caller_title: str, body: str, read_paths: list[str],
                      write_paths: list[str], partner_uuid: str, partner_title: str) -> str:
    """Render the `[RESEARCH]` dispatch, with the partner's configured paths inlined.

    The path block is the part that matters and the part that is easy to get
    wrong by omission. An Antigravity conversation that is not told what it
    may touch will reach for something outside its grant, and reaching for it
    raises an approval prompt -- which this whole design treats as an error
    rather than a question. So the paths are stated, and the instruction not
    to leave them is stated in the same breath as the reason.

    Also carries the identity block (see `identity_block`), because this
    template is the one that tells the agent to "message back a [QUERY] and
    idle" -- an instruction it cannot follow without its own uuid, sent to
    it nowhere else.

    Args:
        caller_title: Title of the Caller delegating the work.
        body: The Caller's message, verbatim.
        read_paths: Paths the partner may read. May be empty.
        write_paths: Paths the partner may write, including files that do not
            exist yet.
        partner_uuid: This partner's own uuid, for the identity block.
        partner_title: This partner's own title, for the identity block.

    Returns:
        The full prompt text to hand to the remote.
    """
    lines = [INSTRUCTS, "", f"/goal Caller {caller_title} delegates the following work to you:",
             "", body, ""]
    if read_paths or write_paths:
        lines += ["Read and write paths already configured for you:", ""]
        lines += [f"  read   {p}" for p in read_paths]
        lines += [f"  write  {p}" for p in write_paths]
    else:
        lines += ["No read or write paths are configured for you. Do not touch the "
                  "filesystem at all."]
    lines += [
        "",
        "Work to a verifiable result: prefer quantitative evidence and named sources over "
        "assertion. If you find you are missing context that only the Caller holds, do not "
        "guess - message back a [QUERY] and idle.",
        "",
        "Never request an approval. If you find yourself blocked on a permission, that is a "
        "configuration error on the Caller's side, not a question for you to ask. Say what "
        "you were missing and stop.",
        "",
    ]
    lines += identity_block(
        partner_uuid=partner_uuid, partner_title=partner_title, caller_title=caller_title
    )
    lines += [
        "",
        "Begin now. You will be asked to summarize when you are done.",
    ]
    return "\n".join(lines)


def truthful_report_request(*, caller_title: str, original_request: str) -> str:
    """Render the `[TRUTHFUL-REPORT]` request, aimed at one section of a long session.

    The failure this template exists to prevent is quiet and expensive. A work
    session holds far more than the work: false starts, tooling detours, the
    task the agent was displaced from and later resumed, and intermediate
    results that later ones replaced. An agent asked to "summarize your work"
    summarizes all of it, weighted by recency, and its most confident
    paragraphs end up being about whatever it touched last.

    Two devices do most of the aiming. The original request is quoted back
    **verbatim**, because "that request" has to resolve to something and by
    construction the agent has been holding more than one. And resumed-from
    work is excluded explicitly, because otherwise a displaced-then-resumed
    task is reported twice -- once in each report, with the two copies
    disagreeing.

    Args:
        caller_title: Title of the Caller asking for the summary.
        original_request: The request being closed out, quoted back exactly as
            it was sent.

    Returns:
        The full prompt text to hand to the remote.
    """
    return "\n".join([
        INSTRUCTS,
        "",
        f"Caller {caller_title} asks you to close out this work:",
        "",
        original_request,
        "",
        "Summarize ONLY the work that answers that request. Specifically:",
        "",
        "- Start from where you began that request, not from the start of the session. "
        "Earlier work in this session, and anything you were displaced from and later "
        "resumed, belongs to a different report.",
        "- Report the result you actually reached. Where you reached it partially, say "
        "which part.",
        "- Give the evidence: files written, sources read, numbers measured. Name them.",
        "- State what you tried that did not work ONLY where it constrains what the Caller "
        "should do next. A list of everything you attempted is not a result.",
        "- Where you are uncertain, say so in the same sentence as the claim, not in a "
        "closing caveat.",
        "",
        "Do not include: setup steps, tool errors you recovered from, or work you did for a "
        "different request.",
        "",
        "Expected context inside the reply message:",
        "",
        "- What was asked, in one sentence",
        "- What you found or produced",
        "- The evidence for it",
        "- What remains, if anything",
    ])


def notebook_query(*, caller_title: str, source: str, body: str) -> str:
    """Render a `[QUERY]` aimed at a NotebookLM source.

    A notebook is not an agent. It holds sources and answers questions about
    them; it never acts, and `source_caps` says so -- `can_execute = 0`,
    `can_send = 0`, `accepts_research = 0`. The generic `relay` is written for
    an agent: it announces a speaker, hands over a message, and closes with the
    call the recipient may answer with. Only the middle third means anything
    here.

    So this carries no identity block. With `can_send = 0` there is no agent
    behind the notebook to make that call, and an instruction nothing can
    follow is worse than no instruction -- it invites the reader to look for a
    capability that does not exist.

    `source` names which of the notebook's sources the question is aimed at,
    and naming it is the only aiming there is: the `nlm` CLI has no per-source
    query, so `deliver_message` asks the whole notebook. The section is an
    instruction about where to look, not a filter the remote enforces, and the
    wording says so rather than implying a precision that is not there.

    Args:
        caller_title: Title of the Caller asking the question.
        source: The `partner_id_in_remote` of the source being addressed --
            for a notebook, that is the source's own URL or id.
        body: The Caller's question, verbatim.

    Returns:
        The full prompt text to hand to the remote.
    """
    return "\n".join([
        RELAYS,
        "",
        f"{caller_title} asks this notebook a question.",
        "",
        "## Targeted URLs inside current Notebook",
        "",
        source,
        "",
        "The question reaches the whole notebook; the source above is where the answer "
        "should be drawn from. Where the notebook's other sources contradict it, say so "
        "rather than silently preferring one.",
        "",
        "## Context",
        "",
        f"Asked by {caller_title}.",
        "",
        "## Query",
        "",
        body,
    ])


def resolution(*, asked_behavior: str, response: str, next_job: str | None = None,
               resumed_behavior: str | None = None) -> str:
    """Render an answer to a blocking question, folded into whatever comes next.

    A raw answer is close to useless on its own. The agent that asked was
    stopped mid-work, and by the time the answer arrives the thing it should do
    next is already decided -- it is the head of its own queue. Handing over
    just the response leaves the agent holding a fact and no instruction, and
    it has to guess whether to resume, wait, or start something.

    So the response and the next instruction arrive as one prompt. Three
    shapes, and which one applies is decided by what the queue actually holds:

    - Something new is waiting: the answer, then that work, in full.
    - Paused work is waiting: the answer, then one line naming what to resume.
      The body is not restated -- the agent never stopped holding it, and a
      worse copy would invite it to start over.
    - Nothing is waiting: the answer alone. This is the only case where a bare
      response is the right prompt, because there is nothing to attach it to.

    Args:
        asked_behavior: The label of the question being answered -- `[QUERY]`
            or `[ERROR]`.
        response: What the answering agent said, verbatim.
        next_job: The body of a fresh task being promoted behind the answer.
        resumed_behavior: The label of a paused task being resumed instead.

    Returns:
        The full prompt text to hand to the remote.
    """
    lines = [
        INSTRUCTS,
        "",
        f"Resolution attempt on {asked_behavior} is returned.",
        "",
        f"Response: {response}",
    ]
    if next_job is not None:
        lines += ["", "Resume your work with this new job:", "", next_job]
    elif resumed_behavior is not None:
        lines += ["", f"Resume your work on: {resumed_behavior}"]
    return "\n".join(lines)


def awaiting_resolution(*, asked_behavior: str, target_title: str) -> str:
    """What the queue records while an agent waits on its own question.

    Never delivered anywhere. The agent that asked has already been stopped;
    this is the text a human reading `status` sees in the working slot, and it
    exists so that "what is this partner doing" has an answer other than a
    label with no context.
    """
    return (
        f"Waiting on {target_title} to answer the {asked_behavior} just sent. "
        "The work paused behind this resumes when that answer arrives."
    )


def resume_displaced(*, behavior: str) -> str:
    """Render the one-line prompt for a task returning to the working slot.

    One line is the whole template. Among the queued tasks carrying a single
    label at most one is marked `in_process`, and it is always picked before
    the others of that label -- so "your previous [RESEARCH]" resolves to
    exactly one thing, which the agent is still holding in its own context.
    Restating the work would hand back a worse copy of something it has not
    forgotten, and would invite it to start over instead of continue.
    """
    return f"{INSTRUCTS}\n\nResume your previous {behavior}."


def relay(*, caller_title: str, behavior: str, body: str, partner_uuid: str,
          partner_title: str) -> str:
    """Render a plain relay: a Partner's message passed through unaltered.

    Used for every label with no template of its own -- `[QUERY]`, `[ERROR]`,
    `[MESSAGE-RESPONSE]`. The header is `[Polling Server]`, not
    `[Polling Server messages you]`, because the Server is showing the agent
    something rather than telling it something.

    Also carries the identity block (see `identity_block`). This is the
    template that hands an agent an `[ERROR]` telling it something is
    blocked -- exactly the shape of agent that may need to answer back with
    its own `[QUERY]` or `[ERROR]`, and it needs its own uuid to do that.
    """
    lines = [RELAYS, "", f"{caller_title} sends you a {behavior}:", "", body, ""]
    lines += identity_block(
        partner_uuid=partner_uuid, partner_title=partner_title, caller_title=caller_title
    )
    return "\n".join(lines)
