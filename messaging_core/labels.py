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
    "[QUERY]",
    "[ERROR]",
    "[MESSAGE-RESPONSE]",
    "[RESEARCH]",
)

#: The two labels an agent uses to say it cannot continue on its own -- a
#: question about what was meant, or a statement that something blocked it.
#: Sending either stops the sender: its working task is pushed back paused and
#: the question itself takes the slot until an answer arrives.
#:
#: Their rank in `label_caps.priority` is the entire interruption mechanism:
#: nothing below it can reach an agent while it waits. There is no separate
#: hold label, because the question IS the hold. Only a `[TRUTHFUL-REPORT]`
#: outranks a waiting agent, which is the one interruption worth allowing --
#: a summary must not be contaminated by other traffic.
BLOCKING_BEHAVIORS: tuple[str, ...] = ("[QUERY]", "[ERROR]")


def validate_behavior(behavior: str) -> None:
    """Raise Rejected("unknown_behavior", ...) if `behavior` is not a recognized label."""
    if behavior not in BEHAVIORS:
        raise Rejected(
            "unknown_behavior",
            f"{behavior!r} is not a recognized behavior label; expected one of {BEHAVIORS}.",
        )
