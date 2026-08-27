"""The boundary between the shared messaging logic and a remote application.

The logic layer (the messaging core, the Polling Server) knows nothing about
NotebookLM, Claude Science, Antigravity, or any other concrete remote. Anywhere
it needs to reach out to one, it does so exclusively through a
:class:`RemoteExtension`. This module defines that boundary, a specialization
for remotes that never execute anything (:class:`NonExecutingExtension`), and
an in-memory fake (:class:`StubExtension`) that every test in this project can
use instead of a real remote.

Concrete adapters (one per remote) live under ``adapters/`` and subclass
:class:`RemoteExtension` (or :class:`NonExecutingExtension`, for a remote like
NotebookLM that provides context but never runs anything). This module does
not import any of them -- the dependency points the other way.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from messaging_core.errors import NeedsRemote, Rejected

__all__ = ["RemoteExtension", "NonExecutingExtension", "StubExtension"]


class RemoteExtension(ABC):
    """The full set of operations the messaging core can ask a remote to perform.

    ``source_prefix`` identifies which family of remote a subclass speaks for
    and must be one of the values ``source_caps.source_prefix`` allows in the
    schema: ``"nlm_"``, ``"code_"``, ``"science_"``, or ``"gemini_"``.

    Four operations are abstract because every remote must answer them in its
    own way: whether a project and a partner really exist on the remote side,
    how to hand a message to the remote, and how to stop whatever the remote
    is currently doing.

    There is deliberately no resume operation. Correcting whatever blocked the
    partner and sending the work again *is* the resumption -- read
    :meth:`add_permissions` and the project note "Antigravity state handling".
    A remote-side resume would be a second way to start work, one that skips
    the queue, and the queue is where priority is decided.

    Two more -- :meth:`poll_completion` and
    :meth:`read_remote_result` -- are concrete with a default that raises
    :class:`NeedsRemote`, because most remotes either don't need a distinct
    "is it done yet?" check beyond what completing a call already tells the
    caller, or report their own results some other way; only a passive
    knowledge base like NotebookLM, which cannot act on its own, needs the
    Polling Server to pull a result out of it directly.
    """

    source_prefix: str

    @abstractmethod
    def verify_project_system_id(self, project_system_id: str) -> bool:
        """True if `project_system_id` names a real project on the remote."""
        raise NotImplementedError

    @abstractmethod
    def verify_partner_id_in_remote(self, project_system_id: str, partner_id_in_remote: str) -> bool:
        """True if `partner_id_in_remote` names a real session/partner under that project."""
        raise NotImplementedError

    @abstractmethod
    def deliver_message(self, *, partner_id_in_remote: str, behavior: str, body: str) -> str:
        """Hand `body` to the remote partner and return a remote-side identifier.

        The identifier is whatever the remote needs to later answer
        "has this finished?" (e.g. a run id, a turn id) -- it is opaque to the
        messaging core and is only ever passed back to this same extension.
        """
        raise NotImplementedError

    @abstractmethod
    def stop_remote_execution(self, *, partner_id_in_remote: str, reason: str) -> None:
        """Stop whatever the remote partner is currently doing, and why."""
        raise NotImplementedError

    # -- permissions -------------------------------------------------------
    #
    # Concrete, not abstract, and refusing by default. Only a remote that
    # executes against a filesystem has anything to grant: Antigravity does,
    # NotebookLM never executes at all, and a Claude Science frame has no
    # per-frame path concept. A default that refuses is what lets those two
    # adapters stay honest without writing three stubs each.
    #
    # Rules are strings in the remote's own grammar -- `write_file(/path)`,
    # `read_file(/path)`. The core builds them and never parses them back
    # apart, so a remote with a different grammar changes this contract, not
    # the caller.

    def get_permissions(self, *, partner_id_in_remote: str) -> list[str]:
        """Return the rules the remote partner is currently allowed, as strings."""
        raise Rejected(
            "not_path_configurable",
            f"{type(self).__name__} does not carry per-partner path permissions.",
        )

    def add_permissions(self, *, partner_id_in_remote: str, rules: list[str]) -> None:
        """Grant `rules` to the remote partner.

        Adding a rule the partner already holds is not an error. The caller
        checks what landed by reading :meth:`get_permissions` back -- this
        method's own return value would be the remote's opinion of its own
        success, which is the thing being verified.
        """
        raise Rejected(
            "not_path_configurable",
            f"{type(self).__name__} does not carry per-partner path permissions.",
        )

    def delete_permissions(self, *, partner_id_in_remote: str, rules: list[str]) -> None:
        """Revoke `rules` from the remote partner.

        Revoking a rule the partner does not hold is not an error. Granting
        without revoking cannot correct a permission set -- a rule added by
        mistake would outlive every attempt to withdraw it -- which is why
        this exists as a separate operation rather than an overwrite.
        """
        raise Rejected(
            "not_path_configurable",
            f"{type(self).__name__} does not carry per-partner path permissions.",
        )

    def poll_completion(self, *, partner_id_in_remote: str) -> bool:
        """True once the remote has finished its current turn.

        The default assumes the remote has no distinct notion of this and
        raises :class:`NeedsRemote`; a concrete extension overrides this only
        if its remote actually supports being polled this way.
        """
        raise NeedsRemote(
            "poll_completion",
            f"{type(self).__name__} does not support polling for turn completion.",
        )

    def read_remote_result(self, *, partner_id_in_remote: str) -> str:
        """Read back whatever the remote produced, without the remote acting on its own.

        Only NotebookLM implements this for real: it is a passive knowledge
        base that cannot call back into the messaging system on its own, so
        the Polling Server must pull its answer out directly. Every other
        remote executes -- its own agent process reports results by acting,
        not by being read from -- so the default here raises
        :class:`NeedsRemote`.
        """
        raise NeedsRemote(
            "read_remote_result",
            f"{type(self).__name__} does not support reading a result directly; "
            "only NotebookLM does.",
        )


class NonExecutingExtension(RemoteExtension):
    """Base for remotes where a session provides context but never executes.

    A NotebookLM source, for instance, never runs anything -- "stop its
    execution" is a meaningless request for it. Rather than leaving that
    abstract method unimplemented (which would surface as a confusing
    AttributeError/NotImplementedError only when someone finally tried it), it
    is implemented here to refuse cleanly with the same `Rejected` vocabulary
    the rest of the system uses.

    The other three abstract methods (`verify_project_system_id`,
    `verify_partner_id_in_remote`, `deliver_message`) remain abstract: a
    non-executing remote still has to be found, verified, and messaged.
    """

    def stop_remote_execution(self, *, partner_id_in_remote: str, reason: str) -> None:
        raise Rejected(
            "not_executable",
            f"{type(self).__name__} never executes anything; there is nothing to stop.",
        )


class StubExtension(RemoteExtension):
    """An in-memory fake `RemoteExtension`, built for tests.

    Every call is recorded on `.calls` as `(method_name, kwargs_dict)`, in
    order, so a test can assert not just that something happened but exactly
    what was asked and in what order. The two verification methods return
    `True` by default (`.verify_project_system_id_result` and
    `.verify_partner_id_in_remote_result` are plain settable attributes for
    the tests that need a `False`), `.completed` is what `poll_completion`
    returns, and `.read_remote_result_value` is what `read_remote_result`
    returns -- unlike the real `RemoteExtension` default, both of these are
    implemented here (not left to raise `NeedsRemote`) precisely so a stub
    can stand in for any remote, executing or not. Permission rules are held
    in `.permissions`, and `.permissions_refuse` makes the stub accept a write
    and quietly not apply it.

    Example::

        stub = StubExtension(source_prefix="science_")
        stub.completed = False          # remote hasn't finished yet
        stub.verify_project_system_id_result = False  # simulate an unknown project
    """

    def __init__(self, *, source_prefix: str = "code_") -> None:
        self.source_prefix = source_prefix
        self.calls: list[tuple[str, dict]] = []

        self.verify_project_system_id_result = True
        self.verify_partner_id_in_remote_result = True
        self.completed = True
        self.read_remote_result_value = ""
        # Permission rules the fake remote "holds", keyed by partner id.
        self.permissions: dict[str, list[str]] = {}
        # Rules this stub silently refuses to add or remove, so a test can
        # exercise the case that matters most: a write the remote appears to
        # accept and then does not actually apply. That is the failure the
        # verify-after-write in `MessagingCore._apply_and_verify` exists for,
        # and it cannot be reached with a stub that always cooperates.
        self.permissions_refuse: set[str] = set()
        # If set, deliver_message always returns this. Otherwise it returns a
        # fresh, distinguishable id on every call.
        self.deliver_message_result: str | None = None
        self._delivery_count = 0

    def _record(self, name: str, **kwargs) -> None:
        self.calls.append((name, kwargs))

    def verify_project_system_id(self, project_system_id: str) -> bool:
        self._record("verify_project_system_id", project_system_id=project_system_id)
        return self.verify_project_system_id_result

    def verify_partner_id_in_remote(self, project_system_id: str, partner_id_in_remote: str) -> bool:
        self._record(
            "verify_partner_id_in_remote",
            project_system_id=project_system_id,
            partner_id_in_remote=partner_id_in_remote,
        )
        return self.verify_partner_id_in_remote_result

    def deliver_message(self, *, partner_id_in_remote: str, behavior: str, body: str) -> str:
        self._record(
            "deliver_message",
            partner_id_in_remote=partner_id_in_remote,
            behavior=behavior,
            body=body,
        )
        self._delivery_count += 1
        if self.deliver_message_result is not None:
            return self.deliver_message_result
        return f"stub-delivery-{self._delivery_count}"

    def stop_remote_execution(self, *, partner_id_in_remote: str, reason: str) -> None:
        self._record("stop_remote_execution", partner_id_in_remote=partner_id_in_remote, reason=reason)

    def get_permissions(self, *, partner_id_in_remote: str) -> list[str]:
        self._record("get_permissions", partner_id_in_remote=partner_id_in_remote)
        return list(self.permissions.get(partner_id_in_remote, []))

    def add_permissions(self, *, partner_id_in_remote: str, rules: list[str]) -> None:
        self._record("add_permissions", partner_id_in_remote=partner_id_in_remote, rules=list(rules))
        held = self.permissions.setdefault(partner_id_in_remote, [])
        for rule in rules:
            if rule not in held and rule not in self.permissions_refuse:
                held.append(rule)

    def delete_permissions(self, *, partner_id_in_remote: str, rules: list[str]) -> None:
        self._record(
            "delete_permissions", partner_id_in_remote=partner_id_in_remote, rules=list(rules)
        )
        held = self.permissions.setdefault(partner_id_in_remote, [])
        for rule in rules:
            if rule in held and rule not in self.permissions_refuse:
                held.remove(rule)

    def poll_completion(self, *, partner_id_in_remote: str) -> bool:
        self._record("poll_completion", partner_id_in_remote=partner_id_in_remote)
        return self.completed

    def read_remote_result(self, *, partner_id_in_remote: str) -> str:
        self._record("read_remote_result", partner_id_in_remote=partner_id_in_remote)
        return self.read_remote_result_value
