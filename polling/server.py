"""The Polling Server: polls remotes on agents' behalf so that no agent ever polls.

An agent hands a message to its partner and moves on. Something still has to
watch the remote until it finishes, notice when it gets stuck, and hand the
result back. That something is this module. Everything it knows about a
specific remote (NotebookLM, Claude Science, Antigravity, ...) comes through a
`RemoteExtension` (see `extension.base`) -- this module never talks to a remote
directly.

What is here is one thing: **the drain thread.** One daemon thread per Partner,
which repeatedly asks `MessagingCore.advance` to make progress, waits for the
remote to finish whatever ended up in the working slot, and pushes the answer
into the Caller's queue.

There is deliberately no state machine any more. The old five-state
`polling_tasks` table encoded where a task was, and it could disagree with
where the task actually was -- the queue and the working slot already answer
that question, and they answer it in one place. What remains of "state" is:
a task is queued, or it holds the working slot, or it is neither.

The swap logic itself is NOT here. `MessagingCore.advance` owns it, and this
module calls it exactly like `send` and `interrupt_partner` do, so there is one
implementation of "compare the head against the working slot and act" rather
than one per caller. That was a real bug in the previous design, where the core
and this module each had their own drain.

All writes go through `Database.write` -- nothing here ever opens its own write
transaction.
"""

from __future__ import annotations

import collections
import threading
import time
from typing import Any

from messaging_core import responses
from messaging_core.core import MessagingCore
from messaging_core.errors import NeedsRemote, Rejected
from messaging_core.labels import INTERRUPT_BEHAVIOR

from extension.base import RemoteExtension

__all__ = ["PollingServer"]

# What a remote is assumed to have produced when it has no way to be read from.
# Only NotebookLM implements `read_remote_result`; every other remote executes
# and reports by acting through its own tools. The placeholder keeps the
# bookkeeping deterministic instead of leaving a queue push half-made.
NO_READABLE_RESULT = "[result reported by the remote through its own channel]"

#: How many swallowed exceptions a server keeps. See `PollingServer.last_errors`.
MAX_RECORDED_ERRORS = 200

#: What a Caller is told when its Partner stops on a permission it does not hold.
#:
#: Deliberately short, and deliberately prescriptive. An approval prompt is not a
#: question anybody answers -- it means the grant was missing before the work
#: started -- so the message names the conversation, names what was asked for,
#: and names the two capabilities that fix it. A Caller handed a full incident
#: report starts debugging instead of granting.
APPROVAL_ERROR_TEMPLATE = """\
An approval was requested by {title}. This is unexpected: an approval means a
permission was missing before the work started, and nothing in this system
answers one.

Conversation: {remote_id}
What it asked for:
{detail}

How to resolve it:

- Call get_permissions for {title} to see what that conversation currently allows.
- Call add_permissions (or delete_permissions) so the set covers the work you
  asked for. Write paths must include files that do not exist yet.
- Then send the work again. Correcting the grant and sending again IS the
  resumption; there is nothing else to resume.
"""


class PollingServer:
    """Polls remotes on behalf of every agent, so agents never have to.

    `extensions` maps a `source_prefix` (`"nlm_"`, `"code_"`, `"science_"`,
    `"gemini_"`) to the `RemoteExtension` that speaks for that family of
    remote. `poll_interval` is how long a drain thread waits between passes.

    The server and the core share one `WorkingSlots`: the server constructs a
    per-source core around the same slots object it was given, because a
    working slot that two objects each keep their own copy of is not a slot.
    """

    def __init__(
        self,
        db,
        extensions: dict[str, RemoteExtension],
        poll_interval: float = 0.25,
        core: MessagingCore | None = None,
    ) -> None:
        self.db = db
        self.extensions = extensions
        self.poll_interval = poll_interval
        # One core, holding the slots every per-source view shares.
        self.core = core if core is not None else MessagingCore(db)

        self._lock = threading.RLock()
        self._drain_threads: dict[int, threading.Thread] = {}
        self._stop_flags: dict[int, threading.Event] = {}
        self._started = False
        # Exceptions swallowed by a drain loop so they don't kill the daemon
        # thread silently. Not part of the public contract; useful for tests
        # and diagnostics.
        #
        # BOUNDED. A repeating failure appends once per poll interval, and an
        # unbounded list is a slow memory leak that only shows up in exactly the
        # situation where something is already wrong. The newest entries are the
        # ones worth keeping, so the oldest are dropped.
        self.last_errors: collections.deque[BaseException] = collections.deque(
            maxlen=MAX_RECORDED_ERRORS
        )
        #: Longest a drain thread will wait between retries after repeated
        #: failures.
        self.max_backoff = max(poll_interval, 30.0)

    # -- the core, bound to one partner's remote ------------------------------

    def _core_for(self, partner_id: int) -> MessagingCore:
        """A core view whose extension speaks for this Partner's source.

        `MessagingCore` holds at most one extension, and this server holds one
        per source. The view shares this server's `db` and -- critically --
        its `slots`, so every view sees the same working slots.
        """
        row = self.db.read_one(
            "SELECT pr.source_prefix AS source_prefix "
            "FROM partners p JOIN projects pr ON pr.id = p.project_id WHERE p.id = ?",
            (partner_id,),
        )
        if row is None:
            raise Rejected("unknown_partner", f"no partner with id {partner_id}.")
        prefix = row["source_prefix"]
        extension = self.extensions.get(prefix)
        if extension is None:
            raise Rejected("no_extension", f"no RemoteExtension registered for {prefix!r}.")
        return MessagingCore(self.db, extension=extension, slots=self.core.slots)

    def _extension_for_partner(self, partner_id: int) -> RemoteExtension:
        return self._core_for(partner_id).extension

    def _partner_remote_id(self, partner_id: int) -> str:
        row = self.db.read_one(
            "SELECT partner_id_in_remote FROM partners WHERE id = ?", (partner_id,)
        )
        if row is None:
            raise Rejected("unknown_partner", f"no partner with id {partner_id}.")
        return row["partner_id_in_remote"]

    # -- draining -------------------------------------------------------------

    def notify_partner_push(self, *, partner_uuid: str) -> str:
        """Ensure a drain thread is running for the Partner named by `partner_uuid`.

        Takes a UUID, never a title -- titles only ever appear at the client
        boundary. If a drain thread already serves this Partner, this is a
        no-op that returns a `[nothing new]` response; otherwise a thread is
        spawned and an `[ok]` response is returned.
        """
        partner = self.db.read_one("SELECT id FROM partners WHERE uuid = ?", (partner_uuid,))
        if partner is None:
            raise Rejected("unknown_partner", f"no partner with uuid {partner_uuid!r}.")
        return self._ensure_thread(partner["id"], partner_uuid)

    def _ensure_thread(self, partner_id: int, label: str) -> str:
        with self._lock:
            existing = self._drain_threads.get(partner_id)
            if existing is not None and existing.is_alive():
                return responses.nothing_new(
                    f"a drain thread is already serving partner {label}."
                )
            self._spawn_drain_thread(partner_id)
        return responses.ok(f"spawned a drain thread for partner {label}.")

    def _spawn_drain_thread(self, partner_id: int) -> None:
        """Start (or restart) the drain thread for `partner_id`. Caller holds `self._lock`."""
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._drain_loop,
            args=(partner_id, stop_event),
            name=f"drain-{partner_id}",
            daemon=True,
        )
        self._stop_flags[partner_id] = stop_event
        self._drain_threads[partner_id] = thread

        def _register(conn):
            conn.execute(
                "INSERT INTO drain_threads(partner_id, thread_id) VALUES (?, ?) "
                "ON CONFLICT(partner_id) DO UPDATE SET "
                "thread_id = excluded.thread_id, "
                "started_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                (partner_id, thread.name),
            )

        self.db.write(_register)
        thread.start()

    def _deregister(self, partner_id: int) -> None:
        """Remove this Partner's `drain_threads` row, and forget the thread.

        The row exists so that a push arriving while a thread is already
        running does not spawn a second one. Once the thread has no work left
        and exits, the row is a claim that a thread is running when none is --
        and the next push would read it, believe a thread already has the
        work, and spawn nothing. That is a liveness bug rather than a cosmetic
        one: the message sits in the queue and nothing ever picks it up.

        Deliberately NOT called on shutdown. `stop()` signals threads for a
        process that is going away with work possibly still queued, and the
        row is what `start()` uses to bring that Partner's thread back. A row
        deleted at shutdown would strand exactly the work it exists to
        protect.
        """
        self.db.write(
            lambda conn: conn.execute(
                "DELETE FROM drain_threads WHERE partner_id = ?", (partner_id,)
            )
        )
        with self._lock:
            # Only if it is still THIS thread's registration: a push that
            # arrived while this one was retiring may already have spawned a
            # replacement, and popping that one would leave a live thread no
            # caller can find.
            current = threading.current_thread()
            if self._drain_threads.get(partner_id) is current:
                self._drain_threads.pop(partner_id, None)
                self._stop_flags.pop(partner_id, None)

    def _has_work(self, partner_id: int) -> bool:
        """Anything queued, or anything in the working slot."""
        if self.core.working_task(partner_id=partner_id) is not None:
            return True
        return bool(
            self.db.read_one(
                "SELECT 1 AS ok FROM message_queue WHERE partner_id = ? LIMIT 1", (partner_id,)
            )
        )

    def _drain_loop(self, partner_id: int, stop_event: threading.Event) -> None:
        retire = True
        consecutive_failures = 0
        try:
            while not stop_event.is_set():
                try:
                    idle = self.drain_once(partner_id=partner_id)
                except BaseException as exc:  # noqa: BLE001 - keep the daemon alive
                    self._record_error(exc)
                    consecutive_failures += 1
                    # Back off. A failure that repeats is usually one that will
                    # keep repeating -- an unreachable session, a refusing
                    # remote -- and polling it at full rate buys nothing while
                    # costing a request per interval. Capped so a transient
                    # failure still recovers promptly.
                    delay = min(self.poll_interval * (2 ** min(consecutive_failures, 6)),
                                self.max_backoff)
                    stop_event.wait(delay)
                    continue
                consecutive_failures = 0
                if idle:
                    # Nothing queued and nothing working, so this thread should
                    # retire -- but the decision has to be made under the SAME
                    # lock `notify_partner_push` decides under, and re-checked
                    # after taking it.
                    #
                    # The race it closes, reproduced rather than theorised: a
                    # message is admitted and a push arrives in the window between
                    # this loop deciding it is idle and the thread actually
                    # exiting. `_ensure_thread` sees this thread still ALIVE,
                    # reports "nothing new" and spawns nothing; this thread then
                    # exits and deletes its row. The message is left queued with
                    # no thread and no row, and nothing will ever pick it up --
                    # exactly the liveness failure `drain_threads` exists to
                    # prevent, arriving by the other door.
                    #
                    # Holding the lock across the re-check makes the two mutually
                    # exclusive: either the push gets in first and this sees its
                    # work, or this retires first and the push finds no live
                    # thread and spawns one.
                    with self._lock:
                        if not self._has_work(partner_id):
                            self._deregister(partner_id)
                            retire = False
                            return
                    continue
                stop_event.wait(max(self.poll_interval / 4, 0.0))
            # Left because stop() asked, not because the work ran out.
            retire = False
        finally:
            if retire:
                self._deregister(partner_id)

    def drain_once(self, *, partner_id: int) -> bool:
        """Make one pass: advance the queue, then service the working slot.

        Returns:
            True when there is nothing queued and nothing working, which is
            the drain thread's signal to retire.
        """
        core = self._core_for(partner_id)
        try:
            core.advance(partner_id=partner_id)
        except NeedsRemote as exc:
            # The queue is untouched -- advance resolves the extension before
            # it writes anything -- so this is worth recording and retrying
            # rather than treating as a lost message.
            self._record_error(exc)

        task = core.working_task(partner_id=partner_id)
        if task is None:
            depth = self.db.read_one(
                "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id = ?", (partner_id,)
            )["n"]
            return depth == 0

        if task["behavior"] == INTERRUPT_BEHAVIOR:
            # An [IDLE] is a hold, not work. There is nothing to poll for and
            # nothing to report: the partner is stopped, and it stays stopped
            # until something arrives that displaces the hold. Retiring here
            # would be wrong -- the queue may hold the paused task this
            # interruption displaced -- so the thread waits instead.
            return False

        extension = core.extension
        remote_id = self._partner_remote_id(partner_id)
        try:
            finished = self._is_complete(extension, remote_id)
        except Rejected as exc:
            if exc.code != "approval_is_an_error":
                raise
            # The Partner is stopped on a permission it does not hold. It is
            # neither busy nor finished, and no amount of polling changes that --
            # so this is reported to the Caller rather than retried. Without this
            # branch the exception escapes to the drain loop, which records it and
            # polls again forever: the slot stays held, the thread never retires,
            # and nobody is ever told.
            self._stop_quietly(extension, remote_id, "stopped on an approval prompt")
            self._raise_approval_to_caller(core, partner_id, task, str(exc))
            return False
        if not finished:
            return False

        self._complete(core, partner_id, task, extension, remote_id)
        return False

    def _record_error(self, exc: BaseException) -> None:
        """Keep a bounded record of what a drain loop swallowed."""
        self.last_errors.append(exc)

    def _stop_quietly(self, extension: RemoteExtension, remote_id: str, reason: str) -> None:
        """Stop the remote, tolerating a remote that cannot be stopped.

        Not every remote can be cancelled, and one that cannot must not turn a
        report into a failure. The report is the point; the stop is hygiene.
        """
        try:
            extension.stop_remote_execution(partner_id_in_remote=remote_id, reason=reason)
        except (Rejected, NeedsRemote) as exc:
            self._record_error(exc)

    def _is_complete(self, extension: RemoteExtension, remote_id: str) -> bool:
        """Whether the remote has finished its turn.

        A remote with no notion of turn completion raises `NeedsRemote`; that
        is treated as "finished", because a remote that cannot be asked has
        already given the only answer it has. The alternative -- polling
        forever -- would hold the working slot against a partner that is
        perfectly free.
        """
        try:
            return extension.poll_completion(partner_id_in_remote=remote_id)
        except NeedsRemote:
            return True

    def _raise_approval_to_caller(
        self, core: MessagingCore, partner_id: int, task: dict[str, Any], detail: str
    ) -> None:
        """Tell the Caller its Partner is stopped on a permission it does not hold.

        A Partner blocked on an approval is not busy and is not finished -- it is
        waiting for something no amount of polling will produce. Left alone it
        holds the working slot indefinitely and nobody is told, because the
        Partner cannot report it (an agent stopped on a prompt is not running)
        and the Caller has no reason to look.

        So the Polling Server reports it, on the Partner's behalf. The slot is
        released, the Partner's remote is stopped so it is not left sitting on
        the prompt, and an `[ERROR]` naming the conversation and the missing
        permission is pushed into the Caller's queue -- where, at priority 2, it
        displaces whatever the Caller was doing. That displacement IS the
        interruption; no separate mechanism is needed.
        """
        released = core.release(partner_id=partner_id)
        caller_id = (released or task)["caller_id"]
        partner = self.db.read_one(
            "SELECT title, partner_id_in_remote FROM partners WHERE id = ?", (partner_id,)
        )
        body = APPROVAL_ERROR_TEMPLATE.format(
            title=partner["title"] if partner else f"partner {partner_id}",
            remote_id=partner["partner_id_in_remote"] if partner else "unknown",
            detail=detail.strip(),
        )
        core.report_back(
            to_partner_id=caller_id,
            from_partner_id=partner_id,
            behavior="[ERROR]",
            body=body,
        )
        caller = self.db.read_one(
            "SELECT uuid FROM partners WHERE id = ? AND archived_at IS NULL", (caller_id,)
        )
        if caller is not None:
            self._ensure_thread(caller_id, caller["uuid"])

    def _read_result(self, extension: RemoteExtension, remote_id: str) -> str:
        """Best-effort fetch of what a finished turn produced."""
        try:
            return extension.read_remote_result(partner_id_in_remote=remote_id)
        except NeedsRemote:
            return NO_READABLE_RESULT

    def _complete(
        self,
        core: MessagingCore,
        partner_id: int,
        task: dict[str, Any],
        extension: RemoteExtension,
        remote_id: str,
    ) -> None:
        """Close out a finished working task: summarize if owed, reply if owed, release.

        A `[RESEARCH]` task is not finished when the work stops. It owes a
        summary, and the summary is a second exchange against the same remote
        inside the same working slot -- see
        `MessagingCore.begin_summary_phase`. Only after that does anything go
        back to the Caller.
        """
        if task["behavior"] == "[RESEARCH]":
            prompt = core.begin_summary_phase(partner_id=partner_id)
            if prompt is not None:
                try:
                    extension.deliver_message(
                        partner_id_in_remote=remote_id,
                        behavior="[TRUTHFUL-REPORT]",
                        body=prompt,
                    )
                except BaseException:
                    # The summary request never reached the remote, and the slot
                    # is already relabelled [TRUTHFUL-REPORT] by
                    # begin_summary_phase. Left alone the Partner holds a task
                    # nobody asked it to do, forever: it is not working, so
                    # poll_completion says finished, and the next pass reports a
                    # summary that was never requested.
                    #
                    # So the slot is released and the work is handed back to the
                    # Caller as an [ERROR] naming what failed. The research
                    # itself is done -- only the summary is lost -- and that is
                    # what the Caller needs to know.
                    released = core.release(partner_id=partner_id)
                    if released is not None:
                        core.report_back(
                            to_partner_id=released["caller_id"],
                            from_partner_id=partner_id,
                            behavior="[ERROR]",
                            body=(
                                "The work finished but its summary could not be requested; "
                                "the remote did not accept the request. The result is in the "
                                "partner's own transcript. Send it a [TRUTHFUL-REPORT] to ask "
                                "again."
                            ),
                        )
                    raise
                # The next pass polls for the summary. The slot now reads
                # [TRUTHFUL-REPORT], so only a forced interruption can take it.
                return

        reply = core.reply_behavior(task["behavior"])
        if task.get("summary_phase"):
            # The second phase of a research task. begin_summary_phase set this
            # marker; the LABEL alone cannot say it, because a [TRUTHFUL-REPORT]
            # can equally be one an agent sent directly -- and that one owes
            # nothing back, because it already IS the report.
            #
            # Inferring it from the label was a real bug: a directly-sent
            # [TRUTHFUL-REPORT] replied with another [TRUTHFUL-REPORT], whose
            # completion replied again, each hop spawning a fresh drain thread.
            # That is the unbounded exchange label_caps.reply_behavior IS NULL
            # exists to prevent, reintroduced by a special case.
            reply = "[TRUTHFUL-REPORT]"

        # Read BEFORE releasing. The slot is what stops another task being
        # promoted and delivered to this same remote, so releasing first opens a
        # window where the next turn can start against a remote whose previous
        # output has not been fetched -- and what comes back then belongs to
        # neither turn.
        body = self._read_result(extension, remote_id) if reply is not None else None

        released = core.release(partner_id=partner_id)
        if reply is None or released is None:
            # [ERROR], [MESSAGE-RESPONSE] and [TRUTHFUL-REPORT] arriving as
            # deliveries are answers already. Replying to an answer is how two
            # agents talk to each other until one of them is archived.
            return
        core.report_back(
            to_partner_id=released["caller_id"],
            from_partner_id=partner_id,
            behavior=reply,
            body=body,
        )
        caller = self.db.read_one(
            "SELECT uuid FROM partners WHERE id = ? AND archived_at IS NULL",
            (released["caller_id"],),
        )
        if caller is not None:
            # The Caller now has something queued; it needs a thread of its
            # own to pick it up. Without this the answer waits until the
            # Caller happens to be pushed to for some other reason.
            self._ensure_thread(released["caller_id"], caller["uuid"])

    # -- lifecycle --------------------------------------------------------------

    def start(self) -> None:
        """Resume a drain thread for every Partner still registered in `drain_threads`.

        Safe to call once at process start-up to pick back up after a restart.
        A row surviving a restart means that Partner had work in flight;
        `notify_partner_push` remains the way new Partners get a thread during
        normal operation.
        """
        with self._lock:
            if self._started:
                return
            self._started = True
            rows = self.db.read("SELECT partner_id FROM drain_threads", ())
            for row in rows:
                partner_id = row["partner_id"]
                existing = self._drain_threads.get(partner_id)
                if existing is None or not existing.is_alive():
                    self._spawn_drain_thread(partner_id)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal every drain thread to stop and join them all. Safe to call twice."""
        with self._lock:
            events = list(self._stop_flags.values())
            threads = list(self._drain_threads.values())
            self._started = False

        for event in events:
            event.set()
        for thread in threads:
            # `ident` is None until a thread has actually been started, and
            # join() on one raises. _spawn_drain_thread registers the thread
            # before starting it, so a start() that fails leaves an unstarted
            # thread in the map -- and stop(), which is documented as always
            # safe to call, would then be the thing that crashed.
            if thread.ident is not None:
                thread.join(timeout=timeout)
        with self._lock:
            self._drain_threads.clear()
            self._stop_flags.clear()
