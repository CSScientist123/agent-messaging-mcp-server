"""Tests for the three real `RemoteExtension` adapters and `adapters.registry`.

Nothing here touches a live remote: `subprocess.run` (NotebookLM, Antigravity)
and `urllib.request.urlopen` (Claude Science) are monkeypatched with small
fakes that stand in for `nlm`, `tmux`, and the Claude Science HTTP API. The
fakes below encode the REAL argv/routes/markers recovered from each remote's
own ground-truth source (`claude_science_mcp/api.py` + `client.py`,
`notebooklm_server.py`, `antigravity-cli/index.js` + `lib/control.js`), not
the originally guessed ones -- see each adapter module's own docstring for
the full list of corrections.

Covers:
- Each adapter's `source_prefix`, and `adapters.registry.build_extension`
  dispatching to the right class (and refusing `"code_"`).
- NotebookLM's `stop_remote_execution` refusing (it is a
  `NonExecutingExtension`) and all three permission operations refusing with
  `Rejected("not_path_configurable", ...)`, touching the `nlm` CLI never.
- NotebookLM's real `nlm notebook get`/`nlm notebook query <notebook>
  <question>`/`nlm chats get <notebook> --json` calls (no `source list`, no
  `--source` flag, no `notebook chat --latest` -- none of those exist in the
  real CLI), with the query path firing exactly once (no retry on a reported
  failure) and the answer harvested from a separate call.
- Claude Science `verify_*` hitting the real, un-nested `/api/frames/{id}`
  route and sending an `Origin` header on every request.
- Claude Science `poll_completion` reading `/api/frames/{id}/trace-shallow`
  and being False for each busy status, True for `completed`.
- Claude Science `deliver_message` going through the real
  `POST /api/request` (with `root_frame_id`, `project_id`, `input_data`),
  not the guessed `/api/frames/{id}/turns`, and carrying the real
  `x-operon-csrf` header on that mutating call.
- Claude Science `stop_remote_execution` refusing (no real cancel route
  exists) and all three permission operations refusing with
  `Rejected("not_path_configurable", ...)`, touching the HTTP API never.
- Antigravity's real truncated session name (`agy-<first 8 chars>`), the
  real two-call literal-text-then-Enter send, and `poll_completion` raising
  `Rejected` on a real permission-prompt header/footer, with a message
  naming interrupt/add_permissions rather than an answer.
- Antigravity `get_permissions` reading the project config JSON (never the
  tmux pane): a missing config file, a missing/empty/unreadable project-id
  file, malformed JSON, and flattening `projectResources` values that are
  strings, lists, or dicts.
- Antigravity `add_permissions` driving the `/permissions` TUI over tmux in
  the exact real key sequence, refusing (and typing nothing) when the editor
  never opens, still closing the editor when typing raises, returning `None`
  without verifying its own success, and never touching the config file to
  check.
- Antigravity `delete_permissions` refusing when the editor advertises no
  removal key (without pressing anything speculatively) and using the
  advertised key when one exists.
- A missing `nlm` binary producing a clearly named error.

There is no `resume_remote_execution` anywhere any more -- it has been
removed from the extension interface and every adapter. What replaced it, on
`RemoteExtension`, is three permission operations
(`get_permissions`/`add_permissions`/`delete_permissions`), concrete with a
refusing default; only Antigravity overrides them for real.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

import pytest

import adapters.antigravity.adapter as antigravity_adapter
from adapters.antigravity.adapter import AntigravityExtension
from adapters.claude_science.adapter import (
    ClaudeScienceExtension,
    ClaudeScienceProjectIdUnknown,
)
from adapters.notebooklm.adapter import NlmBinaryMissing, NlmNotebookIdUnknown, NotebookLMExtension
from adapters.registry import build_extension
from messaging_core.errors import Rejected

# ---------------------------------------------------------------------------
# registry + source_prefix
# ---------------------------------------------------------------------------


def test_each_adapter_reports_its_own_source_prefix():
    assert NotebookLMExtension(nlm_path="/usr/bin/nlm").source_prefix == "nlm_"
    assert ClaudeScienceExtension().source_prefix == "science_"
    assert AntigravityExtension(tmux_path="/usr/bin/tmux").source_prefix == "gemini_"


def test_registry_dispatches_to_the_right_class():
    assert isinstance(build_extension("nlm_", nlm_path="/usr/bin/nlm"), NotebookLMExtension)
    assert isinstance(build_extension("science_"), ClaudeScienceExtension)
    assert isinstance(build_extension("gemini_", tmux_path="/usr/bin/tmux"), AntigravityExtension)


def test_registry_refuses_code_prefix():
    with pytest.raises(Rejected) as exc_info:
        build_extension("code_")
    assert exc_info.value.code == "no_adapter_for_code"


def test_registry_refuses_unknown_prefix():
    with pytest.raises(Rejected):
        build_extension("bogus_")


# ---------------------------------------------------------------------------
# NotebookLM
# ---------------------------------------------------------------------------


def test_notebooklm_never_executes():
    ext = NotebookLMExtension(nlm_path="/usr/bin/nlm")
    with pytest.raises(Rejected):
        ext.stop_remote_execution(partner_id_in_remote="src-1", reason="testing")


def test_notebooklm_refuses_all_permission_operations_and_touches_nothing(monkeypatch):
    # NotebookLM never executes at all and has no per-source filesystem grant
    # -- a fabricated success here is exactly the failure that makes an agent
    # send work which then stops on a prompt. The base class's refusal must
    # come through unmodified, and the `nlm` CLI must never be invoked.
    def boom(cmd, capture_output=True, text=True):
        raise AssertionError(f"NotebookLM must not touch the nlm CLI for a permission call, got: {cmd}")

    monkeypatch.setattr(subprocess, "run", boom)
    ext = NotebookLMExtension(nlm_path="/usr/bin/nlm")

    with pytest.raises(Rejected) as exc_info:
        ext.get_permissions(partner_id_in_remote="src-1")
    assert exc_info.value.code == "not_path_configurable"

    with pytest.raises(Rejected) as exc_info:
        ext.add_permissions(partner_id_in_remote="src-1", rules=["read_file(/a)"])
    assert exc_info.value.code == "not_path_configurable"

    with pytest.raises(Rejected) as exc_info:
        ext.delete_permissions(partner_id_in_remote="src-1", rules=["read_file(/a)"])
    assert exc_info.value.code == "not_path_configurable"


def test_notebooklm_query_does_not_retry_then_harvests(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, capture_output=True, text=True):
        calls.append(cmd)
        if cmd[1:3] == ["notebook", "get"]:
            # Real substitute for "list this notebook's sources" -- there is
            # no `nlm source list`. Used here only to seed the notebook-id
            # cache via verify_partner_id_in_remote.
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="sources:\n  src-1\n", stderr="")
        if cmd[1:3] == ["notebook", "query"]:
            # Reported failure -- the documented lie. Must not trigger a retry.
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="error: failed")
        if cmd[1:3] == ["chats", "get"]:
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout=json.dumps({"transcript": [{"text": "the real answer"}]}), stderr=""
            )
        raise AssertionError(f"unexpected nlm invocation: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ext = NotebookLMExtension(nlm_path="/usr/bin/nlm", harvest_wait_seconds=0)
    # verify_partner_id_in_remote is what learns a source's notebook id in
    # the real system (messaging_core calls it once, at partner
    # registration, before any delivery) -- deliver_message/
    # read_remote_result have no other way to find it.
    assert ext.verify_partner_id_in_remote("nb-1", "src-1") is True

    remote_id = ext.deliver_message(partner_id_in_remote="src-1", behavior="[QUERY]", body="what is this?")
    assert isinstance(remote_id, str) and remote_id

    query_calls = [c for c in calls if c[1:3] == ["notebook", "query"]]
    assert len(query_calls) == 1, "deliver_message must fire the query exactly once, never retry"
    # Real `nlm notebook query <notebook> <question>` -- notebook id
    # positional, no `--source` flag (that was a guess).
    assert query_calls[0][3] == "nb-1"
    assert query_calls[0][4] == "what is this?"
    assert "--source" not in query_calls[0]

    answer = ext.read_remote_result(partner_id_in_remote="src-1")
    assert answer == "the real answer"

    harvest_calls = [c for c in calls if c[1:3] == ["chats", "get"]]
    assert len(harvest_calls) == 1
    # Real `nlm chats get <notebook> --json` -- not `notebook chat --source
    # --latest` (that was a guess; no such subcommand exists).
    assert harvest_calls[0][3] == "nb-1"
    assert "--json" in harvest_calls[0]
    # Still exactly one query call -- read_remote_result must not re-fire it.
    assert len([c for c in calls if c[1:3] == ["notebook", "query"]]) == 1


def test_notebooklm_deliver_message_without_prior_verify_raises_named_error():
    ext = NotebookLMExtension(nlm_path="/usr/bin/nlm", harvest_wait_seconds=0)
    with pytest.raises(NlmNotebookIdUnknown):
        ext.deliver_message(partner_id_in_remote="src-unregistered", behavior="[QUERY]", body="hi")


def test_notebooklm_poll_completion_is_always_true(monkeypatch):
    ext = NotebookLMExtension(nlm_path="/usr/bin/nlm")
    assert ext.poll_completion(partner_id_in_remote="src-1") is True


def test_notebooklm_verify_project_and_partner(monkeypatch):
    def fake_run(cmd, capture_output=True, text=True):
        if cmd[1:3] == ["notebook", "get"]:
            notebook = cmd[3]
            if notebook == "nb-missing":
                return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="")
            # Real `notebook get` output includes the source list itself --
            # see notebooklm_server.py's own notebook_get docstring. There
            # is no separate `nlm source list` command.
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="sources:\n  src-1\n", stderr="")
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ext = NotebookLMExtension(nlm_path="/usr/bin/nlm")

    assert ext.verify_project_system_id("nb-ok") is True
    assert ext.verify_project_system_id("nb-missing") is False
    assert ext.verify_partner_id_in_remote("nb-ok", "src-1") is True
    assert ext.verify_partner_id_in_remote("nb-ok", "src-nope") is False


def test_notebooklm_missing_binary_raises_named_error(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    ext = NotebookLMExtension()  # no explicit path -> falls back to shutil.which
    with pytest.raises(NlmBinaryMissing) as exc_info:
        ext.verify_project_system_id("nb-1")
    assert "nlm" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Claude Science
# ---------------------------------------------------------------------------


class _FakeHeaders:
    """Minimal stand-in for the real `http.client.HTTPMessage` -- just
    enough of `get_all` for the CSRF Set-Cookie parsing this adapter does."""

    def __init__(self, raw: dict[str, list[str]] | None = None) -> None:
        self._raw = raw or {}

    def get_all(self, name: str):
        return self._raw.get(name, [])


class _FakeHTTPResponse:
    def __init__(self, status: int, body: dict | None, headers: dict[str, list[str]] | None = None) -> None:
        self.status = status
        self._body = json.dumps(body).encode("utf-8") if body is not None else b""
        self.headers = _FakeHeaders(headers)

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        pass


def _http_error(url: str, status: int, body: dict | None = None) -> urllib.error.HTTPError:
    import io

    payload = json.dumps(body).encode("utf-8") if body is not None else b""
    return urllib.error.HTTPError(url, status, "error", hdrs=None, fp=io.BytesIO(payload))


def test_claude_science_verify_true_on_200_false_on_404_and_sends_origin(monkeypatch):
    captured: dict = {}

    def fake_urlopen(request):
        captured["request"] = request
        if "proj-missing" in request.full_url:
            raise _http_error(request.full_url, 404)
        return _FakeHTTPResponse(200, {"id": "proj-1"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ext = ClaudeScienceExtension(base_url="http://127.0.0.1:8000", cookie="session=abc")

    assert ext.verify_project_system_id("proj-1") is True
    assert captured["request"].get_header("Origin") == "http://127.0.0.1:8000"

    assert ext.verify_project_system_id("proj-missing") is False
    assert captured["request"].get_header("Origin") == "http://127.0.0.1:8000"


def test_claude_science_verify_partner_true_false(monkeypatch):
    captured: dict = {}

    def fake_urlopen(request):
        captured["request"] = request
        if "frame-missing" in request.full_url:
            raise _http_error(request.full_url, 404)
        return _FakeHTTPResponse(200, {"id": "frame-1"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ext = ClaudeScienceExtension()

    assert ext.verify_partner_id_in_remote("proj-1", "frame-1") is True
    # Real route is un-nested: GET /api/frames/{id}, never
    # /api/projects/{id}/frames/{id} (that nesting was a guess).
    assert captured["request"].full_url.endswith("/api/frames/frame-1")
    assert "proj-1" not in captured["request"].full_url

    assert ext.verify_partner_id_in_remote("proj-1", "frame-missing") is False


@pytest.mark.parametrize("status", ["running", "queued", "in_progress", "streaming", "processing"])
def test_claude_science_poll_completion_false_while_busy(monkeypatch, status):
    def fake_urlopen(request):
        return _FakeHTTPResponse(200, {"status": status})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ext = ClaudeScienceExtension()
    assert ext.poll_completion(partner_id_in_remote="frame-1") is False


def test_claude_science_poll_completion_true_when_completed(monkeypatch):
    captured: dict = {}

    def fake_urlopen(request):
        captured["request"] = request
        return _FakeHTTPResponse(200, {"status": "completed"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ext = ClaudeScienceExtension()
    assert ext.poll_completion(partner_id_in_remote="frame-1") is True
    # Real status is read from GET /api/frames/{id}/trace-shallow, never a
    # /status route (that route does not exist in the ground truth).
    assert "/api/frames/frame-1/trace-shallow" in captured["request"].full_url


def test_claude_science_deliver_message_uses_request_endpoint_with_root_frame_id(monkeypatch):
    captured: dict = {}

    def fake_urlopen(request):
        captured.setdefault("requests", []).append(request)
        if request.full_url.endswith("/api/csrf"):
            return _FakeHTTPResponse(200, None, headers={"Set-Cookie": ["operon_csrf=tok-abc; Path=/"]})
        return _FakeHTTPResponse(200, {"status": "processing"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ext = ClaudeScienceExtension(base_url="http://127.0.0.1:8000", cookie="session=abc")
    # verify_partner_id_in_remote is what learns a frame's project id in the
    # real system (messaging_core calls it once, at partner registration,
    # before any delivery) -- deliver_message has no other way to find it,
    # since POST /api/request requires project_id on every call.
    ext.verify_partner_id_in_remote("proj-1", "frame-1")

    remote_id = ext.deliver_message(partner_id_in_remote="frame-1", behavior="[QUERY]", body="hi")
    assert isinstance(remote_id, str) and remote_id

    post_requests = [r for r in captured["requests"] if r.get_method() == "POST"]
    assert len(post_requests) == 1
    request = post_requests[0]
    # Real route is POST /api/request -- not the guessed
    # /api/frames/{id}/turns (that route does not exist in the ground truth).
    assert request.full_url == "http://127.0.0.1:8000/api/request"
    assert request.get_header("Origin") == "http://127.0.0.1:8000"
    # Real client.py: a mutating call must carry the CSRF token both as a
    # header and echoed back in the Cookie (double-submit).
    assert request.get_header("X-operon-csrf") == "tok-abc"
    assert "operon_csrf=tok-abc" in request.get_header("Cookie")

    body = json.loads(request.data)
    assert body["root_frame_id"] == "frame-1"
    assert body["project_id"] == "proj-1"
    assert body["input_data"] == {"request": "hi"}
    assert body["target_agent"] == "OPERON"
    # There is no "behavior" field in the real body shape at all.
    assert "behavior" not in body


def test_claude_science_deliver_message_without_prior_verify_raises_named_error(monkeypatch):
    def fake_urlopen(request):
        return _FakeHTTPResponse(200, {"status": "ok"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ext = ClaudeScienceExtension(base_url="http://127.0.0.1:8000")
    with pytest.raises(ClaudeScienceProjectIdUnknown):
        ext.deliver_message(partner_id_in_remote="frame-unregistered", behavior="[QUERY]", body="hi")


def test_claude_science_stop_remote_execution_refuses():
    ext = ClaudeScienceExtension(base_url="http://127.0.0.1:8000")
    with pytest.raises(Rejected) as exc_info:
        ext.stop_remote_execution(partner_id_in_remote="frame-1", reason="testing")
    assert exc_info.value.code == "no_remote_cancel"


def test_claude_science_refuses_all_permission_operations_and_touches_nothing(monkeypatch):
    # A Claude Science frame has no per-frame filesystem grant concept at
    # all -- the base class's refusal must come through unmodified (this
    # adapter does not override any of the three), and the HTTP API must
    # never be hit for any of them.
    def boom(request):
        raise AssertionError(
            f"Claude Science must not touch the HTTP API for a permission call, got: {request.full_url}"
        )

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    ext = ClaudeScienceExtension(base_url="http://127.0.0.1:8000")

    with pytest.raises(Rejected) as exc_info:
        ext.get_permissions(partner_id_in_remote="frame-1")
    assert exc_info.value.code == "not_path_configurable"

    with pytest.raises(Rejected) as exc_info:
        ext.add_permissions(partner_id_in_remote="frame-1", rules=["read_file(/a)"])
    assert exc_info.value.code == "not_path_configurable"

    with pytest.raises(Rejected) as exc_info:
        ext.delete_permissions(partner_id_in_remote="frame-1", rules=["read_file(/a)"])
    assert exc_info.value.code == "not_path_configurable"


# ---------------------------------------------------------------------------
# Antigravity
# ---------------------------------------------------------------------------


def test_antigravity_verify_project_system_id_uses_is_dir(tmp_path):
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    assert ext.verify_project_system_id(str(tmp_path)) is True
    assert ext.verify_project_system_id(str(tmp_path / "does-not-exist")) is False


def test_antigravity_session_name_truncates_conversation_id_to_8_chars(monkeypatch):
    # Real `sessionFor = (id) => `agy-${id.slice(0, 8)}`` in index.js --
    # the previously guessed `agy-<full id>` was wrong for any id longer
    # than 8 characters.
    seen = []

    def fake_run(cmd, capture_output=True, text=True):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    ext.verify_partner_id_in_remote("/some/folder", "176197cc-dead-beef-0000-000000000000")

    assert any("agy-176197cc" in part for cmd in seen for part in cmd)
    assert not any("agy-176197cc-dead" in part for cmd in seen for part in cmd)


def test_antigravity_poll_completion_raises_on_approval_prompt(monkeypatch):
    def fake_run(cmd, capture_output=True, text=True):
        if "capture-pane" in cmd:
            # Real prompt header from lib/control.js's PROMPT_HEADERS --
            # not the previously guessed "Allow this action to proceed?".
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout="Do you want to proceed?\n> 1. Yes\n  2. No\nesc to cancel", stderr=""
            )
        raise AssertionError(f"unexpected tmux invocation: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")

    with pytest.raises(Rejected) as exc_info:
        ext.poll_completion(partner_id_in_remote="conv-1")

    assert exc_info.value.code == "approval_is_an_error"
    message = str(exc_info.value).lower()
    # The message must name interrupt+add_permissions as the remedy, not
    # claim this extension answers the prompt. There is no "resume" any
    # more -- correcting the grant and sending again IS the resumption.
    assert "interrupt" in message
    assert "add_permissions" in message
    assert "never a question" in message


def test_antigravity_poll_completion_false_when_busy(monkeypatch):
    def fake_run(cmd, capture_output=True, text=True):
        if "capture-pane" in cmd:
            # Real busy footer from lib/control.js's FOOTER_BUSY -- not the
            # previously guessed "Thinking...".
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="some reply\nesc to cancel\n", stderr="")
        raise AssertionError(f"unexpected tmux invocation: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    assert ext.poll_completion(partner_id_in_remote="conv-1") is False


def test_antigravity_poll_completion_true_when_idle(monkeypatch):
    def fake_run(cmd, capture_output=True, text=True):
        if "capture-pane" in cmd:
            # Real idle footer from lib/control.js's FOOTER_IDLE -- not the
            # previously guessed bare "$ " shell prompt (agy's own TUI, not
            # a shell, is what's actually in this pane).
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="? for shortcuts\n", stderr="")
        raise AssertionError(f"unexpected tmux invocation: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    assert ext.poll_completion(partner_id_in_remote="conv-1") is True


def test_antigravity_deliver_message_sends_literal_text_then_enter(monkeypatch):
    seen = []

    def fake_run(cmd, capture_output=True, text=True):
        seen.append(cmd)
        if "capture-pane" in cmd:
            # A session that is ready before the keystrokes and busy after
            # them. Both halves are required now: delivery refuses a pane that
            # has not reached an input prompt, because typing into agy's trust
            # dialog put the message nowhere and every check afterwards read
            # as success.
            typed = any("send-keys" in c for c in seen)
            return subprocess.CompletedProcess(
                cmd, returncode=0,
                stdout="esc to cancel" if typed else "? for shortcuts", stderr="",
            )
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    remote_id = ext.deliver_message(partner_id_in_remote="conv-1", behavior="[QUERY]", body="hello there")

    assert isinstance(remote_id, str) and remote_id
    send_keys_calls = [c for c in seen if "send-keys" in c]
    # Real agy_send is TWO separate tmux calls: literal text (`-l`) first,
    # then Enter on its own -- not one call with the message and "Enter" as
    # trailing arguments (that was a guess).
    assert len(send_keys_calls) == 2
    assert "-l" in send_keys_calls[0]
    assert "hello there" in send_keys_calls[0]
    assert send_keys_calls[1][-1] == "Enter"
    assert "hello there" not in send_keys_calls[1]


def test_antigravity_stop_remote_execution_sends_escape_not_ctrl_c(monkeypatch):
    seen = []

    def fake_run(cmd, capture_output=True, text=True):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    ext.stop_remote_execution(partner_id_in_remote="conv-1", reason="testing")

    # Real index.js has no interrupt/cancel tool and no C-c send anywhere;
    # the one concrete real signal is lib/control.js's own footer text
    # labelling "esc to cancel" -- not the previously guessed "C-c".
    assert any(cmd[-1] == "Escape" for cmd in seen)
    assert not any("C-c" in cmd for cmd in seen)


# ---------------------------------------------------------------------------
# Antigravity permissions
# ---------------------------------------------------------------------------


def _agy_session(partner_id_in_remote: str) -> str:
    """The real truncated session name a conversation id maps onto."""
    return f"agy-{partner_id_in_remote[:8]}"


def _tmux_fake(responses: dict[tuple, subprocess.CompletedProcess] | None = None, seen: list | None = None):
    """A deterministic stand-in for `subprocess.run`, keyed by the args.

    `responses` maps an argv tuple -- everything AFTER the tmux binary path,
    e.g. `("capture-pane", "-t", "agy-conv1234", "-p")` -- to the exact
    `CompletedProcess` a test wants returned for that call. Anything not
    listed succeeds with an empty pane (`returncode=0, stdout=""`), which is
    all a `send-keys`/`Escape` call whose return value nobody is asserting on
    needs.
    """
    responses = responses or {}

    def fake_run(cmd, capture_output=True, text=True):
        if seen is not None:
            seen.append(cmd)
        key = tuple(cmd[1:])
        if key in responses:
            return responses[key]
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    return fake_run


@pytest.fixture
def antigravity_project_paths(tmp_path, monkeypatch):
    """Point the adapter's own project-id/config-dir constants at tmp_path.

    Patches the module-level constants the adapter builds its paths from
    (`_PROJECT_ID_FILE`, `_PROJECT_CONFIG_DIR`) rather than
    `os.path.expanduser` globally, so a test can still tell "the id file" and
    "the config dir" apart and write to each independently.
    """
    id_file = tmp_path / "default_project_id.txt"
    config_dir = tmp_path / "projects"
    config_dir.mkdir()
    monkeypatch.setattr(antigravity_adapter, "_PROJECT_ID_FILE", str(id_file))
    monkeypatch.setattr(antigravity_adapter, "_PROJECT_CONFIG_DIR", str(config_dir))
    return id_file, config_dir


def _no_tmux(monkeypatch):
    """Fail loudly if get_permissions ever shells out -- it must only read files."""

    def boom(cmd, capture_output=True, text=True):
        raise AssertionError(f"get_permissions must not touch tmux, got: {cmd}")

    monkeypatch.setattr(subprocess, "run", boom)


def test_antigravity_get_permissions_missing_config_file_returns_empty(monkeypatch, antigravity_project_paths):
    id_file, _config_dir = antigravity_project_paths
    id_file.write_text("proj-1")
    _no_tmux(monkeypatch)

    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    # A fresh install, not an error: the file simply doesn't exist yet.
    assert ext.get_permissions(partner_id_in_remote="conv-1") == []


def test_antigravity_get_permissions_missing_id_file_raises_project_unknown(monkeypatch, antigravity_project_paths):
    # id_file is never written -- it does not exist at all.
    _no_tmux(monkeypatch)

    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    with pytest.raises(Rejected) as exc_info:
        ext.get_permissions(partner_id_in_remote="conv-1")
    assert exc_info.value.code == "antigravity_project_unknown"


def test_antigravity_get_permissions_empty_id_file_raises_project_unknown(monkeypatch, antigravity_project_paths):
    id_file, _config_dir = antigravity_project_paths
    id_file.write_text("")
    _no_tmux(monkeypatch)

    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    with pytest.raises(Rejected) as exc_info:
        ext.get_permissions(partner_id_in_remote="conv-1")
    assert exc_info.value.code == "antigravity_project_unknown"


def test_antigravity_get_permissions_unreadable_id_file_raises_project_unknown(
    monkeypatch, antigravity_project_paths
):
    id_file, _config_dir = antigravity_project_paths
    # A directory where a file is expected: read_text() raises an OSError
    # subclass (IsADirectoryError) distinct from a plain missing file, and
    # both must land on the same refusal.
    id_file.mkdir()
    _no_tmux(monkeypatch)

    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    with pytest.raises(Rejected) as exc_info:
        ext.get_permissions(partner_id_in_remote="conv-1")
    assert exc_info.value.code == "antigravity_project_unknown"


def test_antigravity_get_permissions_malformed_json_raises_project_unreadable(monkeypatch, antigravity_project_paths):
    id_file, config_dir = antigravity_project_paths
    id_file.write_text("proj-1")
    (config_dir / "proj-1.json").write_text("{not valid json")
    _no_tmux(monkeypatch)

    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    with pytest.raises(Rejected) as exc_info:
        ext.get_permissions(partner_id_in_remote="conv-1")
    # Malformed JSON is a different failure from "no file yet" -- refuses
    # rather than guessing what it allows, and reports it distinctly.
    assert exc_info.value.code == "antigravity_project_unreadable"


# ---------------------------------------------------------------------------
# Antigravity permissions.
#
# Every pane below is reproduced from a real `/permissions` session, not
# invented. That matters: the first version of these tests was written against
# a GUESSED key sequence, passed cleanly, and described something the CLI does
# not do. Driving it live corrected three things at once -- the store key, the
# number of Enters, and how many Escapes it takes to get out again.
# ---------------------------------------------------------------------------

#: Screen 1. `/permissions` + Enter lands here, NOT on the rule list.
SCOPE_SELECTOR_PANE = """\
────────────────────────────────────────────────────────────────────────────────
>
────────────────────────────────────────────────────────────────────────────────
Permission Config Editor

Select a config scope to edit:

> Project
  Shared with Antigravity
  Global

  Rules that apply only to this project (highest priority)

Keyboard: ↑/↓ Navigate  enter Save  esc Close
"""

#: Screen 3, reached with `a` from the rule list.
ADD_RULE_PANE = """\
────────────────────────────────────────────────────────────────────────────────
>
────────────────────────────────────────────────────────────────────────────────
Add Rule — Project — allowlist

Enter a permission rule:



Format: action(target)

Keyboard: ↑/↓ Navigate  enter Select  esc Close
"""

#: The idle chat prompt, which is what closing the editor must get back to.
IDLE_PANE = """\
────────────────────────────────────────────────────────────────────────────────
>
────────────────────────────────────────────────────────────────────────────────
? for shortcuts                                          Gemini 3.7 Flash · high
"""


def rule_list_pane(rules: list[str], selected: int = 0) -> str:
    """Screen 2, the rule list, with `>` on `rules[selected]`.

    Note the second line: the chat prompt is ALSO a line beginning with `>`.
    Any parser that scans the whole pane for `>` counts it as a rule and is
    off by one from then on, which is why `_listed_rules` is bounded by the
    `allowlist (` header and the `Keyboard:` footer.
    """
    body = "\n".join(
        f"{'>' if i == selected else ' '} {rule}" for i, rule in enumerate(rules)
    ) or "No allowlist rules."
    return f"""\
────────────────────────────────────────────────────────────────────────────────
>
────────────────────────────────────────────────────────────────────────────────
Permissions — Project
 allowlist ({len(rules)})    denylist (0)    asklist (0)   (←/→ to switch)

{body}

Keyboard: ↑/↓ Navigate  ←/→ Switch View  a Add rule  e Edit rule  d/⌫ Delete rul
"""


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    """`_await_pane` and the key-settle delays would otherwise cost real seconds."""
    monkeypatch.setattr(antigravity_adapter.time, "sleep", lambda _s: None)


def _scripted_tmux(panes, seen: list):
    """A `subprocess.run` stand-in whose capture-pane answers follow a script.

    `panes` is a list consumed one entry per `capture-pane`; the last entry
    repeats once exhausted, so a test only has to describe the screens it
    cares about rather than counting how many times the adapter looks.
    """
    remaining = list(panes)

    def fake_run(cmd, capture_output=True, text=True):
        seen.append(cmd)
        if "capture-pane" in cmd:
            pane = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=pane, stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    return fake_run


def _cursor_tmux(rules: list[str], seen: list, scope_first: bool = True):
    """A fake whose rule-list cursor MOVES when Up/Down are sent.

    A fake that hands back a pane with the cursor already on the target makes a
    navigation test pass without any navigation happening — which is how the
    first version of these tests reported success for zero keypresses. The
    cursor here starts at 0 and tracks Up/Down, so the number of presses is a
    real measurement.
    """
    state = {"selected": 0, "scope_shown": not scope_first, "rules": list(rules)}

    def fake_run(cmd, capture_output=True, text=True):
        seen.append(cmd)
        if "capture-pane" in cmd:
            if not state["scope_shown"]:
                state["scope_shown"] = True
                return subprocess.CompletedProcess(cmd, 0, SCOPE_SELECTOR_PANE, "")
            if state.get("closed"):
                return subprocess.CompletedProcess(cmd, 0, IDLE_PANE, "")
            return subprocess.CompletedProcess(
                cmd, 0, rule_list_pane(state["rules"], selected=state["selected"]), ""
            )
        if "send-keys" in cmd:
            key = cmd[-1]
            if key == "Down":
                state["selected"] = min(state["selected"] + 1, max(len(state["rules"]) - 1, 0))
            elif key == "Up":
                state["selected"] = max(state["selected"] - 1, 0)
            elif key == "d" and state["rules"]:
                state["rules"].pop(state["selected"])
                state["selected"] = min(state["selected"], max(len(state["rules"]) - 1, 0))
            elif key == "Escape":
                state["closed"] = True
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return fake_run, state


def _keys(seen: list, session: str) -> list[str]:
    """Just the keystrokes sent, in order, as plain strings."""
    out = []
    for cmd in seen:
        if "send-keys" not in cmd:
            continue
        args = cmd[cmd.index("send-keys") + 1 :]
        args = [a for a in args if a not in ("-t", session)]
        out.append(" ".join(args))
    return out


# -- get_permissions: the store shape ---------------------------------------


def test_antigravity_get_permissions_reads_the_doubly_nested_allow_list(
    monkeypatch, antigravity_project_paths
):
    """The allowlist lives at permissionGrants.permissionGrants.allow.

    Not `projectResources`, which stays `{}` forever. An earlier version read
    that key and was "confirmed" by seeing `{}` beside a TUI reading
    `allowlist (0)` -- empty matching empty, which is evidence of nothing. The
    moment a rule existed the TUI read `allowlist (1)` and the adapter still
    returned `[]`.
    """
    id_file, config_dir = antigravity_project_paths
    id_file.write_text("proj-1")
    (config_dir / "proj-1.json").write_text(
        json.dumps(
            {
                "id": "proj-1",
                "projectResources": {},
                "permissionGrants": {
                    "permissionGrants": {
                        "allow": ["write_file(/mnt/c/Data/tet-dit)", "read_file(/refs)"]
                    }
                },
            }
        )
    )
    _no_tmux(monkeypatch)

    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    assert ext.get_permissions(partner_id_in_remote="conv-1") == [
        "write_file(/mnt/c/Data/tet-dit)",
        "read_file(/refs)",
    ], "the allowlist must come from permissionGrants, not projectResources"


def test_antigravity_get_permissions_accepts_a_singly_nested_allow_list(
    monkeypatch, antigravity_project_paths
):
    """The outer wrapper is unwrapped if present, not assumed.

    A future agy dropping the doubly-nested form must not read as "no rules" --
    silently returning `[]` for a partner that is in fact fully granted is the
    worst possible failure here, because it looks exactly like a fresh install.
    """
    id_file, config_dir = antigravity_project_paths
    id_file.write_text("proj-1")
    (config_dir / "proj-1.json").write_text(
        json.dumps({"permissionGrants": {"allow": ["read_file(/x)"]}})
    )
    _no_tmux(monkeypatch)

    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    assert ext.get_permissions(partner_id_in_remote="conv-1") == ["read_file(/x)"]


def test_antigravity_get_permissions_absent_grants_is_no_rules_not_an_error(
    monkeypatch, antigravity_project_paths
):
    """A project that has never been granted anything has no key at all."""
    id_file, config_dir = antigravity_project_paths
    id_file.write_text("proj-1")
    (config_dir / "proj-1.json").write_text(json.dumps({"id": "proj-1", "projectResources": {}}))
    _no_tmux(monkeypatch)

    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    assert ext.get_permissions(partner_id_in_remote="conv-1") == []


@pytest.mark.parametrize(
    "config",
    [
        {"permissionGrants": ["not", "an", "object"]},
        {"permissionGrants": {"permissionGrants": {"allow": "not-a-list"}}},
        ["a", "top", "level", "array"],
    ],
    ids=["grants-is-a-list", "allow-is-a-string", "top-level-array"],
)
def test_antigravity_get_permissions_refuses_a_shape_it_cannot_read(
    monkeypatch, antigravity_project_paths, config
):
    """Valid JSON of the wrong shape refuses; it does not crash or guess.

    A raw `AttributeError` here would surface inside `add_permissions`'
    verify-after-write step, where it would read as "the grant did not land"
    rather than "the config file is wrong" -- two problems with opposite fixes.
    """
    id_file, config_dir = antigravity_project_paths
    id_file.write_text("proj-1")
    (config_dir / "proj-1.json").write_text(json.dumps(config))
    _no_tmux(monkeypatch)

    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    with pytest.raises(Rejected) as exc_info:
        ext.get_permissions(partner_id_in_remote="conv-1")
    assert exc_info.value.code == "antigravity_project_unreadable", (
        f"expected antigravity_project_unreadable, got {exc_info.value.code!r}"
    )


# -- add_permissions: the real key sequence ----------------------------------


def test_antigravity_add_permissions_walks_all_three_screens_in_order(monkeypatch):
    """/permissions, Enter, Enter, a, <rule literally>, Enter -- then escape out.

    The SECOND Enter is the one worth guarding. `/permissions` + Enter only
    picks the command out of the palette and lands on the scope selector; an
    adapter that stopped there would believe it was on the rule list, and its
    next `a` would go somewhere unintended.
    """
    session = _agy_session("conv1234-dead-beef")
    seen: list = []
    monkeypatch.setattr(
        subprocess,
        "run",
        _scripted_tmux(
            [
                SCOPE_SELECTOR_PANE,                 # after /permissions + Enter
                rule_list_pane([]),                  # after the second Enter
                ADD_RULE_PANE,                       # after `a`
                rule_list_pane(["write_file(/a)"]),  # after the rule + Enter
                rule_list_pane(["write_file(/a)"]),  # closing looks first: still in the editor
                IDLE_PANE,                           # and only then is it back at the prompt
            ],
            seen,
        ),
    )

    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    assert ext.add_permissions(partner_id_in_remote="conv1234-dead-beef",
                               rules=["write_file(/a)"]) is None

    keys = _keys(seen, session)
    assert keys[:5] == [
        "-l /permissions",
        "Enter",
        "Enter",
        "a",
        "-l write_file(/a)",
    ], f"wrong key sequence: {keys}"
    assert "Enter" in keys[5], f"the rule must be confirmed with Enter, got {keys[5:]}"
    assert keys[-1] == "Escape", f"the editor must be closed, got {keys}"


def test_antigravity_add_permissions_sends_the_rule_literally(monkeypatch):
    """`-l`, so the parentheses are typed rather than read as key names."""
    session = _agy_session("conv1234-dead-beef")
    seen: list = []
    monkeypatch.setattr(
        subprocess,
        "run",
        _scripted_tmux(
            [SCOPE_SELECTOR_PANE, rule_list_pane([]), ADD_RULE_PANE,
             rule_list_pane(["write_file(/a)"]), IDLE_PANE],
            seen,
        ),
    )

    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    ext.add_permissions(partner_id_in_remote="conv1234-dead-beef", rules=["write_file(/a)"])

    rule_sends = [
        cmd for cmd in seen if "send-keys" in cmd and "write_file(/a)" in cmd
    ]
    assert rule_sends, "the rule was never sent"
    assert "-l" in rule_sends[0], f"the rule must be sent with -l, got {rule_sends[0]}"


@pytest.mark.parametrize(
    "panes, stopped_at",
    [
        ([IDLE_PANE], "the scope selector"),
        ([SCOPE_SELECTOR_PANE, SCOPE_SELECTOR_PANE], "the rule list"),
        ([SCOPE_SELECTOR_PANE, rule_list_pane([]), rule_list_pane([])], "the add screen"),
    ],
    ids=["no-scope-selector", "no-rule-list", "no-add-screen"],
)
def test_antigravity_add_permissions_refuses_at_every_screen_and_types_no_rule(
    monkeypatch, panes, stopped_at
):
    """Three distinct places the walk can go wrong, and none of them types a rule.

    Typing a permission rule into a screen that is not the rule input posts it
    into the CHAT, to an agent that would then try to act on it. So each step
    is confirmed before the next key, and a screen that never appears is a
    refusal rather than a best guess.
    """
    session = _agy_session("conv1234-dead-beef")
    seen: list = []
    monkeypatch.setattr(subprocess, "run", _scripted_tmux(panes, seen))

    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    with pytest.raises(Rejected) as exc_info:
        ext.add_permissions(partner_id_in_remote="conv1234-dead-beef", rules=["write_file(/a)"])
    assert exc_info.value.code == "permissions_view_did_not_open", (
        f"expected a refusal at {stopped_at}, got {exc_info.value.code!r}"
    )

    typed = [cmd for cmd in seen if "send-keys" in cmd and "write_file(/a)" in cmd]
    assert not typed, f"a rule was typed after failing to reach {stopped_at}: {typed}"


def test_antigravity_add_permissions_escapes_out_even_when_typing_raises(monkeypatch):
    """The `finally` matters more than the failure it wraps.

    A session left sitting in the permissions editor reads the next
    `deliver_message` as editor input -- the message is swallowed and nothing
    downstream reports a thing. A failed permission change is loud; a swallowed
    message is not.
    """
    session = _agy_session("conv1234-dead-beef")
    seen: list = []
    scripted = _scripted_tmux(
        [SCOPE_SELECTOR_PANE, rule_list_pane([]), ADD_RULE_PANE,
         ADD_RULE_PANE,   # closing looks: still inside the editor, so Escape is needed
         IDLE_PANE],
        seen,
    )

    def failing(cmd, capture_output=True, text=True):
        if "send-keys" in cmd and "write_file(/a)" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="pane gone")
        return scripted(cmd, capture_output=capture_output, text=text)

    monkeypatch.setattr(subprocess, "run", failing)

    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    with pytest.raises(Rejected):
        ext.add_permissions(partner_id_in_remote="conv1234-dead-beef", rules=["write_file(/a)"])
    assert "Escape" in _keys(seen, session), (
        f"the editor was left open after a failure; keys were {_keys(seen, session)}"
    )


def test_antigravity_add_permissions_never_claims_success(monkeypatch):
    """It returns None and never reads the config back.

    Verification is the caller's job (`MessagingCore._apply_and_verify`). A
    return value here would be the remote's opinion of its own success, which
    is the thing being verified.
    """
    session = _agy_session("conv1234-dead-beef")
    seen: list = []
    monkeypatch.setattr(
        subprocess,
        "run",
        _scripted_tmux(
            [SCOPE_SELECTOR_PANE, rule_list_pane([]), ADD_RULE_PANE,
             rule_list_pane(["write_file(/a)"]), IDLE_PANE],
            seen,
        ),
    )
    read_calls: list = []
    monkeypatch.setattr(
        AntigravityExtension, "_read_project_rules",
        lambda self: read_calls.append(1) or [],
    )

    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    assert ext.add_permissions(partner_id_in_remote="conv1234-dead-beef",
                               rules=["write_file(/a)"]) is None
    assert not read_calls, "add_permissions must not verify itself"


# -- delete_permissions: navigate, never blind-press -------------------------


@pytest.mark.parametrize(
    "target, expected_downs",
    [("read_file(/a)", 0), ("write_file(/b)", 1), ("read_file(/c)", 2)],
    ids=["first", "second", "third"],
)
def test_antigravity_delete_permissions_navigates_to_the_named_rule(
    monkeypatch, target, expected_downs
):
    """`d` deletes the SELECTED rule with no confirmation, so selection is everything.

    Pressing `d` once per rule -- which an earlier version did -- deletes
    whichever rules happen to be highlighted, not the ones asked for.
    """
    session = _agy_session("conv1234-dead-beef")
    rules = ["read_file(/a)", "write_file(/b)", "read_file(/c)"]
    seen: list = []
    fake, state = _cursor_tmux(rules, seen)
    monkeypatch.setattr(subprocess, "run", fake)

    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    ext.delete_permissions(partner_id_in_remote="conv1234-dead-beef", rules=[target])

    keys = _keys(seen, session)
    assert keys.count("Down") == expected_downs, (
        f"deleting {target!r} should send {expected_downs} Down(s), got {keys}"
    )
    assert keys.count("d") == 1, f"exactly one delete press expected, got {keys}"
    assert target not in state["rules"], (
        f"{target!r} is still listed; the wrong rule was deleted. Remaining: {state['rules']}"
    )
    assert len(state["rules"]) == 2, f"exactly one rule should be gone, got {state['rules']}"


def test_antigravity_delete_permissions_refuses_if_the_cursor_will_not_move(monkeypatch):
    """Refuses rather than deleting whatever is selected instead.

    Deleting the wrong permission is worse than deleting none: the caller is
    told the revocation succeeded, and a grant it believes gone is still live.
    """
    session = _agy_session("conv1234-dead-beef")
    rules = ["read_file(/a)", "write_file(/b)"]
    seen: list = []
    fake, _state = _cursor_tmux(rules, seen)

    def stuck(cmd, capture_output=True, text=True):
        # Swallow the navigation keys: the cursor never leaves the first rule.
        if "send-keys" in cmd and cmd[-1] in ("Down", "Up"):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return fake(cmd, capture_output=capture_output, text=text)

    monkeypatch.setattr(subprocess, "run", stuck)

    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    with pytest.raises(Rejected) as exc_info:
        ext.delete_permissions(partner_id_in_remote="conv1234-dead-beef",
                               rules=["write_file(/b)"])
    assert exc_info.value.code == "permissions_view_did_not_open"
    assert "d" not in _keys(seen, session), "a delete was pressed on the wrong rule"


def test_antigravity_delete_permissions_skips_a_rule_that_is_not_listed(monkeypatch):
    """Revoking something not held is not an error, and presses nothing."""
    session = _agy_session("conv1234-dead-beef")
    seen: list = []
    monkeypatch.setattr(
        subprocess,
        "run",
        _scripted_tmux([SCOPE_SELECTOR_PANE, rule_list_pane(["read_file(/a)"]), IDLE_PANE], seen),
    )

    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    ext.delete_permissions(partner_id_in_remote="conv1234-dead-beef",
                           rules=["write_file(/nowhere)"])
    keys = _keys(seen, session)
    assert "d" not in keys, f"pressed delete for a rule that was not listed: {keys}"


# -- closing, and pane parsing ------------------------------------------------


def test_antigravity_close_refuses_when_it_cannot_get_back_to_the_prompt(monkeypatch):
    """A session stuck in the editor is reported, not ignored.

    Reported because the alternative is silent: everything sent to that session
    afterwards is read as editor input, so the next message is swallowed and no
    error appears anywhere.
    """
    session = _agy_session("conv1234-dead-beef")
    seen: list = []
    monkeypatch.setattr(subprocess, "run", _scripted_tmux([SCOPE_SELECTOR_PANE], seen))

    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    with pytest.raises(Rejected) as exc_info:
        ext._close_permissions_view(session)
    assert exc_info.value.code == "antigravity_session_stuck_in_editor"
    assert _keys(seen, session).count("Escape") >= 1, "it did not even try"


def test_antigravity_listed_rules_ignores_the_chat_prompt_line():
    """The chat prompt is also a line starting with `>`.

    An unscoped scan counts it as a rule and every index after it is off by
    one -- which, for a key that deletes without confirmation, means deleting
    the wrong permission.
    """
    pane = rule_list_pane(["read_file(/a)", "write_file(/b)"], selected=1)
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    assert ext._listed_rules(pane) == ["read_file(/a)", "write_file(/b)"]
    # One argument, not two: the walk derives the list from the same pane it
    # scans, so a separately-computed `rules` could only ever disagree with it.
    assert ext._selected_index(pane) == 1


def test_antigravity_listed_rules_is_empty_when_there_are_none():
    pane = rule_list_pane([])
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    assert ext._listed_rules(pane) == []


def test_antigravity_deliver_message_waits_for_the_turn_to_actually_start(monkeypatch):
    """deliver_message must not return while the pane still shows the idle footer.

    The bug this pins was found live and was silent. `deliver_message` returned
    the instant Enter was pressed, before the TUI had repainted out of its idle
    state -- so the drain thread's very next `poll_completion` read a stale idle
    footer, declared the turn FINISHED, and closed a task the agent had not yet
    begun. The caller was handed a placeholder while the pane went on to show
    the real answer that nobody was waiting for any more. Nothing raised.

    A timeout is deliberately NOT an error: a turn short enough to finish inside
    the window never shows a busy footer, and returning is right for that case.
    """
    session = _agy_session("conv1234-dead-beef")
    seen: list = []
    # Idle for the first two captures, then busy -- i.e. the TUI repaints late.
    panes = [IDLE_PANE, IDLE_PANE,
             "output\nesc to cancel                        Gemini 3.7 Flash · high\n"]
    monkeypatch.setattr(subprocess, "run", _scripted_tmux(panes, seen))

    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    ext.deliver_message(partner_id_in_remote="conv1234-dead-beef",
                        behavior="[QUERY]", body="do the thing")

    captures = [c for c in seen if "capture-pane" in c]
    assert len(captures) >= 3, (
        "deliver_message returned without waiting for the pane to go busy; it made "
        f"{len(captures)} capture(s), so the next poll_completion would read a stale "
        "idle footer and close the task before the agent started"
    )


def test_antigravity_deliver_message_returns_when_the_turn_is_too_fast_to_observe(monkeypatch):
    """A pane that never goes busy is not an error -- the answer is already there."""
    session = _agy_session("conv1234-dead-beef")
    seen: list = []
    monkeypatch.setattr(subprocess, "run", _scripted_tmux([IDLE_PANE], seen))

    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    rid = ext.deliver_message(partner_id_in_remote="conv1234-dead-beef",
                              behavior="[QUERY]", body="quick")
    assert rid, "deliver_message must still return a remote call id"


# ---------------------------------------------------------------------------
# read_remote_result: what a Caller actually ends up reading
# ---------------------------------------------------------------------------
#
# Before these were implemented, `PollingServer._read_result` fell back to the
# literal placeholder "[result reported by the remote through its own channel]"
# for every science_ and gemini_ turn -- and because [TRUTHFUL-REPORT] and
# [MESSAGE-RESPONSE] are stored, that placeholder was what `read` returned. The
# answer existed on the remote and never reached the agent that asked for it.


def test_claude_science_read_remote_result_returns_the_trailing_assistant_run(monkeypatch):
    """A reply can span several assistant messages before the frame yields.

    Stopping at the last one would return only the closing sentence of an
    answer whose substance was in the message before it.
    """
    captured: dict = {}

    def fake_urlopen(request):
        captured["request"] = request
        return _FakeHTTPResponse(200, {"messages": [
            {"role": "user", "content": [{"type": "text", "text": "an older question"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "an older answer"}]},
            {"role": "user", "content": [{"type": "text", "text": "summarize the work"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "I measured 41.2ms."}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Evidence: bench.json."}]},
        ]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ext = ClaudeScienceExtension()

    result = ext.read_remote_result(partner_id_in_remote="frame-1")

    assert result == "I measured 41.2ms.\n\nEvidence: bench.json.", (
        f"expected the whole trailing assistant run in order, got: {result!r}"
    )
    assert "an older answer" not in result, (
        "anything at or before the last user message belongs to a previous exchange"
    )
    assert "/api/frames/frame-1/messages" in captured["request"].full_url


def test_claude_science_read_remote_result_drops_harness_injected_messages(monkeypatch):
    """`_harness_notice` marks Claude Science's own runtime context injection.

    A skill-discovery dump or a [Memory] recall block is not the agent
    speaking, and returning one as the result hands the Caller the app's
    bookkeeping in place of its answer.
    """
    def fake_urlopen(request):
        return _FakeHTTPResponse(200, [
            {"role": "user", "content": [{"type": "text", "text": "do the work"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "the real answer"}]},
            {"role": "assistant", "_harness_notice": True,
             "content": [{"type": "text", "text": "[Memory] recalled 4 items"}]},
        ])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ext = ClaudeScienceExtension()

    result = ext.read_remote_result(partner_id_in_remote="frame-1")

    assert result == "the real answer", f"got: {result!r}"


def test_claude_science_read_remote_result_is_empty_not_an_error_when_nothing_was_said(monkeypatch):
    """A turn that ended in a tool call said nothing, and that is a real answer."""
    def fake_urlopen(request):
        return _FakeHTTPResponse(200, {"messages": []})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ext = ClaudeScienceExtension()
    assert ext.read_remote_result(partner_id_in_remote="frame-1") == ""


def test_antigravity_read_remote_result_slices_after_the_echoed_prompt(monkeypatch):
    """The pane holds the whole transcript; only this turn's part is the result.

    agy echoes what was typed into it, and that echo is the only marker
    available -- there is no API and no delimiter. The LAST echo is the one
    that matters: the same text can appear earlier, quoted back by agy itself.
    """
    pane = "\n".join([
        "Reply with PROVEN",
        "PROVEN",
        "Reply with PROVEN",
        "PROVEN, and here is why: the benchmark ran clean.",
        "",
        "╰──────────────╯",
        "? for shortcuts",
    ])

    seen: list[list[str]] = []

    def fake_run(cmd, capture_output=True, text=True):
        seen.append(cmd)
        if "capture-pane" in cmd:
            if "-S" in cmd:
                # The scrollback read read_remote_result makes.
                return subprocess.CompletedProcess(cmd, returncode=0, stdout=pane, stderr="")
            # Ready before the keystrokes, busy after: delivery now refuses a
            # session that has not reached an input prompt.
            typed = any("send-keys" in c for c in seen)
            return subprocess.CompletedProcess(
                cmd, returncode=0,
                stdout="esc to cancel" if typed else "? for shortcuts", stderr="",
            )
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    ext.deliver_message(partner_id_in_remote="conv-1", behavior="[QUERY]", body="Reply with PROVEN")

    result = ext.read_remote_result(partner_id_in_remote="conv-1")

    assert result == "PROVEN, and here is why: the benchmark ran clean.", (
        f"expected only what followed the LAST prompt echo, with chrome stripped; got: {result!r}"
    )


def test_antigravity_read_remote_result_keeps_the_whole_pane_when_it_cannot_anchor(monkeypatch):
    """Degrading to the whole pane is deliberate.

    With no recorded delivery there is nothing to anchor a narrower slice to,
    and a guessed slice would silently drop the answer. A whole pane is a poor
    result but a real one.
    """
    def fake_run(cmd, capture_output=True, text=True):
        if "capture-pane" in cmd:
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout="some answer text\n? for shortcuts", stderr=""
            )
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")

    assert ext.read_remote_result(partner_id_in_remote="conv-1") == "some answer text"


def test_antigravity_read_remote_result_asks_for_scrollback(monkeypatch):
    """An answer longer than the visible pane must not be silently truncated."""
    seen: list[list[str]] = []

    def fake_run(cmd, capture_output=True, text=True):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="answer", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    ext.read_remote_result(partner_id_in_remote="conv-1")

    assert any("-S" in cmd for cmd in seen), (
        f"read_remote_result must capture scrollback, not just the visible screen; ran: {seen}"
    )


# ---------------------------------------------------------------------------
# Delivering into a TUI that is not ready
# ---------------------------------------------------------------------------
#
# Found live. A fresh agy session in a folder agy has not seen before paints a
# banner and then a modal trust prompt; `tmux has-session` succeeds the instant
# the session exists, so `create_partner` registers a conversation that cannot
# receive anything. The first delivery typed the Caller's message into that
# menu, `_await_busy` timed out and returned (a timeout there is deliberately
# not an error), `poll_completion` saw no busy footer and reported the turn
# finished, and `read_remote_result` fell back to the whole pane -- so the
# Caller received agy's startup banner stored as its answer. Nothing errored.


def _agy_pane_runner(panes):
    """A fake tmux whose capture-pane returns each pane in turn, then the last."""
    state = {"i": 0}

    def fake_run(cmd, capture_output=True, text=True):
        if "capture-pane" in cmd:
            i = min(state["i"], len(panes) - 1)
            state["i"] += 1
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=panes[i], stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    return fake_run, state


def test_antigravity_refuses_to_type_into_a_trust_prompt(monkeypatch):
    """A trust prompt is an approval, and this extension never answers one.

    Raising `approval_is_an_error` routes it into the path that already
    exists: the Polling Server reports it to the Caller as an [ERROR] naming
    the remedy. Typing into the menu instead loses the message silently.
    """
    trust_pane = (
        "Do you trust the contents of this project?\n"
        "Antigravity CLI requires permission to read, edit, and execute files here.\n"
        "> Yes, I trust this folder\n  No, exit\n"
    )
    fake_run, _ = _agy_pane_runner([trust_pane])
    monkeypatch.setattr(subprocess, "run", fake_run)
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")

    with pytest.raises(Rejected) as exc_info:
        ext.deliver_message(partner_id_in_remote="conv-1", behavior="[QUERY]", body="hello")

    assert exc_info.value.code == "approval_is_an_error", (
        f"a trust prompt must be reported as an approval, got {exc_info.value.code!r}"
    )


def test_antigravity_sends_nothing_when_it_refuses(monkeypatch):
    """The whole point: the keystrokes must not happen."""
    trust_pane = "Do you trust the contents of this project?\n> Yes, I trust this folder\n"
    sent: list[list[str]] = []

    def fake_run(cmd, capture_output=True, text=True):
        if "send-keys" in cmd:
            sent.append(cmd)
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=trust_pane, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")

    with pytest.raises(Rejected):
        ext.deliver_message(partner_id_in_remote="conv-1", behavior="[QUERY]", body="hello")

    assert sent == [], f"the message was typed into the prompt anyway: {sent}"


def test_antigravity_refuses_a_session_that_never_reaches_a_prompt(monkeypatch):
    """A pane that is neither ready nor prompting is a session still booting.

    Typing into it puts the message nowhere, and every check afterwards reads
    as success.
    """
    fake_run, _ = _agy_pane_runner(["Antigravity CLI 1.1.22\nloading...\n"])
    monkeypatch.setattr(subprocess, "run", fake_run)
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")

    with pytest.raises(Rejected) as exc_info:
        ext.deliver_message(
            partner_id_in_remote="conv-1", behavior="[QUERY]", body="hello",
        )

    assert exc_info.value.code in ("antigravity_session_not_ready", "approval_is_an_error")


def test_antigravity_delivers_once_the_prompt_is_ready(monkeypatch):
    """The wait must not become a new way for a healthy delivery to fail."""
    ready = "? for shortcuts\n"
    busy = "esc to cancel\n"
    panes = [ready, busy, busy]
    fake_run, _ = _agy_pane_runner(panes)
    sent: list[list[str]] = []

    def wrapper(cmd, capture_output=True, text=True):
        if "send-keys" in cmd:
            sent.append(cmd)
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        return fake_run(cmd, capture_output=capture_output, text=text)

    monkeypatch.setattr(subprocess, "run", wrapper)
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")

    call_id = ext.deliver_message(
        partner_id_in_remote="conv-1", behavior="[QUERY]", body="hello"
    )

    assert call_id.startswith("agy-turn-")
    assert any("-l" in cmd for cmd in sent), f"the body was never typed: {sent}"


def test_antigravity_read_remote_result_returns_nothing_when_it_cannot_find_its_own_echo(
    monkeypatch,
):
    """The observed failure: a startup banner stored as an agent's answer.

    A recorded delivery whose echo is absent from the pane is positive
    evidence the pane does not hold that turn. Returning it anyway is
    fabricating a remote's answer, which this module must never do -- distinct
    from the no-recorded-body case, where a whole pane really is the best
    available and is kept.
    """
    ready = "? for shortcuts\n"
    busy = "esc to cancel\n"
    banner = "Antigravity CLI 1.1.22\nsome@user\n? for shortcuts\n"
    seq = [ready, busy, banner, banner, banner]
    fake_run, _ = _agy_pane_runner(seq)
    monkeypatch.setattr(subprocess, "run", fake_run)
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    ext.deliver_message(
        partner_id_in_remote="conv-1", behavior="[QUERY]", body="a prompt never echoed",
    )

    result = ext.read_remote_result(partner_id_in_remote="conv-1")

    assert result == "", (
        f"a pane not containing this turn must not be returned as its answer: {result!r}"
    )


def test_antigravity_read_remote_result_keeps_the_pane_with_no_recorded_delivery(monkeypatch):
    """Unchanged, and deliberately different: nothing to anchor on."""
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, returncode=0, stdout="some answer text\n? for shortcuts", stderr=""))
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")

    assert ext.read_remote_result(partner_id_in_remote="never-delivered") == "some answer text"


# ---------------------------------------------------------------------------
# A turn that was never seen running is not a turn that finished
# ---------------------------------------------------------------------------
#
# Also found live, one step past the readiness gate. The prompt WAS delivered
# -- the pane showed it -- but `_await_busy`'s six-second budget expired before
# the model produced its first token, `poll_completion` read the absent busy
# footer as FINISHED, and the Caller got a [MESSAGE-RESPONSE] with an empty
# body while the agent was still about to answer. From one pane read, "not
# started yet" and "already finished" look identical.


def _agy_seq(panes, *, record=None):
    """A fake tmux serving `panes` in order, then repeating the last."""
    state = {"i": 0}

    def fake_run(cmd, capture_output=True, text=True):
        if record is not None:
            record.append(cmd)
        if "capture-pane" in cmd:
            i = min(state["i"], len(panes) - 1)
            state["i"] += 1
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=panes[i], stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    return fake_run


_READY = "? for shortcuts\n"
_BUSY = "esc to cancel\n"


def _deliver(ext, monkeypatch, panes_after):
    """Drive a delivery to completion, then serve `panes_after` to the poller."""
    panes = [_READY] + list(panes_after)
    monkeypatch.setattr(subprocess, "run", _agy_seq(panes))
    return ext.deliver_message(
        partner_id_in_remote="conv-1", behavior="[QUERY]", body="Reply with PROVEN"
    )


def test_antigravity_does_not_call_an_unstarted_turn_finished(monkeypatch):
    """The observed failure: an empty answer, and the slot released early."""
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    # Never busy: _await_busy exhausts its budget and the pane stays idle,
    # exactly as it did live while the model was still thinking.
    _deliver(ext, monkeypatch, [_READY])

    assert ext.poll_completion(partner_id_in_remote="conv-1") is False, (
        "a turn that was never seen running must not be reported finished -- doing so "
        "releases the working slot and hands the Caller an empty body"
    )


def test_antigravity_calls_it_finished_once_the_turn_was_seen_running(monkeypatch):
    """The busy footer is the evidence, and seeing it once is enough."""
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    _deliver(ext, monkeypatch, [_BUSY, _BUSY])

    # Still working while the footer is up.
    assert ext.poll_completion(partner_id_in_remote="conv-1") is False
    # Footer gone, and the turn was seen running -- that is a real completion.
    monkeypatch.setattr(subprocess, "run", _agy_seq([_READY]))
    assert ext.poll_completion(partner_id_in_remote="conv-1") is True


def test_antigravity_gives_up_waiting_once_the_settle_window_passes(monkeypatch):
    """The hold is bounded. A turn that never starts must not poll forever."""
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux", settle_seconds=0.0)
    _deliver(ext, monkeypatch, [_READY])

    assert ext.poll_completion(partner_id_in_remote="conv-1") is True, (
        "with the settle window elapsed, an idle pane is a finished turn again"
    )


def test_antigravity_session_with_no_recorded_delivery_is_unchanged(monkeypatch):
    """A process that restarted mid-turn has no record, and must not poll forever."""
    monkeypatch.setattr(subprocess, "run", _agy_seq([_READY]))
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")

    assert ext.poll_completion(partner_id_in_remote="never-delivered") is True


def test_antigravity_an_approval_still_fires_before_the_settle_window(monkeypatch):
    """A blocked Partner must be reported immediately, never held.

    Waiting out the settle window on an approval prompt would delay by exactly
    as long the one message that says the Partner cannot continue at all.
    """
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    _deliver(ext, monkeypatch, [_READY])
    monkeypatch.setattr(subprocess, "run", _agy_seq(["Do you want to proceed?\n> 1. Yes\n"]))

    with pytest.raises(Rejected) as exc_info:
        ext.poll_completion(partner_id_in_remote="conv-1")

    assert exc_info.value.code == "approval_is_an_error"


def test_antigravity_a_cancelled_turn_does_not_leave_a_stale_started_flag(monkeypatch):
    """Otherwise the NEXT turn inherits 'already seen running' and closes early."""
    ext = AntigravityExtension(tmux_path="/usr/bin/tmux")
    _deliver(ext, monkeypatch, [_BUSY, _BUSY])
    assert ext.poll_completion(partner_id_in_remote="conv-1") is False

    monkeypatch.setattr(subprocess, "run", _agy_seq([_READY]))
    ext.stop_remote_execution(partner_id_in_remote="conv-1", reason="displaced")

    # A fresh delivery that never goes busy must be held again, not waved
    # through on the previous turn's evidence.
    _deliver(ext, monkeypatch, [_READY])
    assert ext.poll_completion(partner_id_in_remote="conv-1") is False
