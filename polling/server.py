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
module calls it exactly like `send` does, so there is one implementation of
"compare the head against the working slot and act" rather than one per caller.
That was a real bug in the previous design, where the core and this module each
had their own drain.

All writes go through `Database.write` -- nothing here ever opens its own write
transaction.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Any

from messaging_core import responses
from messaging_core.core import MessagingCore
from messaging_core.errors import NeedsRemote, Rejected

from extension.base import RemoteExtension

__all__ = ["PollingServer"]

# No `logging.basicConfig` here or anywhere else in this module -- see
# `messaging_core.core`'s own logger comment for why a library never touches
# the root logger.
logger = logging.getLogger(__name__)

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
#:
#: `{detail}` is the raising extension's own `Rejected` message, carried
#: through verbatim rather than re-derived here -- the same reasoning as
#: `mcp_server.server._needs_remote_body` uses for `NeedsRemote.reason`: the
#: extension is the thing that actually read the prompt, so a second guess at
#: what it asked for, made here from a stringified exception, could only ever
#: disagree with it. `AntigravityExtension` -- the only extension that raises
#: this code today -- opens that message with two labelled lines, "Permission
#: asked: READ/WRITE" and "Path requested: <path>", precisely so this template
#: does not have to parse them back out to be scannable; see
#: `adapters.antigravity.adapter._approval_prompt_detail`.
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
        supervisor_interval: float = 1.0,
        hold_interval: float = 2.0,
    ) -> None:
        self.db = db
        self.extensions = extensions
        self.poll_interval = poll_interval
        self.supervisor_interval = supervisor_interval
        #: How long a drain thread waits between passes while the agent is
        #: waiting on its own unanswered question. Deliberately coarser than
        #: `poll_interval` -- see the wait call in `_drain_loop` for why a slow
        #: poll is safe here.
        self.hold_interval = hold_interval
        # One core, holding the slots every per-source view shares.
        self.core = core if core is not None else MessagingCore(db)

        self._lock = threading.RLock()
        self._drain_threads: dict[int, threading.Thread] = {}
        self._stop_flags: dict[int, threading.Event] = {}
        self._started = False
        # The supervisor is a single extra daemon thread, started by start()
        # and stopped by stop() alongside every drain thread; None until the
        # first start().
        self._supervisor_thread: threading.Thread | None = None
        self._supervisor_stop_event: threading.Event | None = None
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

    def _source_prefix_for(self, partner_id: int) -> str | None:
        """This Partner's `source_prefix`, or `None` if the Partner is unknown.

        A caller that only wants to test membership in `self.extensions` --
        `_ensure_thread` is the one that matters -- has no reason to go through
        `_core_for`: that also builds a whole `MessagingCore` and raises on an
        unregistered source, which is wasted work (and the wrong control flow)
        for something that only needs to look at a dict key.
        """
        row = self.db.read_one(
            "SELECT pr.source_prefix AS source_prefix "
            "FROM partners p JOIN projects pr ON pr.id = p.project_id WHERE p.id = ?",
            (partner_id,),
        )
        return row["source_prefix"] if row is not None else None

    def _core_for(self, partner_id: int) -> MessagingCore:
        """A core view whose extension speaks for this Partner's source.

        `MessagingCore` holds at most one extension, and this server holds one
        per source. The view shares this server's `db` and -- critically --
        its `slots`, so every view sees the same working slots.
        """
        prefix = self._source_prefix_for(partner_id)
        if prefix is None:
            raise Rejected("unknown_partner", f"no partner with id {partner_id}.")
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
        boundary. Resolves the uuid to an id and delegates to
        `ensure_partner_thread`, so there is exactly one implementation of
        "arm a drain thread for this partner".
        """
        partner = self.db.read_one("SELECT id FROM partners WHERE uuid = ?", (partner_uuid,))
        if partner is None:
            raise Rejected("unknown_partner", f"no partner with uuid {partner_uuid!r}.")
        return self.ensure_partner_thread(partner_id=partner["id"])

    def ensure_partner_thread(self, *, partner_id: int) -> str:
        """Arm a drain thread for `partner_id`, if this process can serve its source.

        Looks up the Partner's uuid itself rather than asking the caller for
        one: `send`'s receipt carries a `partner_id`, not a uuid, and
        `_ensure_thread` needs a label regardless of which caller reached it.
        An unknown `partner_id` is not raised here -- `_ensure_thread`'s own
        guard already turns that into a `[nothing new]` rather than an error,
        and arming is an optimisation nothing downstream depends on
        succeeding.
        """
        partner = self.db.read_one("SELECT uuid FROM partners WHERE id = ?", (partner_id,))
        label = partner["uuid"] if partner is not None else str(partner_id)
        return self._ensure_thread(partner_id, label)

    def _ensure_thread(self, partner_id: int, label: str) -> str:
        # Resolved and checked BEFORE anything is spawned or written. Two live
        # bugs this closes:
        #
        # - `_complete` calls this for the Caller after reporting back. When
        #   the Caller belongs to a source this process holds no extension
        #   for, spawning here would start a thread that raises
        #   `no_extension` on every single pass, forever -- it never retires,
        #   and the `drain_threads` row it writes survives a restart and
        #   re-arms the same doomed thread on every `start()`.
        # - `code_` Partners deliberately have no adapter in ANY process: a
        #   Claude Code session receives work through its own built-in
        #   channel, not through this system, so nothing may ever spawn a
        #   drain thread for one.
        source_prefix = self._source_prefix_for(partner_id)
        if source_prefix not in self.extensions:
            named_source = source_prefix if source_prefix is not None else "unknown"
            # DEBUG, not a WARNING: in a multi-process deployment this is the
            # expected outcome for a source this process was never meant to
            # serve, not a sign that anything is wrong.
            logger.debug(
                "declining to arm a drain thread for partner %s: source %s is not served here",
                label, named_source,
            )
            return responses.nothing_new(
                f"partner {label}'s source is {named_source!r}; this process holds no "
                "extension for it."
            )
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

        Be precise about what this row does, because it is easy to assume it
        does more. NO arming path reads it: `_ensure_thread` and `scan_once`
        de-duplicate against `self._drain_threads`, the in-process dict of live
        threads. The only reader is `start()`.

        So a row left behind by a retired thread does not strand a message --
        it makes the next `start()` spawn a thread for a Partner with nothing
        to do, which discovers as much and retires again. Deleting it here
        keeps the registry meaning what it says: these Partners had a thread
        when this process was last running.

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
                held_task = self.core.slots.get(partner_id)
                if held_task is not None and held_task.get("awaiting_resolution"):
                    # A wait is not something this thread waits to change --
                    # it is something SOMEBODY ELSE changes. The message that
                    # ends a wait is delivered by whoever answers: `send`
                    # calls `advance()` directly, which consumes the answer
                    # and delivers what comes next in the sender's own call,
                    # synchronously. So this loop is never racing to notice a
                    # resume; it is only re-checking a slot that, if it has
                    # changed at all, already changed before this wait even
                    # started. Polling it 16x/second, forever, for a
                    # partner deliberately stopped and with nothing to poll,
                    # bought nothing but wakeups.
                    #
                    # `hold_interval` is still kept small rather than backed
                    # off indefinitely, though: once the swap DOES happen,
                    # this is the only thread that will ever poll the new
                    # task for completion, and a long sleep taken right
                    # before that swap would delay noticing it.
                    stop_event.wait(self.hold_interval)
                else:
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
        except Rejected as exc:
            if exc.code != "approval_is_an_error":
                raise
            # An approval can block a DELIVERY, not only a poll. The Antigravity
            # adapter raises the same code from `deliver_message`, and that path
            # used to escape this method entirely: the blanket handler in
            # `_drain_loop` caught it, recorded it, and retried with backoff
            # FOREVER while nobody was told. `advance` has already requeued the
            # task by then, so the work survives -- but the Caller never hears,
            # which is exactly what the approval doctrine exists to prevent.
            #
            # Route it the same way a poll-time approval goes.
            task = core.working_task(partner_id=partner_id)
            remote_id = self._partner_remote_id(partner_id)
            if remote_id is not None:
                extension = self.extensions.get(self._source_prefix_for(partner_id))
                if extension is not None:
                    self._stop_quietly(extension, remote_id, "stopped on an approval prompt")
            self._raise_approval_to_caller(
                core, partner_id, task or {"caller_id": None}, exc.message
            )
            return False

        task = core.working_task(partner_id=partner_id)
        if task is None:
            depth = self.db.read_one(
                "SELECT COUNT(*) AS n FROM message_queue WHERE partner_id = ?", (partner_id,)
            )["n"]
            return depth == 0

        if task.get("awaiting_resolution"):
            # The agent asked a blocking question and is stopped until it is
            # answered. There is nothing to poll for and nothing to report: it
            # is not running, and its own remote has no idea it is waiting.
            #
            # Retiring here would be wrong -- the queue holds the work this
            # question displaced, and the answer that clears it arrives as an
            # ordinary message. So the thread waits, and each pass calls
            # `advance`, which is what notices the answer and folds it into
            # whatever the agent should do next.
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
            # `exc.message`, not `str(exc)`: the latter prefixes the code, so a
            # message the extension wrote to be read as labelled lines --
            # "Permission asked: WRITE" -- arrives as
            # "[approval_is_an_error] Permission asked: WRITE" and stops
            # scanning cleanly. The code is this branch's own condition; it
            # tells the Caller nothing it needs.
            self._raise_approval_to_caller(core, partner_id, task, exc.message)
            return False
        if not finished:
            return False

        self._complete(core, partner_id, task, extension, remote_id)
        return False

    def _record_error(self, exc: BaseException) -> None:
        """Keep a bounded record of what a drain loop swallowed.

        Logged at WARNING, not swallowed silently alongside the exception
        itself -- a daemon thread staying alive through a failure it retries
        past is correct, but a thread failing on every single pass must not
        be indistinguishable from one with nothing to do. This is the one
        logging call in this class that matters most for exactly that reason.
        """
        logger.warning("drain loop swallowed %s to stay alive: %s", type(exc).__name__, exc)
        self.last_errors.append(exc)

    def diagnostics(self) -> dict:
        """A plain, JSON-shaped snapshot of the three sinks nothing else reads.

        `last_errors`, `self.core.uncancelled_displacements`, and an
        extension's own `close_errors` are all write-only otherwise: a drain
        thread failing on every single pass looks exactly like one with
        nothing to do, because nothing surfaces what it swallowed. This is
        the one place that reads all three and hands them back in a shape an
        operator can actually use, rather than a deque of exception objects
        or a list of tuples of ints to decode by hand.

        Must never raise -- this is what an operator reaches for once
        something is ALREADY wrong, so each field below is built
        independently: a broken one degrades to empty rather than taking the
        others down with it and leaving the operator with nothing at all.
        """
        try:
            last_errors = [str(exc) for exc in self.last_errors]
        except BaseException:
            last_errors = []
        try:
            last_error_count = len(self.last_errors)
        except BaseException:
            last_error_count = 0

        try:
            uncancelled_displacements = [
                {
                    "partner_id": partner_id,
                    "displaced_behavior": displaced_behavior,
                    "arriving_behavior": arriving_behavior,
                }
                for partner_id, displaced_behavior, arriving_behavior in (
                    self.core.uncancelled_displacements
                )
            ]
        except BaseException:
            uncancelled_displacements = []

        # `close_errors` is an Antigravity implementation detail, not part of
        # the `RemoteExtension` contract -- most extensions never have it, so
        # a source is only ever added here when `getattr` actually finds one.
        extension_errors: dict[str, list[str]] = {}
        for source_prefix, extension in self.extensions.items():
            try:
                close_errors = getattr(extension, "close_errors", None)
                if close_errors is None:
                    continue
                extension_errors[source_prefix] = [str(exc) for exc in close_errors]
            except BaseException:
                continue

        return {
            "last_errors": last_errors,
            "last_error_count": last_error_count,
            "uncancelled_displacements": uncancelled_displacements,
            "extension_errors": extension_errors,
        }

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
        goes to the front of everything below it and displaces a task already
        running unless that task outranks it or ties. That displacement IS the
        interruption; no separate mechanism is needed.

        Note the tie: displacement needs a STRICTLY lower priority number, so a
        Caller already working a `[QUERY]`, another `[ERROR]`, or a summary
        finishes that first and takes this next. It is the front of the queue,
        not an unconditional pre-emption, and describing it as the latter would
        promise a latency this does not provide.
        """
        # PARK, do not destroy.
        #
        # This used to call `core.release`, which pops the slot and discards what
        # it held. The partner's in-flight work was silently lost: nothing
        # requeued it, and for a [RESEARCH] the request text was not in `messages`
        # either, so the Caller was told "send the work again" without being shown
        # what the work was.
        #
        # Parking is what an agent that raises an [ERROR] itself already gets --
        # its task pushed back paused and its slot held empty until an answer
        # arrives. An approval detected by the Polling Server is the same
        # situation reached from the outside, so it gets the same treatment, and
        # the answer then resolves it through the same path.
        parked = core.park_for_approval(partner_id=partner_id)
        caller_id = (parked or task)["caller_id"]
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

    # -- supervisor -------------------------------------------------------------

    def scan_once(self) -> int:
        """Arm a drain thread for every Partner of this process's own sources that has queued work.

        This is the mechanism that lets a message sent by ANY process end up
        drained by the process that actually owns the target's remote:
        `send`'s own best-effort arm only fires in the sender's process, and a
        push notification only fires when the remote itself calls back.
        Neither covers a target this process didn't just interact with --
        without this scan, a message queued from elsewhere would sit forever
        with no thread and nothing to arm one.

        Returns:
            How many threads were newly armed.
        """
        if not self.extensions:
            # `IN ()` is invalid SQL, not a query that matches zero rows, and
            # a process holding no extension at all can serve nothing anyway.
            return 0
        placeholders = ", ".join("?" for _ in self.extensions)
        rows = self.db.read(
            "SELECT DISTINCT p.id AS id, p.uuid AS uuid "
            "FROM partners p "
            "JOIN projects pr ON pr.id = p.project_id "
            "JOIN message_queue q ON q.partner_id = p.id "
            f"WHERE p.archived_at IS NULL AND pr.source_prefix IN ({placeholders})",
            tuple(self.extensions),
        )
        candidates = {row["id"]: row["uuid"] for row in rows}

        # A queued row is not the only way a Partner can be left unattended.
        # A same-process `send` DELETES the row as it promotes the task into
        # the working slot, so a Partner whose remote is mid-turn has an EMPTY
        # queue and an occupied slot -- and if the arm that should have
        # followed that send did not happen (it is deliberately best-effort,
        # and a thread can also die), a queue-only scan would look straight
        # past a remote that is working with nobody watching it.
        #
        # An agent WAITING on its own question is the exception. Its remote was
        # stopped when it asked, so there is no turn to harvest and no
        # completion to notice -- a thread armed for it would poll a halted
        # session forever. What ends the wait is the answer, and an answer is a
        # queued row: it arms this partner through the branch above, in
        # whichever process owns the remote.
        for partner_id in self.core.slots.occupied():
            task = self.core.slots.get(partner_id)
            if task is not None and task.get("awaiting_resolution"):
                continue
            candidates.setdefault(partner_id, str(partner_id))

        armed = 0
        for partner_id, label in candidates.items():
            with self._lock:
                existing = self._drain_threads.get(partner_id)
                if existing is not None and existing.is_alive():
                    continue
            self._ensure_thread(partner_id, label)
            # Whether a thread was actually spawned is read off the registry,
            # never off the response text -- and it genuinely has to be asked:
            # `_ensure_thread` declines outright for a source this process
            # holds no extension for, and counting that as an arm would report
            # coverage this process does not have.
            with self._lock:
                spawned = self._drain_threads.get(partner_id)
            if spawned is not None and spawned is not existing:
                armed += 1
        return armed

    def _supervisor_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                self.scan_once()
            except BaseException as exc:  # noqa: BLE001 - keep the daemon alive
                # One failed scan must not be the last scan: every Partner
                # this process could have picked up on the next pass would be
                # left stranded right alongside the one that actually failed.
                self._record_error(exc)
            stop_event.wait(self.supervisor_interval)

    # -- lifecycle --------------------------------------------------------------

    def start(self) -> None:
        """Resume this process's own drain threads and start the supervisor.

        Safe to call once at process start-up to pick back up after a restart.
        A `drain_threads` row surviving a restart means that Partner had work
        in flight; `notify_partner_push` remains the way new Partners get a
        thread during normal operation, and the supervisor thread started
        here is what picks up work this process was never pushed to at all.

        Only rows for Partners of this process's own sources are resumed. A
        row for another source is left in place, untouched -- that Partner's
        OWN process is what owns it, and its own `start()` is what needs to
        find the row still there; deleting it here would strand exactly the
        work it exists to protect.
        """
        with self._lock:
            if self._started:
                return
            self._started = True
            if self.extensions:
                placeholders = ", ".join("?" for _ in self.extensions)
                rows = self.db.read(
                    "SELECT dt.partner_id AS partner_id "
                    "FROM drain_threads dt "
                    "JOIN partners p ON p.id = dt.partner_id "
                    "JOIN projects pr ON pr.id = p.project_id "
                    f"WHERE pr.source_prefix IN ({placeholders})",
                    tuple(self.extensions),
                )
                for row in rows:
                    partner_id = row["partner_id"]
                    existing = self._drain_threads.get(partner_id)
                    if existing is None or not existing.is_alive():
                        self._spawn_drain_thread(partner_id)

            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._supervisor_loop,
                args=(stop_event,),
                name="drain-supervisor",
                daemon=True,
            )
            self._supervisor_stop_event = stop_event
            self._supervisor_thread = thread
            thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal every drain thread and the supervisor to stop, and join them all.

        Safe to call twice, and safe when `start()` was never called.
        """
        with self._lock:
            events = list(self._stop_flags.values())
            threads = list(self._drain_threads.values())
            supervisor_stop_event = self._supervisor_stop_event
            supervisor_thread = self._supervisor_thread
            self._started = False
            self._supervisor_stop_event = None
            self._supervisor_thread = None

        for event in events:
            event.set()
        if supervisor_stop_event is not None:
            supervisor_stop_event.set()
        for thread in threads:
            # `ident` is None until a thread has actually been started, and
            # join() on one raises. _spawn_drain_thread registers the thread
            # before starting it, so a start() that fails leaves an unstarted
            # thread in the map -- and stop(), which is documented as always
            # safe to call, would then be the thing that crashed.
            if thread.ident is not None:
                thread.join(timeout=timeout)
        if supervisor_thread is not None and supervisor_thread.ident is not None:
            # Same reasoning as the drain threads above: start() creates the
            # Thread object before calling thread.start() on it, so a start()
            # that raised partway through would otherwise make this the thing
            # that crashed instead.
            supervisor_thread.join(timeout=timeout)
        with self._lock:
            self._drain_threads.clear()
            self._stop_flags.clear()
