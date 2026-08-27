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
"""

from __future__ import annotations

INSTRUCTS = "[Polling Server messages you]"
RELAYS = "[Polling Server]"


def research_dispatch(*, caller_title: str, body: str, read_paths: list[str],
                      write_paths: list[str]) -> str:
    """Render the `[RESEARCH]` dispatch, with the partner's configured paths inlined.

    The path block is the part that matters and the part that is easy to get
    wrong by omission. An Antigravity conversation that is not told what it
    may touch will reach for something outside its grant, and reaching for it
    raises an approval prompt -- which this whole design treats as an error
    rather than a question. So the paths are stated, and the instruction not
    to leave them is stated in the same breath as the reason.

    Args:
        caller_title: Title of the Caller delegating the work.
        body: The Caller's message, verbatim.
        read_paths: Paths the partner may read. May be empty.
        write_paths: Paths the partner may write, including files that do not
            exist yet.

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


def idle_interruption(*, caller_title: str, reason: str) -> str:
    """Render the `[IDLE]` forced interruption.

    Two sentences, deliberately. An interrupted agent handed a paragraph
    starts working on the paragraph.
    """
    return "\n".join([
        INSTRUCTS,
        "",
        f"Caller {caller_title} stopped you:",
        "",
        reason,
        "",
        "Stop what you are doing and wait. You will be told when to continue, and the work "
        "you were stopped on is remembered - do not restart it and do not summarize it yet.",
    ])


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


def relay(*, caller_title: str, behavior: str, body: str) -> str:
    """Render a plain relay: a Partner's message passed through unaltered.

    Used for every label with no template of its own -- `[QUERY]`, `[ERROR]`,
    `[MESSAGE-RESPONSE]`. The header is `[Polling Server]`, not
    `[Polling Server messages you]`, because the Server is showing the agent
    something rather than telling it something.
    """
    return "\n".join([RELAYS, "", f"{caller_title} sends you a {behavior}:", "", body])
