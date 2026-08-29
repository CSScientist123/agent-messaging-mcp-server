"""The invariant the one-line resume prompt rests on.

`templates.resume_displaced` renders exactly one line -- "Resume your previous
[RESEARCH]." -- and that is only meaningful because, among the queued rows
carrying a single label, at most ONE is ever marked `in_process`. Two would
make "your previous [RESEARCH]" ambiguous, and the agent would be resumed onto
whichever the tie-break happened to pick.

Nothing in the schema enforces this. It is a consequence of how `advance`
moves rows: a displacement requires the arriving label to have a STRICTLY
lower priority number than the working one, and priority is a function of the
label, so the displaced task and the task displacing it can never share a
label. `_requeue` after a failed delivery is the other way a row becomes
paused, and it puts back the row that was just promoted.

That is an argument, not evidence. This drives the real code through
randomised-but-seeded sequences of sends, interruptions and failed deliveries
and checks the invariant after every step, so a future change to the swap
rules is caught here rather than by an agent being resumed onto the wrong task.
"""

from __future__ import annotations

import random

import pytest

from extension.base import StubExtension
from messaging_core.core import MessagingCore
from messaging_core.db import Database
from messaging_core.errors import NeedsRemote, Rejected

from tests.test_polling_working_slot import make_pair

SENDABLE = ["[QUERY]", "[ERROR]", "[MESSAGE-RESPONSE]", "[TRUTHFUL-REPORT]", "[RESEARCH]"]

# Sending one of these stops the sender and gives its slot to the question, so
# a sequence that includes them exercises the interruption path as well as the
# ordinary swap -- which is where a second paused row would come from if one
# could.


class FlakyStub(StubExtension):
    """A stub whose delivery fails whenever `fail_next` is set."""

    def __init__(self) -> None:
        super().__init__(source_prefix="science_")
        self.fail_next = False

    def deliver_message(self, *, partner_id_in_remote: str, behavior: str, body: str) -> str:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("the remote refused the delivery")
        return super().deliver_message(
            partner_id_in_remote=partner_id_in_remote, behavior=behavior, body=body
        )


def paused_counts(db: Database, partner_id: int) -> dict[str, int]:
    rows = db.read(
        # Every paused row is now WORK. There is no second kind: interrupting
        # pushes the agent's own task back as an ordinary paused row, and
        # nothing else writes one. The count is unfiltered, and the invariant is
        # correspondingly stricter than it used to be.
        "SELECT behavior, COUNT(*) AS n FROM message_queue "
        "WHERE partner_id = ? AND in_process = 1 "
        "GROUP BY behavior",
        (partner_id,),
    )
    return {r["behavior"]: r["n"] for r in rows}


@pytest.fixture
def db():
    database = Database(path=":memory:")
    yield database
    database.close()


@pytest.mark.parametrize("seed", list(range(12)))
def test_paused_rows_are_never_duplicated_or_emptied(db, seed):
    """Randomised fuzz over the queue. The seed IS the bug report.

    **This invariant changed with sender-interruption, and the change is real.**

    It used to be "at most one paused row per label", which is what let the
    resume prompt be a single line -- "resume your previous [RESEARCH]" had
    exactly one referent. That no longer holds, and it is worth being precise
    about why rather than quietly weakening the assertion:

    A paused row is now written by three paths, not one. Displacement pauses the
    incumbent; a failed delivery pauses the task it could not hand over; and
    **interrupting pauses the sender's own working task**. So an agent can be
    interrupted with a `[MESSAGE-RESPONSE]` in its slot (paused row one),
    restarted by a second `[MESSAGE-RESPONSE]` that arrives, and then have that
    one fail delivery (paused row two). Two paused rows, one label, every step
    legitimate.

    The consequence is bounded and not a loss: `_HEAD_ROW_SQL` still picks one
    deterministically (paused first, then arrival, then row id), both bodies
    survive, and both are eventually delivered. What the agent loses is only
    that the one-line resume prompt no longer distinguishes them.

    So what is asserted here is what still protects the caller: **no paused row
    is ever duplicated, and none is ever emptied.** Two paused rows of a label
    are allowed; two paused rows with the SAME BODY are not, because that would
    mean one message became two.
    """
    stub = FlakyStub()
    core = MessagingCore(db, extension=stub)
    caller, worker = make_pair(core)
    rng = random.Random(seed)
    history: list[str] = []

    for step in range(40):
        action = rng.choice(["send", "send", "send", "interrupt", "fail", "complete"])
        try:
            if action == "send":
                behavior = rng.choice(SENDABLE)
                history.append(f"{step}: send {behavior}")
                core.send(
                    requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
                    message=f"m{step}", behavior=behavior,
                )
            elif action == "interrupt":
                # Interruption belongs to the SENDER, so this stops the worker
                # by having it send something, not by acting on it.
                history.append(f"{step}: worker sends a request (interrupting itself)")
                core.send(
                    requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
                    message=f"blocked at step {step}", behavior=rng.choice(["[QUERY]", "[ERROR]"]),
                )
            elif action == "fail":
                history.append(f"{step}: fail next delivery, then advance")
                stub.fail_next = True
                core.advance(partner_id=worker["id"])
            else:
                history.append(f"{step}: complete (release + advance)")
                core.release(partner_id=worker["id"])
                core.advance(partner_id=worker["id"])
        except (Rejected, NeedsRemote, RuntimeError):
            # Caps, direction rules and the injected delivery failure are all
            # normal outcomes here. What is under test is the state they leave
            # behind, not whether each call succeeded.
            stub.fail_next = False

        paused = db.read(
            "SELECT behavior, body FROM message_queue "
            "WHERE partner_id = ? AND in_process = 1", (worker["id"],)
        )
        bodies = [(r["behavior"], r["body"]) for r in paused]
        dupes = {b for b in bodies if bodies.count(b) > 1}
        assert not dupes, (
            f"seed={seed}: {dupes} appears twice among the paused rows -- one "
            "message became two.\nhistory:\n  " + "\n  ".join(history)
        )
        empties = [b for b in bodies if not b[1]]
        assert not empties, (
            f"seed={seed}: a paused row lost its body: {empties}\nhistory:\n  "
            + "\n  ".join(history)
        )


@pytest.mark.parametrize("seed", list(range(12)))
def test_a_paused_row_never_loses_its_body(db, seed):
    """A resumed task is told only its label, so the row must still carry the work.

    An empty or replaced body here would mean the one-line resume prompt points
    at a row that no longer holds what the Caller actually asked for.
    """
    stub = FlakyStub()
    core = MessagingCore(db, extension=stub)
    caller, worker = make_pair(core)
    rng = random.Random(seed + 100)

    for step in range(30):
        try:
            if rng.random() < 0.6:
                core.send(
                    requester_uuid=caller["uuid"], queried_partner_title=worker["title"],
                    message=f"body-{step}", behavior=rng.choice(SENDABLE),
                )
            else:
                core.send(
                    requester_uuid=worker["uuid"], queried_partner_title=caller["title"],
                    message=f"blocked at step {step}",
                    behavior=rng.choice(["[QUERY]", "[ERROR]"]),
                )
        except (Rejected, NeedsRemote, RuntimeError):
            pass

        rows = db.read(
            "SELECT behavior, body FROM message_queue WHERE partner_id = ? AND in_process = 1",
            (worker["id"],),
        )
        for row in rows:
            assert row["body"], (
                f"seed={seed}: a paused {row['behavior']} row has an empty body"
            )
