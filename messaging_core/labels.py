"""Behavior labels: what the six of them are, and where the rest of their meaning lives.

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
    "[IDLE]",
    "[TRUTHFUL-REPORT]",
    "[QUERY]",
    "[ERROR]",
    "[MESSAGE-RESPONSE]",
    "[RESEARCH]",
)

#: The one label no agent may hand to `send`. It exists to carry a forced
#: interruption, and `interrupt_partner` is the only thing that pushes it --
#: see `MessagingCore.interrupt_partner`. Accepting it in `send` would be a
#: second route to interrupting a partner, one that skips the same-project and
#: can-execute checks and never stops the remote.
INTERRUPT_BEHAVIOR = "[IDLE]"


def validate_behavior(behavior: str) -> None:
    """Raise Rejected("unknown_behavior", ...) if `behavior` is not a recognized label."""
    if behavior not in BEHAVIORS:
        raise Rejected(
            "unknown_behavior",
            f"{behavior!r} is not a recognized behavior label; expected one of {BEHAVIORS}.",
        )
