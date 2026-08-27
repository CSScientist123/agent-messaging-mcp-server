"""Exception types shared across the messaging core.

Two kinds of failure are distinguished on purpose:

``Rejected`` is a documented business-rule refusal -- the request was understood
and nothing was wrong with the machinery, but a rule (a queue cap, an unknown
behavior, an archived title, ...) says no. Nothing is changed when this is
raised. It carries a stable ``code`` an agent or test can branch on, a
human-readable ``message``, and an optional ``next_call`` naming the concrete
next action available to the caller.

``NeedsRemote`` means the abstract layer -- this package -- has done everything
it can do on its own and the rest of the work requires a remote extension
(a live connection to an external system) that isn't this package's job to
provide. It is not a refusal of a rule; it is a statement of what capability
is missing and why.
"""

from __future__ import annotations


class MessagingError(Exception):
    """Base class for every exception raised by the messaging core."""


class Rejected(MessagingError):
    """A documented business-rule refusal. Nothing was changed.

    Attributes:
        code: A stable, machine-checkable identifier for the rule that fired
            (e.g. ``"unknown_behavior"``, ``"queue_full"``).
        message: A human-readable explanation of the refusal.
        next_call: An optional concrete next action the caller can take.
        already_committed: False by default -- "nothing was changed" above is
            the normal case. Some callers raise this same exception AFTER an
            earlier step of theirs has already landed (`MessagingCore.send`
            commits the queue push, then calls `advance()`, which can itself
            raise); for exactly those, the raiser sets this to True on the
            exception before it escapes. It means the request's local effect
            stands and only a later step failed, so nothing about the failure
            should invite a retry of the whole call -- retrying would repeat
            the part that already succeeded.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        next_call: str | None = None,
        already_committed: bool = False,
    ) -> None:
        self.code = code
        self.message = message
        self.next_call = next_call
        self.already_committed = already_committed
        super().__init__(str(self))

    def __str__(self) -> str:
        base = f"[{self.code}] {self.message}"
        if self.next_call:
            base = f"{base} Next: {self.next_call}"
        return base


class NeedsRemote(MessagingError):
    """The abstract layer cannot finish without a remote extension.

    Attributes:
        capability: The name of the missing capability (e.g. ``"send_email"``).
        reason: Why the abstract layer cannot provide it itself.
        already_committed: Same meaning as `Rejected.already_committed` --
            False unless a raiser sets it after its own earlier effect has
            already landed. See there for why the distinction matters.
    """

    def __init__(self, capability: str, reason: str, *, already_committed: bool = False) -> None:
        self.capability = capability
        self.reason = reason
        self.already_committed = already_committed
        super().__init__(str(self))

    def __str__(self) -> str:
        return f"needs remote capability {self.capability!r}: {self.reason}"
