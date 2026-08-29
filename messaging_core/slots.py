"""The working slot: the one poll task a partner is actually being worked on.

Every other piece of queue state is in SQLite. This one is not, and that is a
decision rather than an omission.

A working slot is process state. It changes on every swap, it is meaningless
to a second process (only the Polling Server that owns the drain thread can
act on it), and it does not survive a restart -- because the remote's own turn
does not survive one either. Persisting it would put a row in the database
that a reader would reasonably take for durable truth, and that reader would
be wrong in exactly the situation where being wrong is expensive: after a
crash, when the row says a partner is mid-task and no thread is driving it.

So it is here, in memory, and the schema comment on `message_queue` says so.

Locking is per partner, not global, and the lock is public because callers
outside this class have to hold it.

Two operations need it. Deciding whether a caller is at its cap means counting
queued rows AND asking whether the working slot holds one more of the same
kind; a swap landing between those two questions would have the count taken
against a slot that no longer exists. And a swap itself spans a database write
and a remote call, which must not interleave with a second swap on the same
partner.

Per partner rather than one global lock, because a swap holds its lock across
remote I/O -- a network round trip to Antigravity or Claude Science. Under a
single lock every drain thread in the process would queue behind whichever
partner's remote was slowest, which is the opposite of why the drain threads
exist.
"""

from __future__ import annotations

import threading
from typing import Any


class WorkingSlots:
    """Per-partner working slots, keyed by `partners.id`.

    A slot holds the same shape of dict a queue row is popped into:
    ``{"id", "partner_id", "caller_id", "behavior", "body", "in_process",
    "message_id", "enqueued_at", "summary_phase", "origin_behavior"}``. ``id``
    is the `message_queue` row id the task came from; the row itself is gone,
    deleted when the task was promoted, so the id is a provenance breadcrumb
    rather than a live key. ``summary_phase`` and ``origin_behavior`` are
    usually absent -- `begin_summary_phase` is what adds them, in place, to
    mark a `[RESEARCH]` task that has been relabelled `[TRUTHFUL-REPORT]` and
    still owes its Caller the report.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[int, threading.RLock] = {}
        self._slots: dict[int, dict[str, Any]] = {}
        # Partners under a FORCED interruption: they sent a request and cannot
        # proceed until it is answered. Their slot is empty and must STAY empty
        # -- this set is what says "empty on purpose" rather than "idle", and it
        # is the difference between a partner waiting for an answer and one
        # waiting for work.
        #
        # In memory for the same reason the slots are: it describes a turn that
        # does not survive a restart, and a row claiming an agent is mid-wait
        # after a crash would be a lie a reader could not detect.
        self._forced: set[int] = set()

    def lock_for(self, partner_id: int) -> threading.RLock:
        """The lock guarding this partner's slot. Re-entrant, and safe to hold across I/O."""
        with self._guard:
            lock = self._locks.get(partner_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[partner_id] = lock
            return lock

    def get(self, partner_id: int) -> dict[str, Any] | None:
        """Return the task currently being worked for `partner_id`, or None."""
        with self._guard:
            return self._slots.get(partner_id)

    def set(self, partner_id: int, task: dict[str, Any]) -> None:
        """Put `task` in the slot, replacing whatever was there."""
        with self._guard:
            self._slots[partner_id] = task

    def clear(self, partner_id: int) -> dict[str, Any] | None:
        """Empty the slot and return what it held, or None if it was empty."""
        with self._guard:
            return self._slots.pop(partner_id, None)

    def force(self, partner_id: int) -> None:
        """Begin a forced interruption: the slot is empty and stays empty."""
        with self._guard:
            self._forced.add(partner_id)

    def release_force(self, partner_id: int) -> bool:
        """End one. Returns whether a forced interruption was actually open."""
        with self._guard:
            was = partner_id in self._forced
            self._forced.discard(partner_id)
            return was

    def is_forced(self, partner_id: int) -> bool:
        """Whether this partner is waiting on an answer to its own request."""
        with self._guard:
            return partner_id in self._forced

    def forced(self) -> list[int]:
        """Every partner currently under a forced interruption."""
        with self._guard:
            return list(self._forced)

    def occupied(self) -> list[int]:
        """Partner ids that currently hold a working task."""
        with self._guard:
            return list(self._slots)

    def outstanding(self, partner_id: int, caller_id: int, behavior: str) -> int:
        """1 if the working slot holds this caller's task counting against this label, else 0.

        This is the term a cap check adds to its count of queued rows. A cap
        limits work in flight, not work waiting: a caller allowed three
        `[QUERY]` tasks against a partner has three including the one the
        partner is answering right now, otherwise the fourth arrives the
        moment the third starts and the cap means one more than it says.

        A match on the current `behavior` OR a recorded `origin_behavior` --
        not `behavior` alone -- because `begin_summary_phase` relabels a
        `[RESEARCH]` task to `[TRUTHFUL-REPORT]` in place, in this same slot,
        without the task ever leaving it. If the label check followed
        `behavior` only, the relabelling itself would silently free a
        `[RESEARCH]` cap slot for as long as the summary took to write, and
        the same Caller could admit one more `[RESEARCH]` than the cap
        allows. It is still the same delegated work under a second
        instruction, so it must still count.
        """
        with self._guard:
            task = self._slots.get(partner_id)
            if task is None:
                return 0
            if task["caller_id"] != caller_id:
                return 0
            if task["behavior"] == behavior or task.get("origin_behavior") == behavior:
                return 1
            return 0
