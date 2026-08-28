"""Tests for the MCP tool surface in `mcp_server/server.py`.

These tests exercise `build_server` end-to-end through FastMCP's own public
API (`list_tools` / `call_tool`) rather than reaching into private attributes,
using `Database(path=":memory:")` and `StubExtension` exactly as the rest of
this project's test suite does.

This tests BEHAVIOUR, not the tool inventory: nowhere here does a test assert
a fixed name/count for the full tool set (a list of tool names breaks on
every rename and would not notice a dropped constraint). What is asserted
instead:

- No tool schema anywhere takes a `requester_title` parameter -- callers
  identify themselves by uuid; titles only ever address a target.
- `send`'s schema has no `role` parameter and no path arguments -- permissions
  are configured in advance by their own tools, never as a side effect of
  sending work.
- Every tool -- discovered live from `list_tools`, not hard-coded -- turns a
  `Rejected` and a `NeedsRemote` from `MessagingCore` (or, for
  `notify_partner_push`, `PollingServer`) into a response body rather than
  letting either propagate as a raised exception.
- `notify_partner_push` exists on the built server if and only if a
  `PollingServer` was passed to `build_server`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from extension.base import StubExtension
from mcp.types import ContentBlock
from extension.base import RemoteFailure
from mcp_server.server import build_server
from messaging_core.core import MessagingCore
from messaging_core.db import Database
from messaging_core.errors import NeedsRemote, Rejected
from messaging_core.responses import ANTI_POLL, NOTHING_CHANGED
from polling.server import PollingServer


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    database = Database(path=":memory:")
    yield database
    database.close()


def ext(prefix: str = "science_") -> StubExtension:
    return StubExtension(source_prefix=prefix)


@pytest.fixture
def core(db):
    return MessagingCore(db, ext("science_"))


@pytest.fixture
def server(core):
    return build_server(name="messaging-test", core=core)


def _run(coro):
    return asyncio.run(coro)


def _text_of(result) -> str:
    """Extract the plain-text body FastMCP returned for a tool call.

    `call_tool(..., )` (as used internally with `convert_result=True`) may
    return either a bare content sequence or a `(content, structured)` pair
    depending on whether the tool's return annotation produced an output
    schema; either way the human-readable body is the concatenated text of
    the content blocks.
    """
    content: Sequence[ContentBlock]
    if isinstance(result, tuple):
        content, _structured = result
    else:
        content = result
    return "\n".join(block.text for block in content if hasattr(block, "text"))


def call(server, name: str, arguments: dict) -> str:
    result = _run(server.call_tool(name, arguments))
    return _text_of(result)


def list_tools(server):
    return _run(server.list_tools())


def make_project(core: MessagingCore, *, title: str, source_prefix: str, system_id: str) -> int:
    core.extension = ext(source_prefix)
    return core.create_project(title=title, source_prefix=source_prefix, project_system_id=system_id)


def make_partner(core: MessagingCore, *, project_id: int, title: str, remote_id: str) -> dict:
    return core.create_partner(
        project_id=project_id, title=title, partner_id_in_remote=remote_id, descr="a partner"
    )


def _dummy_value(schema: dict):
    """A type-appropriate placeholder value for one JSON-schema property.

    Only used to synthesize arguments for a tool call whose backing
    `MessagingCore`/`PollingServer` method has been monkeypatched to raise
    unconditionally -- the actual values never reach any real logic, so all
    that matters is that FastMCP's own argument validation accepts them.
    """
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            if option.get("type") != "null":
                return _dummy_value(option)
        return None
    kind = schema.get("type")
    if kind == "string":
        return "x"
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return True
    if kind == "array":
        return [_dummy_value(schema.get("items", {"type": "string"}))]
    if kind == "object":
        return {}
    return "x"


def _synthetic_args(tool) -> dict:
    """Minimal arguments satisfying `tool`'s required parameters."""
    schema = tool.inputSchema or {}
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    return {name: _dummy_value(properties[name]) for name in required}


def _raise(exc: Exception):
    def _inner(*args, **kwargs):
        raise exc

    return _inner


# ---------------------------------------------------------------------------
# requester_title never appears
# ---------------------------------------------------------------------------


def test_no_tool_takes_a_requester_title(server):
    tools = list_tools(server)
    for tool in tools:
        properties = (tool.inputSchema or {}).get("properties", {})
        assert "requester_title" not in properties, f"{tool.name} exposes requester_title"


def test_no_tool_takes_a_requester_title_with_polling(db):
    core2 = MessagingCore(db, ext("science_"))
    polling = PollingServer(db, extensions={})
    server_with_polling = build_server(name="messaging-test", core=core2, polling=polling)
    for tool in list_tools(server_with_polling):
        properties = (tool.inputSchema or {}).get("properties", {})
        assert "requester_title" not in properties, f"{tool.name} exposes requester_title"


def test_every_tool_has_a_non_empty_description(server):
    tools = list_tools(server)
    for tool in tools:
        assert tool.description, f"{tool.name} has an empty description"


# ---------------------------------------------------------------------------
# send: no role, no path arguments
# ---------------------------------------------------------------------------


def test_send_schema_has_no_role_or_path_arguments(server):
    tools = {t.name: t for t in list_tools(server)}
    assert "send" in tools
    properties = (tools["send"].inputSchema or {}).get("properties", {})

    # role was removed entirely -- there is no longer a causal-role concept
    # a caller passes on send.
    assert "role" not in properties, "send must not accept a role argument"
    # Permissions are configured in advance by their own tools
    # (get_permissions/add_permissions/delete_permissions); a send that took
    # paths would be configuring them too late by construction.
    for path_arg in ("read_paths", "write_paths", "paths"):
        assert path_arg not in properties, f"send must not accept {path_arg!r}"


# ---------------------------------------------------------------------------
# notify_partner_push exists iff a PollingServer was passed
# ---------------------------------------------------------------------------


def test_notify_partner_push_exists_only_with_a_polling_server(db):
    core_no_polling = MessagingCore(db, ext("science_"))
    server_without_polling = build_server(name="messaging-test", core=core_no_polling)
    names_without = {t.name for t in list_tools(server_without_polling)}
    assert "notify_partner_push" not in names_without

    core_with_polling = MessagingCore(db, ext("science_"))
    polling = PollingServer(db, extensions={})
    server_with_polling = build_server(name="messaging-test", core=core_with_polling, polling=polling)
    names_with = {t.name for t in list_tools(server_with_polling)}
    assert "notify_partner_push" in names_with


# ---------------------------------------------------------------------------
# success path
# ---------------------------------------------------------------------------


def test_successful_call_has_no_rejected_marker(server, core):
    body = call(
        server,
        "create_project",
        {"title": "proj-ok", "source_prefix": "science_", "project_system_id": "sys-ok"},
    )
    assert "[rejected]" not in body
    assert body.startswith("[ok]")


# ---------------------------------------------------------------------------
# rejection path
# ---------------------------------------------------------------------------


def test_rejected_call_returns_marker_and_raises_nothing(server, core):
    make_project(core, title="dup-project", source_prefix="science_", system_id="dup-sys")

    # Duplicate title -> Rejected inside MessagingCore.create_project. The
    # tool must swallow it and report it as text, never let it propagate.
    body = call(
        server,
        "create_project",
        {"title": "dup-project", "source_prefix": "science_", "project_system_id": "some-other-sys"},
    )

    assert body.startswith("[rejected]")
    assert NOTHING_CHANGED in body


# ---------------------------------------------------------------------------
# NeedsRemote path
# ---------------------------------------------------------------------------


def test_needs_remote_names_the_capability_and_is_not_success(db):
    core_without_extension = MessagingCore(db, extension=None)
    server_no_ext = build_server(name="messaging-test", core=core_without_extension)

    body = call(
        server_no_ext,
        "create_project",
        {"title": "needs-remote-proj", "source_prefix": "science_", "project_system_id": "sys-nr"},
    )

    assert "verify_project_system_id" in body
    assert not body.startswith("[ok]")
    assert "[rejected]" not in body


# ---------------------------------------------------------------------------
# every tool: Rejected and NeedsRemote both become text, never a raise
# ---------------------------------------------------------------------------


def test_every_tool_turns_rejected_and_needs_remote_into_a_response_body(monkeypatch, db):
    """The tool layer's whole job is turning `Rejected`/`NeedsRemote` into
    readable text -- for every tool actually registered, not just the ones
    with a dedicated example above.

    The tool set itself is read live from `list_tools`, never hard-coded, so
    this does not care what the tools are named or how many there are. What
    it relies on instead is `mcp_server.server`'s own documented invariant
    that "this module never makes a decision MessagingCore didn't already
    make": each tool function calls a `MessagingCore` method of the exact
    same name (or, for `notify_partner_push`, `PollingServer`'s), so
    redirecting that identically named method to raise is enough to prove
    the tool wrapping it never lets the exception through.
    """
    core2 = MessagingCore(db, ext("science_"))
    polling = PollingServer(db, extensions={})
    srv = build_server(name="messaging-test", core=core2, polling=polling)

    tools = list_tools(srv)
    assert tools, "no tools registered; nothing to check"

    for tool in tools:
        args = _synthetic_args(tool)
        owner = polling if tool.name == "notify_partner_push" else core2
        assert hasattr(owner, tool.name), (
            f"tool {tool.name!r} has no identically named method on {owner!r} for this "
            "test to redirect -- update the owner-resolution rule in this test to match "
            "wherever its logic actually lives."
        )

        monkeypatch.setattr(owner, tool.name, _raise(Rejected("synthetic_rejection", f"{tool.name} refused")))
        body = call(srv, tool.name, args)
        assert body.startswith("[rejected]"), f"{tool.name}: a Rejected leaked out as {body!r}"
        assert NOTHING_CHANGED in body, f"{tool.name}: rejection body is missing 'nothing changed': {body!r}"

        monkeypatch.setattr(
            owner, tool.name, _raise(NeedsRemote(tool.name, f"{tool.name} needs a remote extension"))
        )
        body = call(srv, tool.name, args)
        assert body.startswith("[needs remote]"), f"{tool.name}: a NeedsRemote leaked out as {body!r}"
        assert not body.startswith("[ok]"), f"{tool.name}: NeedsRemote was read as success: {body!r}"
        assert "[rejected]" not in body, f"{tool.name}: NeedsRemote was read as a rejection: {body!r}"
        assert repr(tool.name) in body, f"{tool.name}: needs-remote body does not name the capability: {body!r}"


def test_every_tool_turns_a_remote_failure_into_a_response_body(monkeypatch, db):
    """An adapter's own exception must not reach an agent as a traceback.

    Every adapter raises `RuntimeError` subclasses of its own -- a missing
    binary, a refused connection, an HTTP error. None of them is a `Rejected`
    or a `NeedsRemote`, so before `RemoteFailure` existed each one escaped
    every tool wrapper here as a raw Python stack trace, against this
    module's own rule that a failure is never one.

    Discovered live from `list_tools` for the same reason as the test above:
    a hard-coded list would silently stop covering a tool added later.
    """
    core2 = MessagingCore(db, ext("science_"))
    polling = PollingServer(db, extensions={})
    srv = build_server(name="messaging-test", core=core2, polling=polling)

    tools = list_tools(srv)
    assert tools, "no tools registered; nothing to check"

    for tool in tools:
        args = _synthetic_args(tool)
        owner = polling if tool.name == "notify_partner_push" else core2
        monkeypatch.setattr(
            owner, tool.name, _raise(RemoteFailure(f"{tool.name}: tmux is not installed"))
        )
        body = call(srv, tool.name, args)
        assert body.startswith("[remote failed]"), (
            f"{tool.name}: a RemoteFailure leaked out as {body!r}"
        )
        assert "tmux is not installed" in body, (
            f"{tool.name}: the body must name what actually broke: {body!r}"
        )
        assert NOTHING_CHANGED not in body, (
            f"{tool.name}: a transport failure is not a rule-based rejection and must not "
            f"borrow its wording: {body!r}"
        )
        assert "Traceback" not in body


def test_an_adapter_error_really_is_a_remote_failure():
    """The renderer above is only reachable if the adapters actually inherit it.

    Checked on the concrete classes rather than trusting the base: each was
    a plain RuntimeError, and re-parenting is what routes it to the handler.
    """
    from adapters.antigravity.adapter import TmuxBinaryMissing
    from adapters.claude_science.adapter import (
        ClaudeScienceHTTPError,
        ClaudeScienceProjectIdUnknown,
    )
    from adapters.notebooklm.adapter import NlmBinaryMissing

    for cls in (TmuxBinaryMissing, ClaudeScienceHTTPError,
                ClaudeScienceProjectIdUnknown, NlmBinaryMissing):
        assert issubclass(cls, RemoteFailure), (
            f"{cls.__name__} still escapes every tool wrapper as a raw traceback"
        )
        assert issubclass(cls, RuntimeError), (
            f"{cls.__name__} must stay a RuntimeError so existing handlers are unaffected"
        )


# ---------------------------------------------------------------------------
# send never claims nothing happened once the message is committed
# ---------------------------------------------------------------------------


def test_send_does_not_say_nothing_changed_when_the_message_is_queued(monkeypatch, db):
    """`send` commits the queue row and THEN calls `advance`, which can fail.

    Rendering that failure with "Nothing was changed." tells the agent to
    retry -- which double-sends and burns its `[QUERY]` cap on work the
    system already accepted.
    """
    core2 = MessagingCore(db, ext("science_"))
    srv = build_server(name="messaging-test", core=core2)

    committed = Rejected("synthetic", "the remote refused the swap")
    committed.already_committed = True
    monkeypatch.setattr(core2, "send", _raise(committed))

    body = call(srv, "send", {
        "requester_uuid": "u-1", "queried_partner_title": "bob",
        "message": "hi", "behavior": "[QUERY]",
    })

    assert NOTHING_CHANGED not in body, (
        f"the message IS queued; saying nothing changed invites a double-send: {body!r}"
    )
    assert "queue" in body.lower(), (
        f"the body must say the message is queued, so the agent does not resend: {body!r}"
    )


def test_send_still_says_nothing_changed_when_nothing_was_committed(monkeypatch, db):
    """The ordinary refusal is unchanged -- a cap or a missing handshake really
    did leave the queue untouched, and an agent must be free to fix and retry."""
    core2 = MessagingCore(db, ext("science_"))
    srv = build_server(name="messaging-test", core=core2)

    monkeypatch.setattr(core2, "send", _raise(Rejected("no_handshake", "no handshake exists")))

    body = call(srv, "send", {
        "requester_uuid": "u-1", "queried_partner_title": "bob",
        "message": "hi", "behavior": "[QUERY]",
    })

    assert body.startswith("[rejected]")
    assert NOTHING_CHANGED in body, f"a genuine refusal must still say so: {body!r}"


# ---------------------------------------------------------------------------
# send's anti-poll body
# ---------------------------------------------------------------------------


def test_send_body_ends_with_anti_poll_line(db):
    # science_ (not nlm_): the sender has to belong to a source that can
    # originate a message at all -- nlm_'s can_send=0 makes an nlm_ sender an
    # immediate, unrelated [rejected] ("A nlm_ partner never originates a
    # message") that would never reach the anti-poll tail this test is
    # actually about.
    core2 = MessagingCore(db, ext("science_"))
    server2 = build_server(name="messaging-test", core=core2)

    project_id = make_project(core2, title="proj-send", source_prefix="science_", system_id="sys-send")
    sender = make_partner(core2, project_id=project_id, title="alice", remote_id="rem-alice")
    make_partner(core2, project_id=project_id, title="bob", remote_id="rem-bob")

    core2.claim_orchestrator(
        requester_uuid=sender["uuid"], project_id=project_id, orchestrator_type="project-orchestrator"
    )
    core2.handshake(requester_uuid=sender["uuid"], partner_title="bob")

    body = call(
        server2,
        "send",
        {
            "requester_uuid": sender["uuid"],
            "queried_partner_title": "bob",
            "message": "hello",
            "behavior": "[QUERY]",
        },
    )

    assert "[rejected]" not in body
    assert body.rstrip().endswith(ANTI_POLL)


def test_send_does_not_invite_a_resend_when_the_remote_failed_after_admission(monkeypatch, db):
    """`_remote_failed_body` ordinarily closes with "send the work again".

    That is right when the send never landed. When admission already committed
    and `advance` has put the task back in the queue, it is an instruction to
    double-send — and a failed remote is the ordinary shape of a missing binary
    or a refused connection, not an exotic case.
    """
    core2 = MessagingCore(db, ext("science_"))
    srv = build_server(name="messaging-test", core=core2)

    committed = RemoteFailure("tmux is not installed")
    committed.already_committed = True
    monkeypatch.setattr(core2, "send", _raise(committed))

    body = call(srv, "send", {
        "requester_uuid": "u-1", "queried_partner_title": "bob",
        "message": "hi", "behavior": "[QUERY]",
    })

    assert body.startswith("[remote failed]")
    assert "tmux is not installed" in body
    assert "queued" in body.lower(), (
        f"the caller must be told the message is queued: {body!r}"
    )
    assert "again" not in body.lower().split("queued")[0], (
        f"nothing before that should invite a resend: {body!r}"
    )


def test_a_remote_failure_before_admission_still_says_to_send_again(monkeypatch, db):
    """The ordinary case is unchanged: nothing landed, so re-sending is right."""
    core2 = MessagingCore(db, ext("science_"))
    srv = build_server(name="messaging-test", core=core2)

    monkeypatch.setattr(core2, "send", _raise(RemoteFailure("tmux is not installed")))

    body = call(srv, "send", {
        "requester_uuid": "u-1", "queried_partner_title": "bob",
        "message": "hi", "behavior": "[QUERY]",
    })

    assert body.startswith("[remote failed]")
    assert "again" in body.lower()
