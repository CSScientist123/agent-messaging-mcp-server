"""Behavior labels: what the five of them are, and where the rest of their meaning lives.

A label is only a label. It carries no direction: every labelled message is a
push into the recipient's single priority queue, and the same label travels
both ways. What a label *does* decide is which task holds the working slot,
because each label has a priority.

That priority -- along with how many of a label one caller may have
outstanding, and whether the label is ever written to `messages` -- is
deliberately NOT in this module. It lives in the `label_caps` table, and every
decision that depends on it is made inside SQL against that table. There is
one authority for it, and a second copy here would be a second authority that
could disagree.

What is here is the tuple of recognized labels, used to refuse an unknown one
at the tool boundary with a readable message rather than a foreign-key error
from four frames deeper.
"""

from __future__ import annotations

from .errors import Rejected

#: Every label the system recognizes, in priority order (highest first). The
#: order is documentation; `label_caps.priority` is what the code reads.
BEHAVIORS: tuple[str, ...] = (
    "[TRUTHFUL-REPORT]",
    "[ERROR]",
    "[QUERY]",
    "[RESEARCH]",
)

#: The labels that are REQUESTS: they ask somebody for something, and the sender
#: cannot proceed until it is answered. Sending one forces an interruption --
#: the sender's working task is pushed back paused and its slot is left EMPTY
#: until the answer arrives.
#:
#: A mirror of `label_caps.is_request`, which is the authority. This tuple exists
#: only so the hot path does not need a query to ask a question the table has
#: already answered; `tests/test_foundation.py` asserts the two agree, so a
#: divergence is a failing test rather than a silent second opinion.
#:
#: Note this is a strictly wider set than the labels that may travel the shortcut
#: channel. `[RESEARCH]` is a request and forces a wait like any other, but
#: delegating new work down a channel that exists for mid-task clarification is
#: not what it is for -- `send` refuses it there by name.
REQUEST_BEHAVIORS: tuple[str, ...] = ("[ERROR]", "[QUERY]", "[RESEARCH]")

#: Backwards-compatible alias. Every request blocks its sender, so the two sets
#: are the same thing named from two directions -- "what it is" and "what it does".
BLOCKING_BEHAVIORS: tuple[str, ...] = REQUEST_BEHAVIORS

#: What may be pushed into the shortcut channel today. Narrower than
#: REQUEST_BEHAVIORS on purpose; see its note above.
SHORTCUT_BEHAVIORS: tuple[str, ...] = ("[ERROR]", "[QUERY]")


def validate_behavior(behavior: str) -> None:
    """Raise Rejected("unknown_behavior", ...) if `behavior` is not a recognized label."""
    if behavior not in BEHAVIORS:
        raise Rejected(
            "unknown_behavior",
            f"{behavior!r} is not a recognized behavior label; expected one of {BEHAVIORS}.",
        )
