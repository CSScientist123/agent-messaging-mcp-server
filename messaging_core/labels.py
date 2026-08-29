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

#: The three labels that ASK for something. Sending one interrupts ITS SENDER:
#: the sender has handed work away and is waiting on the outcome, so its working
#: task is pushed back paused, its slot is emptied, and its drain thread stops.
#: Interruption is always the sender's, never the recipient's -- a recipient just
#: finds a job in its queue.
#:
#: The complement is RESPONSE_BEHAVIORS below, and the split is exactly
#: `label_caps.reply_behavior IS NULL`: a label that expects an answer is a
#: request, and a label that IS an answer is a response.
REQUEST_BEHAVIORS: tuple[str, ...] = ("[RESEARCH]", "[ERROR]", "[QUERY]")

#: The two labels that ARE answers. Neither interrupts its sender, and either one
#: RESTARTS an interrupted recipient by taking its empty working slot.
RESPONSE_BEHAVIORS: tuple[str, ...] = ("[TRUTHFUL-REPORT]", "[MESSAGE-RESPONSE]")

def validate_behavior(behavior: str) -> None:
    """Raise Rejected("unknown_behavior", ...) if `behavior` is not a recognized label."""
    if behavior not in BEHAVIORS:
        raise Rejected(
            "unknown_behavior",
            f"{behavior!r} is not a recognized behavior label; expected one of {BEHAVIORS}.",
        )
