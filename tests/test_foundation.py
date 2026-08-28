"""Tests for the messaging_core foundation modules: errors, labels, config, db,
responses, slots, templates.

Covers, at minimum:
- assert_native_filesystem rejecting a fabricated 9p mount and accepting ext4,
  using fake /proc/mounts content so nothing depends on the real machine.
- Database.write really serialising concurrent writers.
- an exception inside write(fn) propagating to the caller and rolling back.
- foreign_keys=ON being active on both a read connection and the writer connection.
- the single-writer invariant: read() cannot write, on disk or in :memory:, and
  that a caller cannot undo it by passing "PRAGMA query_only = OFF" to read().
- labels.validate_behavior accepting every recognized label and refusing an
  unknown one.
- WorkingSlots: per-partner identity of lock_for, re-entrancy of that lock,
  outstanding() requiring both caller and behavior to match, and clear()
  returning what it held then None.
- templates: rendering behaviour (paths included/excluded, behavior named,
  original request quoted verbatim) rather than exact wording.
- the response helpers' marker/"nothing changed" shapes.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from messaging_core import config as config_module
from messaging_core import responses
from messaging_core import templates
from messaging_core.db import Database
from messaging_core.errors import MessagingError, NeedsRemote, Rejected
from messaging_core.labels import BEHAVIORS, BLOCKING_BEHAVIORS, validate_behavior
from messaging_core.slots import WorkingSlots

# A small standalone schema used only by these tests, kept independent of the
# real messaging schema so failures here point at messaging_core.db itself
# rather than at schema/schema.sql.
TEST_SCHEMA = """
CREATE TABLE parent (
    id   INTEGER PRIMARY KEY
);

CREATE TABLE child (
    id        INTEGER PRIMARY KEY,
    parent_id INTEGER NOT NULL REFERENCES parent(id)
);

CREATE TABLE counter (
    id    INTEGER PRIMARY KEY,
    value INTEGER NOT NULL
);
INSERT INTO counter (id, value) VALUES (1, 0);
"""


@pytest.fixture
def schema_file(tmp_path: Path) -> Path:
    p = tmp_path / "test_schema.sql"
    p.write_text(TEST_SCHEMA)
    return p


@pytest.fixture
def file_db(tmp_path: Path, schema_file: Path):
    db = Database(path=tmp_path / "test.sqlite3", schema=schema_file)
    yield db
    db.close()


@pytest.fixture
def memory_db(schema_file: Path):
    db = Database(path=":memory:", schema=schema_file)
    yield db
    db.close()


# ---------------------------------------------------------------------------
# config.assert_native_filesystem
# ---------------------------------------------------------------------------

FAKE_MOUNTS = (
    "/dev/sdd / ext4 rw,relatime,discard,errors=remount-ro,data=ordered 0 0\n"
    "C:\\ /mnt/c 9p rw,dirsync,aname=drvfs;path=C:\\;uid=1000;gid=1000 0 0\n"
    "none /mnt/net cifs rw 0 0\n"
)


def test_assert_native_filesystem_rejects_9p():
    with pytest.raises(RuntimeError, match="9p"):
        config_module.assert_native_filesystem(
            Path("/mnt/c/some/project/data.sqlite3"), mounts_text=FAKE_MOUNTS
        )


def test_assert_native_filesystem_rejects_cifs():
    with pytest.raises(RuntimeError):
        config_module.assert_native_filesystem(
            Path("/mnt/net/share/data.sqlite3"), mounts_text=FAKE_MOUNTS
        )


def test_assert_native_filesystem_accepts_ext4():
    # Should not raise.
    config_module.assert_native_filesystem(
        Path("/home/someone/.messaging-mcp/messaging.sqlite3"), mounts_text=FAKE_MOUNTS
    )


def test_assert_native_filesystem_does_not_raise_when_mounts_unreadable(monkeypatch, tmp_path):
    def fake_read_text(self, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    # Absence of proof is not proof of a problem -- must not raise.
    config_module.assert_native_filesystem(tmp_path / "data.sqlite3")


def test_data_dir_and_db_path_use_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("MESSAGING_MCP_HOME", str(tmp_path / "custom-home"))
    d = config_module.data_dir()
    assert d == tmp_path / "custom-home"
    assert d.is_dir()
    assert config_module.db_path() == d / "messaging.sqlite3"


def test_schema_path_points_at_real_schema_file():
    p = config_module.schema_path()
    assert p.name == "schema.sql"
    assert p.is_file()
    assert "CREATE TABLE messages" in p.read_text()


# ---------------------------------------------------------------------------
# Database concurrency and transaction semantics
# ---------------------------------------------------------------------------


def test_write_serializes_concurrent_increments(file_db):
    def increment(conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT value FROM counter WHERE id = 1").fetchone()
        new_value = row["value"] + 1
        conn.execute("UPDATE counter SET value = ? WHERE id = 1", (new_value,))

    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker():
        try:
            file_db.write(increment)
        except BaseException as exc:  # noqa: BLE001 - want to see anything that escapes
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"unexpected errors escaped write(): {errors!r}"
    final = file_db.read_one("SELECT value FROM counter WHERE id = 1")
    assert final["value"] == 20


def test_write_serializes_concurrent_increments_in_memory(memory_db):
    # Same test against the :memory: shared-connection path.
    def increment(conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT value FROM counter WHERE id = 1").fetchone()
        conn.execute("UPDATE counter SET value = ? WHERE id = 1", (row["value"] + 1,))

    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker():
        try:
            memory_db.write(increment)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors
    final = memory_db.read_one("SELECT value FROM counter WHERE id = 1")
    assert final["value"] == 20


def test_write_exception_propagates_and_rolls_back(file_db):
    class Boom(RuntimeError):
        pass

    def doomed(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO parent (id) VALUES (999)")
        raise Boom("deliberate failure after a partial write")

    with pytest.raises(Boom, match="deliberate failure"):
        file_db.write(doomed)

    # The insert must not have survived the rollback.
    row = file_db.read_one("SELECT id FROM parent WHERE id = 999")
    assert row is None


def test_write_exception_preserves_original_type(file_db):
    def doomed(conn: sqlite3.Connection):
        raise ValueError("specific business error")

    with pytest.raises(ValueError, match="specific business error"):
        file_db.write(doomed)


def test_foreign_keys_enforced_on_writer_connection(file_db):
    def bad_insert(conn: sqlite3.Connection):
        conn.execute("INSERT INTO child (id, parent_id) VALUES (1, 12345)")

    with pytest.raises(sqlite3.IntegrityError):
        file_db.write(bad_insert)

    assert file_db.read_one("SELECT id FROM child WHERE id = 1") is None


def test_foreign_keys_pragma_active_on_read_connection(file_db):
    # write() is the only path that legitimately writes (see
    # test_foreign_keys_enforced_on_writer_connection above), so this checks
    # foreign_keys=ON on the read connection directly via the pragma's
    # value rather than via a doomed insert -- which read() must refuse
    # outright now (test_read_cannot_write), independent of what its
    # content would have done to referential integrity.
    rows = file_db.read("PRAGMA foreign_keys")
    assert rows[0][0] == 1


def test_foreign_keys_pragma_active_on_read_connection_in_memory(memory_db):
    rows = memory_db.read("PRAGMA foreign_keys")
    assert rows[0][0] == 1


def test_read_cannot_write(file_db):
    # The single-writer invariant must be enforced by the connection, not
    # just by convention: a write attempted through read() has to fail
    # loudly instead of quietly bypassing the writer thread.
    with pytest.raises(sqlite3.Error):
        file_db.read("INSERT INTO parent (id) VALUES (777)")
    assert file_db.read_one("SELECT id FROM parent WHERE id = 777") is None

    # The reader connection is opened `mode=ro` at the OS level (see
    # `_new_reader_connection`), which is what makes the guarantee hold even
    # against a caller who tries to undo it. `PRAGMA query_only = OFF` is an
    # ordinary statement `read()` will run without complaint -- it disables
    # the *pragma's* protection -- but the write attempt right after it must
    # still fail, because the underlying file descriptor never had write
    # access to begin with. If this ever starts passing without raising,
    # something (e.g. the connection URI) stopped actually being read-only.
    file_db.read("PRAGMA query_only = OFF")
    with pytest.raises(sqlite3.Error):
        file_db.read("INSERT INTO parent (id) VALUES (778)")
    assert file_db.read_one("SELECT id FROM parent WHERE id = 778") is None


def test_read_cannot_write_in_memory(memory_db):
    # Same invariant on the :memory: shared-connection path, where read()
    # and write() otherwise share one physical connection.
    with pytest.raises(sqlite3.Error):
        memory_db.read("INSERT INTO parent (id) VALUES (777)")
    assert memory_db.read_one("SELECT id FROM parent WHERE id = 777") is None


def test_memory_database_works_end_to_end(memory_db):
    def insert_parent(conn: sqlite3.Connection):
        conn.execute("INSERT INTO parent (id) VALUES (1)")

    memory_db.write(insert_parent)
    row = memory_db.read_one("SELECT id FROM parent WHERE id = 1")
    assert row is not None
    assert row["id"] == 1


def test_close_is_idempotent(tmp_path, schema_file):
    db = Database(path=tmp_path / "closeme.sqlite3", schema=schema_file)
    db.close()
    db.close()  # must not raise


def test_context_manager_closes(tmp_path, schema_file):
    with Database(path=tmp_path / "ctxmgr.sqlite3", schema=schema_file) as db:
        db.write(lambda conn: conn.execute("INSERT INTO parent (id) VALUES (1)"))
    with pytest.raises(RuntimeError):
        db.write(lambda conn: conn.execute("INSERT INTO parent (id) VALUES (2)"))


def test_row_factory_is_sqlite_row(file_db):
    row = file_db.read_one("SELECT value FROM counter WHERE id = 1")
    assert isinstance(row, sqlite3.Row)
    assert row["value"] == 0


def test_schema_applied_once_reopen_does_not_reset_data(tmp_path, schema_file):
    path = tmp_path / "persist.sqlite3"
    db1 = Database(path=path, schema=schema_file)
    db1.write(lambda conn: conn.execute("UPDATE counter SET value = 7 WHERE id = 1"))
    db1.close()

    db2 = Database(path=path, schema=schema_file)
    try:
        row = db2.read_one("SELECT value FROM counter WHERE id = 1")
        assert row["value"] == 7
    finally:
        db2.close()


# ---------------------------------------------------------------------------
# labels
#
# labels.py is deliberately thin in the v2 model: it only names the
# recognized labels and refuses an unknown one. Priority, caps, storage and
# reply_behavior all live in the `label_caps` table (see
# test_schema_constraints.py) rather than as a second copy here, so there is
# nothing to test in this module about priority ordering or storage -- that
# would be testing a fact this module doesn't hold an opinion on.
# ---------------------------------------------------------------------------


def test_validate_behavior_accepts_every_recognized_label():
    for behavior in BEHAVIORS:
        validate_behavior(behavior)  # must not raise


def test_validate_behavior_rejects_unknown_label():
    with pytest.raises(Rejected) as excinfo:
        validate_behavior("[NOT-A-REAL-LABEL]")
    assert excinfo.value.code == "unknown_behavior"


def test_validate_behavior_rejects_case_variant_of_a_real_label():
    # A label is an exact token, not a case-insensitive one -- a near-miss
    # must be refused exactly like any other unrecognized string.
    with pytest.raises(Rejected) as excinfo:
        validate_behavior("[query]")
    assert excinfo.value.code == "unknown_behavior"


def test_the_blocking_labels_are_the_two_that_stop_their_sender():
    # Sending either stops the sender and gives its slot to the question. They
    # are ordinary labels an agent may send -- there is no separate hold label
    # any more, so both must be ones validate_behavior accepts.
    assert set(BLOCKING_BEHAVIORS) == {"[QUERY]", "[ERROR]"}
    for behavior in BLOCKING_BEHAVIORS:
        validate_behavior(behavior)  # must not raise


def test_there_is_no_hold_label():
    """The question an agent asked IS the hold, so nothing else carries one."""
    assert "[IDLE]" not in BEHAVIORS
    with pytest.raises(Rejected):
        validate_behavior("[IDLE]")


def test_behaviors_contains_exactly_the_five_labels_the_design_names():
    # Membership, not order or count-as-shape: what matters behaviourally is
    # that every label the rest of the system refers to by name is one
    # validate_behavior will accept, and nothing else sneaks in.
    for name in (
        "[TRUTHFUL-REPORT]",
        "[QUERY]",
        "[ERROR]",
        "[MESSAGE-RESPONSE]",
        "[RESEARCH]",
    ):
        assert name in BEHAVIORS
        validate_behavior(name)
    assert len(BEHAVIORS) == 5


# ---------------------------------------------------------------------------
# slots.WorkingSlots
# ---------------------------------------------------------------------------


def test_lock_for_returns_the_same_lock_for_the_same_partner():
    slots = WorkingSlots()
    assert slots.lock_for(1) is slots.lock_for(1)


def test_lock_for_returns_a_different_lock_for_a_different_partner():
    slots = WorkingSlots()
    assert slots.lock_for(1) is not slots.lock_for(2)


def test_lock_for_lock_is_reentrant():
    # A swap holds this lock across a remote call, and code inside that
    # remote call path may legitimately re-enter it on the same thread. A
    # plain (non-reentrant) Lock would deadlock on the second acquire; using
    # a bounded timeout here means a regression fails the assertion instead
    # of hanging the test run forever.
    slots = WorkingSlots()
    lock = slots.lock_for(1)
    got_outer = lock.acquire(timeout=5)
    assert got_outer, "could not acquire the lock at all"
    try:
        got_inner = lock.acquire(timeout=5)
        assert got_inner, (
            "lock_for(partner_id) is not re-entrant: a second acquire by the "
            "same thread did not succeed within the timeout"
        )
        if got_inner:
            lock.release()
    finally:
        lock.release()


def test_get_is_none_when_nothing_has_been_set():
    slots = WorkingSlots()
    assert slots.get(1) is None


def test_set_then_get_returns_the_same_task():
    slots = WorkingSlots()
    task = {"caller_id": 10, "behavior": "[QUERY]"}
    slots.set(1, task)
    assert slots.get(1) == task


def test_set_replaces_whatever_was_previously_held():
    slots = WorkingSlots()
    slots.set(1, {"caller_id": 10, "behavior": "[QUERY]"})
    slots.set(1, {"caller_id": 20, "behavior": "[RESEARCH]"})
    assert slots.get(1) == {"caller_id": 20, "behavior": "[RESEARCH]"}


def test_occupied_lists_only_partners_currently_holding_a_task():
    slots = WorkingSlots()
    assert slots.occupied() == []
    slots.set(1, {"caller_id": 10, "behavior": "[QUERY]"})
    slots.set(2, {"caller_id": 20, "behavior": "[RESEARCH]"})
    assert sorted(slots.occupied()) == [1, 2]
    slots.clear(1)
    assert slots.occupied() == [2]


def test_clear_returns_what_it_held_and_none_the_second_time():
    slots = WorkingSlots()
    task = {"caller_id": 10, "behavior": "[QUERY]"}
    slots.set(1, task)
    first = slots.clear(1)
    assert first == task
    second = slots.clear(1)
    assert second is None


def test_clear_on_a_partner_that_was_never_set_returns_none():
    slots = WorkingSlots()
    assert slots.clear(999) is None


def test_outstanding_is_zero_when_the_slot_is_empty():
    slots = WorkingSlots()
    assert slots.outstanding(1, 10, "[QUERY]") == 0


def test_outstanding_requires_both_caller_and_behavior_to_match():
    slots = WorkingSlots()
    slots.set(1, {"caller_id": 10, "behavior": "[QUERY]"})

    # Both match: the only case that counts.
    assert slots.outstanding(1, 10, "[QUERY]") == 1

    # Caller matches, behavior doesn't.
    assert slots.outstanding(1, 10, "[RESEARCH]") == 0

    # Behavior matches, caller doesn't.
    assert slots.outstanding(1, 11, "[QUERY]") == 0

    # Neither matches.
    assert slots.outstanding(1, 11, "[RESEARCH]") == 0

    # Right caller/behavior, wrong partner -- a different partner's slot
    # must not be consulted at all.
    assert slots.outstanding(2, 10, "[QUERY]") == 0


# ---------------------------------------------------------------------------
# templates
#
# Assertions target behaviour -- what must be present, what must be absent,
# what must be said explicitly -- never whole-string wording, so a rewording
# of the prose is a refactor rather than a test failure.
# ---------------------------------------------------------------------------


def test_research_dispatch_contains_every_path_it_was_given():
    prompt = templates.research_dispatch(
        partner_uuid="u-worker-1", partner_title="the-worker",
        caller_title="orchestrator",
        body="do the thing",
        read_paths=["/proj/read-a", "/proj/read-b"],
        write_paths=["/proj/write-a"],
    )
    for path in ("/proj/read-a", "/proj/read-b", "/proj/write-a"):
        assert path in prompt, f"expected granted path {path!r} to appear in the dispatch"


def test_research_dispatch_does_not_contain_a_path_it_was_not_given():
    prompt = templates.research_dispatch(
        partner_uuid="u-worker-1", partner_title="the-worker",
        caller_title="orchestrator",
        body="do the thing",
        read_paths=["/proj/only-this-one"],
        write_paths=[],
    )
    assert "/proj/some-other-path" not in prompt

    # Two renders with disjoint path sets must not leak into each other.
    other_prompt = templates.research_dispatch(
        partner_uuid="u-worker-1", partner_title="the-worker",
        caller_title="orchestrator",
        body="do the thing",
        read_paths=["/proj/a-completely-different-path"],
        write_paths=[],
    )
    assert "/proj/only-this-one" not in other_prompt
    assert "/proj/a-completely-different-path" not in prompt


def test_research_dispatch_with_no_paths_says_so_explicitly():
    prompt = templates.research_dispatch(
        partner_uuid="u-worker-1", partner_title="the-worker",
        caller_title="orchestrator", body="do the thing", read_paths=[], write_paths=[]
    )
    # An agent handed silence about paths has no reason to think it holds
    # none; the empty case must be a stated fact, not an omitted section.
    assert "no read or write paths" in prompt.lower() or "no paths" in prompt.lower()


def test_research_dispatch_contains_the_body_and_caller_title():
    prompt = templates.research_dispatch(
        partner_uuid="u-worker-1", partner_title="the-worker",
        caller_title="the-delegating-caller",
        body="a distinctive instruction string",
        read_paths=[],
        write_paths=[],
    )
    assert "a distinctive instruction string" in prompt
    assert "the-delegating-caller" in prompt


def test_truthful_report_request_contains_the_original_request_verbatim():
    original = "Verify the wobble coefficient against the 2024 dataset, exactly."
    prompt = templates.truthful_report_request(
        caller_title="orchestrator", original_request=original
    )
    assert original in prompt


def test_truthful_report_request_names_the_caller():
    prompt = templates.truthful_report_request(
        caller_title="a-specific-caller-title", original_request="the request body"
    )
    assert "a-specific-caller-title" in prompt


def test_resume_displaced_names_the_behavior_it_was_given():
    for behavior in ("[RESEARCH]", "[QUERY]", "[TRUTHFUL-REPORT]"):
        prompt = templates.resume_displaced(behavior=behavior)
        assert behavior in prompt


def test_resume_displaced_does_not_name_a_behavior_it_was_not_given():
    prompt = templates.resume_displaced(behavior="[QUERY]")
    assert "[RESEARCH]" not in prompt


def test_there_is_no_template_for_a_hold():
    """Nothing is said to an agent that is waiting on its own question.

    Its remote was stopped as the question took the slot. Handing it a
    paragraph gives it something to act on when the point is that it should do
    nothing until it hears back.
    """
    assert not hasattr(templates, "idle_interruption")


def test_relay_contains_behavior_body_and_caller():
    prompt = templates.relay(partner_uuid="u-worker-1", partner_title="the-worker", caller_title="worker", behavior="[QUERY]", body="what is X?")
    assert "[QUERY]" in prompt
    assert "what is X?" in prompt
    assert "worker" in prompt


def test_relay_does_not_misattribute_the_behavior_it_was_given():
    """Asserted on the announcement line, not the whole prompt.

    The identity block names [QUERY] and [ERROR] as the two reasons an agent
    may call `send` itself, so both strings legitimately appear further down.
    What must never happen is the line that tells the agent WHICH label just
    arrived naming a different one.
    """
    prompt = templates.relay(
        partner_uuid="u-worker-1", partner_title="the-worker",
        caller_title="worker", behavior="[QUERY]", body="what is X?",
    )
    announcement = next(line for line in prompt.splitlines() if "sends you a" in line)
    assert "[QUERY]" in announcement
    assert "[ERROR]" not in announcement


def test_instructing_templates_open_with_the_instructs_header():
    # An agent that cannot tell an instruction from a quotation answers the
    # quotation -- every template that is the Server telling the agent
    # something (as opposed to relaying a Partner's own words) must open
    # with the same, distinct marker.
    instructing_prompts = [
        templates.research_dispatch(
            partner_uuid="u-worker-1", partner_title="the-worker",
            caller_title="c", body="b", read_paths=[], write_paths=[]
        ),
        templates.resume_displaced(behavior="[QUERY]"),
        templates.truthful_report_request(caller_title="c", original_request="r"),
    ]
    for prompt in instructing_prompts:
        assert prompt.startswith(templates.INSTRUCTS)


def test_relay_opens_with_the_relays_header_not_instructs():
    prompt = templates.relay(partner_uuid="u-worker-1", partner_title="the-worker", caller_title="c", behavior="[QUERY]", body="b")
    assert prompt.startswith(templates.RELAYS)
    assert not prompt.startswith(templates.INSTRUCTS)


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


def test_rejected_carries_code_message_next_call():
    exc = Rejected("queue_full", "Queue is full.", next_call="Call status.")
    assert exc.code == "queue_full"
    assert exc.message == "Queue is full."
    assert exc.next_call == "Call status."
    assert "queue_full" in str(exc)
    assert "Queue is full." in str(exc)


def test_rejected_default_next_call_is_none():
    exc = Rejected("some_code", "some message")
    assert exc.next_call is None


def test_needs_remote_carries_capability_and_reason():
    exc = NeedsRemote("send_email", "no SMTP configured")
    assert exc.capability == "send_email"
    assert exc.reason == "no SMTP configured"
    assert "send_email" in str(exc)
    assert "no SMTP configured" in str(exc)


def test_error_hierarchy():
    assert issubclass(Rejected, MessagingError)
    assert issubclass(NeedsRemote, MessagingError)
    assert issubclass(MessagingError, Exception)


# ---------------------------------------------------------------------------
# responses
# ---------------------------------------------------------------------------


def test_rejected_response_shape():
    out = responses.rejected(
        'Queue for "lit-review" is full (1/1).',
        next_call="Call status to see the pair's state.",
    )
    assert out.startswith('[rejected] Queue for "lit-review" is full (1/1).')
    assert "Nothing was changed." in out
    assert "Call status to see the pair's state." in out


def test_rejected_response_without_next_call_still_states_nothing_changed():
    out = responses.rejected("Cannot do that.")
    assert out.startswith("[rejected] Cannot do that.")
    assert responses.NOTHING_CHANGED in out


def test_nothing_new_response_shape():
    out = responses.nothing_new('No recorded messages with "lit-review".')
    assert out.startswith('[nothing new] No recorded messages with "lit-review".')


def test_nothing_new_with_next_call():
    out = responses.nothing_new("Nothing to report.", next_call="Call poll again later.")
    assert out.startswith("[nothing new] Nothing to report.")
    assert "Call poll again later." in out


def test_still_working_response_shape_and_anti_poll():
    out = responses.still_working("lit-review handoff")
    assert out.startswith("[still working - lit-review handoff]")
    assert responses.ANTI_POLL in out


def test_ok_response_with_anti_poll():
    out = responses.ok("Message sent.", anti_poll=True)
    assert out.startswith("[ok] Message sent.")
    assert responses.ANTI_POLL in out


def test_ok_response_with_next_call():
    out = responses.ok("Partner created.", next_call="Call send to deliver the first message.")
    assert out.startswith("[ok] Partner created.")
    assert "Call send to deliver the first message." in out
