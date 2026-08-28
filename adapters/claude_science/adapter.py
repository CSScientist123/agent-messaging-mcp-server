"""Claude Science adapter: a :class:`~extension.base.RemoteExtension` for the
local Claude Science HTTP API.

Claude Science runs as a local service and is spoken to over plain HTTP via
:mod:`urllib.request` -- no third-party HTTP client, per this package's
stdlib-only constraint.

Ground truth for every route/field/header below is
``claude_science_mcp/api.py`` and ``claude_science_mcp/client.py`` (the real,
reverse-engineered client Claude Science's own MCP server uses), not the
routes originally guessed here. What changed and why:

- **Sending a message -- to a brand-new frame or an existing one -- goes
  through one endpoint, ``POST /api/request``**, with a JSON body of
  ``{target_agent, project_id, input_data: {request: <text>}, effort,
  thinking, ultra_mode, intent_id, root_frame_id?}``. There is no
  ``/api/frames/{id}/turns`` route (that was a guess). Confirmed live in
  ``api.py``'s own module docstring: this endpoint transparently reactivates
  a "cancelled" frame too, with no separate resume step needed.
- **A frame's live status is read from ``GET /api/frames/{id}/trace-shallow``**'s
  ``status`` field (``frames.getTraceShallow`` in the real route table),
  not a ``/status`` route -- no such route appears anywhere in the ground
  truth.
- **``POST /api/request`` requires ``project_id`` on every call**, even ones
  that target an existing frame via ``root_frame_id`` (see
  ``ClaudeScienceAPI.submit_request``'s signature: ``project_id`` is a
  required positional argument, always). But this extension's
  ``deliver_message`` is only ever given
  ``partner_id_in_remote`` (see ``extension.base.RemoteExtension``), never a
  ``project_system_id``. Claude Science's own MCP server hits the same wall
  and solves it by keeping a local record mapping a frame id to the
  project id it belongs to (``SessionStore`` -- see ``rec["project_id"]``
  used throughout ``server.py``'s ``message_session``). This file does the
  same: ``verify_partner_id_in_remote`` IS given both ids, and
  ``messaging_core`` always calls it once, when a partner is registered,
  before any delivery -- so it caches that association for
  ``deliver_message`` to consult later (see
  ``_project_id_by_frame``). That in-memory cache is process-local and does
  not survive a restart, but the same association also lives in the
  database (``partners.partner_id_in_remote`` joined to
  ``projects.project_system_id``), so a cache miss first falls back to
  whatever ``resolve_project_system_id`` callable the constructor was given,
  caching whatever it returns for next time. ``ClaudeScienceProjectIdUnknown``
  is raised only when no resolver was supplied, or the resolver also comes
  up empty -- rather than sending Claude Science a request it will very
  likely reject, or guessing a value.

Two things the earlier, guessed version of this file assumed exist, but do
not, as far as anything in the ground truth shows:

- **Cancelling a running frame.** The real API's only interrupt route,
  ``POST /frames/{id}/executions/{execId}/interrupt``, needs an execution id
  that is never surfaced anywhere in the ground truth: ``server.py`` never
  calls ``interrupt_execution``, and its own ``message_session`` docstring
  says outright that Claude Science's API exposes no way to cancel a queued
  message or a running turn, telling its own caller to "cancel it in the
  Claude Science UI" instead. The previously guessed ``POST
  /api/frames/{id}/cancel`` does not appear anywhere in the ground truth's
  route table either. Rather than call a route that most likely 404s (or,
  worse, silently does something unintended if it happens to exist for a
  different purpose), ``stop_remote_execution`` raises a clear ``Rejected``
  explaining this instead of guessing.
- **There is no per-frame filesystem grant, so there are no permissions to
  configure.** The real ``resume_frame`` is ``POST /frames/{id}/resume`` with
  no body at all, and nothing anywhere in the ground truth associates a frame
  with a set of readable or writable paths. ``get_permissions``,
  ``add_permissions`` and ``delete_permissions`` are therefore left at the
  base class's refusal rather than implemented against an invented endpoint:
  a caller told "granted" here would send work that stops on a prompt this
  adapter cannot see, which is the exact failure the approval doctrine exists
  to prevent.

  Resuming has no method at all any more, in this adapter or anywhere else.
  It turned out not to need one: per ``api.py``'s own docstring, ``POST
  /api/request`` -- the very endpoint ``deliver_message`` uses --
  transparently reactivates a cancelled frame, so for a real Claude Science
  instance, resuming-with-a-message and delivering-a-message are the same
  call. The design now says the same thing everywhere: you resume a partner
  by sending it the next message.
- ``Origin``: set to the base URL, sent unconditionally on every call (the
  real client does the same -- ``client.py`` notes ``GET /api/csrf``
  specifically 403s without it, with a distinct ``origin_required`` error
  that no amount of retrying can fix).
- ``x-operon-csrf`` on every mutating call (POST/PUT/PATCH/DELETE): the
  value of an ``operon_csrf`` cookie, minted by ``GET /api/csrf`` and cached
  until a 403 says it has gone stale (retried once, mirroring ``client.py``'s
  own retry-once-on-403 handling). Per ``client.py``'s double-submit
  description, the same value also has to ride along in the outgoing
  ``Cookie`` header, not just the custom header, so this adapter appends it
  there too.

NOT mirrored, and flagged here rather than silently approximated: the real
client's automatic re-authentication on a 401 (``client.py``'s ``_reauth``,
which mints a brand-new session cookie through the daemon's own local CLI
via ``auth.py``). This adapter is only ever constructed with a static
``Cookie`` string (``CLAUDE_SCIENCE_COOKIE`` or the ``cookie=`` argument) and
has no equivalent local daemon CLI to mint a fresh one from -- a 401 here is
therefore terminal and surfaces as ``ClaudeScienceHTTPError``, not something
this file can recover from on its own.

``project_system_id`` is a Claude Science project id; ``partner_id_in_remote``
is a frame id within that project.
"""

from __future__ import annotations

import json
import os
import uuid
import urllib.error
import urllib.request
from collections.abc import Callable

from extension.base import RemoteExtension, RemoteFailure
from messaging_core.errors import Rejected

__all__ = [
    "ClaudeScienceExtension",
    "ClaudeScienceHTTPError",
    "ClaudeScienceProjectIdUnknown",
]

ENV_BASE_URL = "CLAUDE_SCIENCE_BASE_URL"
ENV_COOKIE = "CLAUDE_SCIENCE_COOKIE"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Frame statuses that mean "still working" -- poll_completion is True for
# anything NOT in this set. This matches claude_science_mcp/server.py's own
# BUSY_STATUSES exactly (confirmed live 2026-08-20 there that "processing"
# belongs in this set too, e.g. mid-run waiting on a tool-execution
# approval) -- the original guess here happened to already match.
_BUSY_STATUSES = frozenset({"running", "queued", "in_progress", "streaming", "processing"})


class ClaudeScienceHTTPError(RemoteFailure):
    """An HTTP call to the Claude Science API returned something other than
    the status codes this adapter knows how to interpret for that call."""

    def __init__(self, method: str, path: str, status: int, body: bytes = b"") -> None:
        self.method = method
        self.path = path
        self.status = status
        detail = body.decode("utf-8", errors="replace")[:500] if body else ""
        message = f"Claude Science {method} {path} returned unexpected status {status}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class ClaudeScienceProjectIdUnknown(RemoteFailure):
    """`deliver_message` needs `partner_id_in_remote`'s
    project id to build a real `POST /api/request` body, but no
    `verify_partner_id_in_remote` call has ever cached one for it on this
    extension instance (see the module docstring)."""

    def __init__(self, partner_id_in_remote: str) -> None:
        super().__init__(
            f"Don't know which Claude Science project frame {partner_id_in_remote!r} "
            "belongs to -- POST /api/request requires project_id on every call, but "
            "this adapter only learns a frame's project id from verify_partner_id_in_remote "
            "(which messaging_core calls once, at partner registration). Register this "
            "partner in this same process first, or call verify_partner_id_in_remote "
            "directly, before delivering to it."
        )


class ClaudeScienceExtension(RemoteExtension):
    source_prefix = "science_"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        cookie: str | None = None,
        resolve_project_system_id: Callable[[str], str | None] | None = None,
    ) -> None:
        """
        Args:
            base_url: Root of the Claude Science API, e.g. ``http://127.0.0.1:8000``.
                Defaults to ``$CLAUDE_SCIENCE_BASE_URL``, then to
                ``http://127.0.0.1:8000``.
            cookie: The full ``Cookie`` header value used to authenticate.
                Defaults to ``$CLAUDE_SCIENCE_COOKIE``, then to an empty string.
            resolve_project_system_id: Looks up a frame id's project id
                (`project_system_id`) when `_project_id_by_frame` has no entry
                for it -- typically backed by the same database row
                `verify_partner_id_in_remote` would have cached from, had this
                process been the one to register the partner. `None` (the
                default) means a cache miss has nowhere else to go, so it
                raises `ClaudeScienceProjectIdUnknown` immediately, same as
                before this argument existed.
        """
        resolved_base = base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL
        self.base_url = resolved_base.rstrip("/")
        self.cookie = cookie if cookie is not None else os.environ.get(ENV_COOKIE, "")
        self._csrf_token: str | None = None
        self._resolve_project_system_id = resolve_project_system_id
        # partner_id_in_remote (frame id) -> project_system_id (project id).
        # Populated by verify_partner_id_in_remote; see module docstring.
        self._project_id_by_frame: dict[str, str] = {}

    # -- HTTP plumbing -----------------------------------------------------

    def _headers(self, method: str = "GET") -> dict[str, str]:
        # Origin must equal the base URL on every request -- this API
        # rejects requests without it, so it is not conditional on method.
        headers = {"Origin": self.base_url}
        cookie = self.cookie
        if method in _MUTATING_METHODS:
            if self._csrf_token is None:
                self._refresh_csrf()
            if self._csrf_token:
                headers["x-operon-csrf"] = self._csrf_token
                # Double-submit: client.py describes the token as a cookie
                # that must ALSO be echoed back as this header -- so the
                # cookie side needs to actually carry it too, not just the
                # header.
                csrf_cookie = f"operon_csrf={self._csrf_token}"
                cookie = f"{cookie}; {csrf_cookie}" if cookie else csrf_cookie
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _refresh_csrf(self) -> None:
        """Mirrors client.py's own CSRF bootstrap: GET /api/csrf, then read
        whatever `operon_csrf` cookie value the server just issued via
        Set-Cookie. Confirmed live in client.py: this call specifically also
        requires the Origin header (already sent unconditionally by
        `_headers`), or it 403s with a distinct 'origin_required' error that
        retrying can never fix on its own.
        """
        url = f"{self.base_url}/api/csrf"
        request = urllib.request.Request(url, headers=self._headers("GET"), method="GET")
        try:
            response = urllib.request.urlopen(request)
            try:
                set_cookie_headers = response.headers.get_all("Set-Cookie") or []
            finally:
                response.close()
        except urllib.error.HTTPError as exc:
            headers = exc.headers
            set_cookie_headers = list(headers.get_all("Set-Cookie") or []) if headers else []
            exc.close()
            if not set_cookie_headers:
                raise ClaudeScienceHTTPError("GET", "/api/csrf", exc.code) from exc
        for raw in set_cookie_headers:
            name_value = raw.split(";", 1)[0]
            if "=" not in name_value:
                continue
            name, value = name_value.split("=", 1)
            if name.strip() == "operon_csrf":
                self._csrf_token = value.strip()
                return

    def _request(self, method: str, path: str, *, json_body: dict | None = None) -> tuple[int, dict | None]:
        """Issue one HTTP request. Returns (status, parsed_json_or_None).

        A non-2xx response raised by urllib as `HTTPError` is captured here
        rather than propagated, so every caller decides for itself which
        statuses (e.g. 404) are meaningful and which are `ClaudeScienceHTTPError`.

        A 403 on a mutating call gets exactly one retry after refreshing the
        CSRF token -- mirrors client.py's own "likely a stale CSRF token"
        retry-once behaviour.
        """
        url = f"{self.base_url}{path}"
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")

        def _once(headers: dict[str, str]) -> tuple[int, bytes]:
            if data is not None:
                headers = {**headers, "Content-Type": "application/json"}
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                response = urllib.request.urlopen(request)
                try:
                    return response.status, response.read()
                finally:
                    response.close()
            except urllib.error.HTTPError as exc:
                try:
                    return exc.code, exc.read()
                finally:
                    exc.close()

        status, raw = _once(self._headers(method))
        if status == 403 and method in _MUTATING_METHODS:
            self._csrf_token = None
            status, raw = _once(self._headers(method))

        payload = None
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
        return status, payload

    def _require_ok(self, method: str, path: str, status: int, payload: dict | None, ok: tuple[int, ...]) -> dict:
        if status not in ok:
            body = json.dumps(payload).encode("utf-8") if payload is not None else b""
            raise ClaudeScienceHTTPError(method, path, status, body)
        return payload or {}

    def _require_project_id(self, frame_id: str) -> str:
        """Mirrors NotebookLM's `_require_notebook_id`: the in-memory cache
        `verify_partner_id_in_remote` wrote to is empty in a fresh process,
        but the same association also lives in the database, so a miss
        defers to `_resolve_project_system_id` (when one was given) and
        caches whatever it returns before raising only if that also comes up
        empty -- see the module docstring. The resolver is an injected
        callable, not code this adapter controls -- this class is what
        promises callers a specific ClaudeScienceProjectIdUnknown on an
        unresolvable id, and it cannot keep that promise while trusting an
        arbitrary callable not to throw something else instead, so a raising
        resolver is treated as just another miss.
        """
        project_id = self._project_id_by_frame.get(frame_id)
        if project_id is None and self._resolve_project_system_id is not None:
            try:
                project_id = self._resolve_project_system_id(frame_id)
            except Exception:
                project_id = None
            if project_id is not None:
                self._project_id_by_frame[frame_id] = project_id
        if project_id is None:
            raise ClaudeScienceProjectIdUnknown(frame_id)
        return project_id

    def _submit_into_frame(self, frame_id: str, text: str) -> tuple[str, dict]:
        """The real `POST /api/request` call the Claude Science UI uses to
        send `text` into `frame_id`, whether it is cancelled or not
        (confirmed live -- see the module docstring). Shared by
        `deliver_message`, since ground truth
        shows they are, for a real Claude Science instance, the same
        operation. Returns (intent_id, response_payload).
        """
        project_id = self._require_project_id(frame_id)
        intent_id = str(uuid.uuid4())
        body = {
            "target_agent": "OPERON",
            "project_id": project_id,
            "root_frame_id": frame_id,
            "input_data": {"request": text},
            "effort": "high",
            "thinking": True,
            "ultra_mode": False,
            "intent_id": intent_id,
        }
        status, payload = self._request("POST", "/api/request", json_body=body)
        payload = self._require_ok("POST", "/api/request", status, payload, ok=(200, 201))
        return intent_id, payload

    # -- RemoteExtension surface --------------------------------------------

    def verify_project_system_id(self, project_system_id: str) -> bool:
        path = f"/api/projects/{project_system_id}"
        status, _ = self._request("GET", path)
        if status == 200:
            return True
        if status == 404:
            return False
        raise ClaudeScienceHTTPError("GET", path, status)

    def verify_partner_id_in_remote(self, project_system_id: str, partner_id_in_remote: str) -> bool:
        """`frames.getFrame` -- `GET /frames/{id}`, per the real route table.

        Not nested under a project: Claude Science's real API has no
        `/api/projects/{id}/frames/{id}` route (that was a guess) -- frames
        are addressed directly by id everywhere in the ground truth. On a
        200, also caches `project_system_id` for this frame id, since
        `deliver_message` needs it later and has
        no other way to learn it (see module docstring).
        """
        path = f"/api/frames/{partner_id_in_remote}"
        status, _ = self._request("GET", path)
        if status == 200:
            self._project_id_by_frame[partner_id_in_remote] = project_system_id
            return True
        if status == 404:
            return False
        raise ClaudeScienceHTTPError("GET", path, status)

    def deliver_message(self, *, partner_id_in_remote: str, behavior: str, body: str) -> str:
        """`POST /api/request` with `root_frame_id` set to `partner_id_in_remote`
        -- the single endpoint the real Claude Science UI uses to send any
        message into any existing frame, cancelled or not (see module
        docstring; confirmed live by intercepting the UI's own network
        traffic). There is no `/api/frames/{id}/turns` route.

        `behavior` has no counterpart in the real request body (it is a
        messaging-MCP-local concept) and is intentionally unused here.
        Returns the freshly generated `intent_id` sent with the request --
        Claude Science's own response never echoes back an id for this call
        (there is no per-turn id in the real response shape), so this is
        the only identifier this adapter has to offer as an opaque handle.
        """
        del behavior  # no real counterpart; see docstring
        intent_id, _ = self._submit_into_frame(partner_id_in_remote, body)
        return intent_id

    def stop_remote_execution(self, *, partner_id_in_remote: str, reason: str) -> None:
        """`POST /api/frames/{id}/cancel` -- the route the UI's own stop button calls.

        Confirmed live by driving the Claude Science UI and watching the
        network: pressing stop mid-turn issues exactly this POST, returns 200,
        and the frame reports "This session was cancelled." A `sleep 25` that
        had been approved and started never printed its completion marker, so
        the turn is genuinely stopped rather than merely marked.

        It takes the frame id and NOTHING else. That matters, because the
        interrupt route this adapter used to reach for --
        `POST /frames/{id}/executions/{execId}/interrupt` -- needs an execution
        id no other call returns, and finding none, the adapter concluded no
        cancel existed and refused every time. The conclusion followed from
        looking at the wrong route.

        **What it stops, precisely.** The AGENT'S TURN, not everything the turn
        started. A compute kernel the agent kicked off keeps running -- the UI
        still showed "1 running" for its own reasons after the cancel landed.
        For this system's purposes that is the right granularity: displacement
        needs the agent to stop reading and acting on the old instruction
        before the new one arrives, and it does.
        """
        path = f"/api/frames/{partner_id_in_remote}/cancel"
        status, payload = self._request("POST", path)
        # 404 means the frame is gone, and a frame that does not exist is not
        # running -- which is the outcome asked for. Anything else is a real
        # failure and must not be swallowed into a silent no-op, because the
        # caller is about to deliver a second instruction to an agent it
        # believes has stopped.
        if status == 404:
            return
        self._require_ok("POST", path, status, payload, ok=(200, 201, 202, 204))

    def poll_completion(self, *, partner_id_in_remote: str) -> bool:
        """`frames.getTraceShallow` -- `GET /frames/{id}/trace-shallow`, per
        the real route table. The previously guessed `/api/frames/{id}/status`
        route does not exist anywhere in the ground truth; Claude Science's
        own MCP server reads a frame's live status this same way
        (`api.get_trace_shallow(frame_id).get("status")`, throughout
        server.py).
        """
        path = f"/api/frames/{partner_id_in_remote}/trace-shallow?include_messages=false"
        status, payload = self._request("GET", path)
        payload = self._require_ok("GET", path, status, payload, ok=(200,))
        return payload.get("status") not in _BUSY_STATUSES

    def read_remote_result(self, *, partner_id_in_remote: str) -> str:
        """`frames.getMessages` -- `GET /frames/{id}/messages?from=&limit=`,
        per the real route table (queried here as `?limit=200` with no
        `from`, the same "most recent window" shape `poll_completion` already
        uses for `trace-shallow`). The payload is either a bare list of
        messages or a dict wrapping one under `"messages"` -- both are
        accepted since nothing in the ground truth pins the shape down to one
        of them.

        Messages carrying `"_harness_notice": true` are dropped first,
        before the trailing run below is even computed: that flag marks
        Claude Science's own runtime context injection (skill-discovery
        keyword dumps, compute-spec snapshots, `[Memory]` recall blocks) into
        the transcript, not either side of the conversation actually
        speaking. Leaving one in place could stop the trailing run one
        message short, or worse, return the harness's own bookkeeping as if
        it were Claude Science's reply.

        The **trailing run of `role == "assistant"` messages** -- walking the
        filtered list backwards and stopping at the first non-assistant
        message -- is exactly the answer to the last thing this system sent:
        a single reply can span several assistant messages in a row (tool
        calls interleaved with prose) before the frame yields back, and every
        one of them belongs to that same reply. Anything at or before the
        last non-assistant message belongs to a prior exchange, not this one.

        Text is collected from that run in original order. `content` is
        normally a list of blocks (a text block being `{"type": "text",
        "text": ...}`), but may also be a plain string, and both are
        handled. Each piece is stripped, empty pieces are dropped, and what
        remains is joined with a blank line between messages. Nothing found
        is a real answer -- e.g. a reply that ended in only a tool call --
        so this returns `""` rather than inventing a placeholder or raising.
        """
        path = f"/api/frames/{partner_id_in_remote}/messages?limit=200"
        status, payload = self._request("GET", path)
        payload = self._require_ok("GET", path, status, payload, ok=(200,))

        if isinstance(payload, dict):
            messages = payload.get("messages") or []
        elif isinstance(payload, list):
            messages = payload
        else:
            messages = []

        speaking = [
            message
            for message in messages
            if isinstance(message, dict) and not message.get("_harness_notice")
        ]

        # Walk backwards from the end: everything from here to the end is
        # this reply, everything before it is the message that provoked it.
        run_start = len(speaking)
        while run_start > 0 and speaking[run_start - 1].get("role") == "assistant":
            run_start -= 1
        trailing_run = speaking[run_start:]

        pieces: list[str] = []
        for message in trailing_run:
            content = message.get("content")
            if isinstance(content, str):
                text = content.strip()
                if text:
                    pieces.append(text)
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = str(block.get("text", "")).strip()
                    if text:
                        pieces.append(text)

        return "\n\n".join(pieces)
