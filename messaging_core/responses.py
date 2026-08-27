"""Agent-facing response text.

House style, enforced by tests:

- A response starts with a bracketed marker stating the *kind* of outcome, so
  an agent can branch on the prefix without parsing prose: "[rejected] ...",
  "[nothing new] ...", "[still working - ...]".
- Every rejection ends with a line stating that nothing changed.
- Where there is a sensible next action, it is named as a concrete call, e.g.
  "Call search_partner to find the exact title."
- Anything asynchronous ends with ANTI_POLL, so an agent doesn't burn turns
  polling for a result that will arrive as an event.
"""

from __future__ import annotations

ANTI_POLL = "Do not poll. The event will carry the output."
NOTHING_CHANGED = "Nothing was changed."


def ok(body: str, *, next_call: str | None = None, anti_poll: bool = False) -> str:
    """Format a successful response.

    `next_call` is used verbatim -- callers pass the full sentence, e.g.
    "Call search_partner to find the exact title."
    """
    parts = [f"[ok] {body}"]
    tail_parts = []
    if next_call:
        tail_parts.append(next_call)
    if anti_poll:
        tail_parts.append(ANTI_POLL)
    if tail_parts:
        parts.append(" ".join(tail_parts))
    return "\n\n".join(parts)


def rejected(reason: str, *, noop: str = NOTHING_CHANGED, next_call: str | None = None) -> str:
    """Format a business-rule refusal. Always states that nothing changed."""
    tail = noop
    if next_call:
        tail = f"{tail} {next_call}"
    return f"[rejected] {reason}\n\n{tail}"


def nothing_new(what: str, *, next_call: str | None = None) -> str:
    """Format a "there is nothing to report" response."""
    parts = [f"[nothing new] {what}"]
    if next_call:
        parts.append(next_call)
    return "\n\n".join(parts)


def still_working(subject: str) -> str:
    """Format an in-progress response. Always ends with ANTI_POLL."""
    return f"[still working - {subject}]\n\n{ANTI_POLL}"
