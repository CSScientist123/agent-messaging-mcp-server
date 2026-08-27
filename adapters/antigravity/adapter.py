"""Antigravity adapter: a :class:`~extension.base.RemoteExtension` that drives
the ``agy`` CLI through ``tmux``, one session per conversation.

``project_system_id`` is a filesystem folder path -- verified locally with
``Path.is_dir()`` (the real ``agy_create_conversation`` tool checks the same
thing with ``fs.existsSync(folder)`` before ever shelling out). No CLI call
needed. ``partner_id_in_remote`` is a conversation id, mapped onto a tmux
session name; every other operation (sending a message, interrupting,
resuming, checking status) is done by sending keys into that session and
reading its pane back with ``tmux capture-pane``.

Ground truth is ``antigravity-cli``'s own ``index.js`` and ``lib/control.js``,
not what was originally guessed here. What changed and why:

- **The session name truncates the conversation id.** The real
  `sessionFor = (id) => agy-${id.slice(0, 8)}` in `index.js` uses only
  the first 8 characters of the conversation id, not the whole id (matches
  the real fixture pane names on this machine, e.g.
  `agy-176197cc.pane.txt`). The previously guessed `agy-<full id>` was wrong
  for any id longer than 8 characters.
- **A message is typed in as two separate tmux calls, not one.** The real
  `agy_send` does `tmux send-keys -t <sess> -l <message>` (the `-l` flag
  sends it as LITERAL text, so nothing in the message is misread as a key
  name) and only THEN, as a second, separate call, `tmux send-keys -t
  <sess> Enter`. The previously guessed single call
  (`send-keys -t <sess> <body> Enter`) omits `-l`, so a message that happens
  to look like a tmux key name could be misinterpreted instead of typed.
- **Idle/busy/prompt are read from the pane's footer, not invented phrases.**
  `lib/control.js` reads two fixed footer rows agy paints in every state:
  `esc to cancel` (shown while streaming AND while showing a permission
  prompt) and `? for shortcuts` (shown only when accepting input, i.e.
  idle). None of the previously guessed busy markers ("thinking...",
  "generating...", "running...", "working...") appear anywhere in the real
  pane-classification code. Likewise, the real permission/trust headers are
  specific fixed phrases ("Do you want to proceed?", "Requesting permission
  for:", "Do you trust the contents of this project?", "Allow access to
  this file?" / "Reason: outside workspace") -- not the previously guessed
  set ("allow this action", "grant permission", "approve this", "trust
  this", ...), none of which appear in `lib/control.js` either. NOTE: the
  real classifier additionally requires CORROBORATION -- a genuine
  permission prompt must ALSO look like a contiguous, single-selection
  numbered options menu (`optionsLookLikeAMenu`), and an empty input box on
  screen is a HARD NEGATIVE that forces idle/busy over prompt no matter what
  text is present (this is precisely what stops an ordinary numbered-list
  reply from being misread as a prompt -- see `lib/control.js`'s own
  docstring for the false positive this fixed live). That full corroboration
  algorithm is NOT reimplemented here -- this adapter only ports the real
  footer/header text constants, not the menu-shape/hard-negative logic
  around them -- so it is more prone to that same class of false positive
  than the real `agy` CLI is. Treat a `poll_completion` verdict here as a
  useful signal, not as reliable as reading `lib/control.js` directly.
- **There is no real command to interrupt a running turn.** `index.js` has
  no `agy_interrupt`/cancel tool, and no `C-c`/Ctrl-C send anywhere in it.
  The one concrete, real signal about how a turn is meant to be cancelled is
  the footer text itself: `lib/control.js` labels `esc to cancel` as
  "shown while streaming AND while prompting". `stop_remote_execution`
  therefore sends `Escape`, not `C-c`.

  **Confirmed live, and the absence of an API confirmed by reading the
  client.** `index.js` sends `Escape` in exactly two places -- `escapePicker`
  (leave a menu) and `agy_dismiss` (dismiss an overlay) -- so there genuinely
  is no dedicated cancel call to prefer over the keystroke. And driving it
  against a real session settles what the source cannot: a busy turn went
  idle 2 seconds after `Escape`, with the pane reading
  `Interrupted - What should Antigravity CLI do instead?`. This was an
  inference for a long time; it is now a measurement.
- **Permissions are project-scoped, and the store is a JSON file.** The
  `agy` binary logs `persistProjectPermissions: saved permissions to project
  %q` and writes `~/.gemini/config/projects/<project-id>.json`. The project id
  comes from `~/.gemini/antigravity-cli/cache/default_project_id.txt`.

  The allowlist lives at **`permissionGrants.permissionGrants.allow`** -- a
  list of rule strings, under a doubly-nested key. Not `projectResources`,
  which stays `{}`.

  That distinction was got wrong once and is worth the warning. An earlier
  version of this adapter read `projectResources`, "confirmed" by observing
  that it was `{}` while the TUI header read `allowlist (0)`. Empty matched
  empty, which is not evidence of anything: the moment a rule was actually
  added live, the TUI read `allowlist (1)` and this method still returned `[]`.
  A mapping is only confirmed by a NON-empty value appearing on both sides.

  Two other candidate stores were investigated and rejected on evidence.
  `~/.gemini/antigravity-cli/settings.json` holds `permissions.allow`, but it
  is global -- writing it grants every conversation, which is not what a
  project-scoped grant means. And the conversation's own
  `conversations/<id>.db` contains rule strings in an `executor_metadata`
  protobuf, but that blob still held `write_file(/)` while the TUI reported
  `allowlist (0)` -- so it is not the list the UI reads.

  So `get_permissions` READS that JSON, and `add_permissions` /
  `delete_permissions` WRITE through the TUI's own `/permissions` view over
  tmux and then verify against the same JSON. The asymmetry is deliberate:
  reading a file is exact, and typing into a TUI is not, so the typed half is
  the half that has to prove it worked.

The one rule this module exists to enforce, not just implement: an approval
or permission prompt inside an Antigravity conversation is an ERROR, never a
question this extension answers. There is no method anywhere in this file
that types a response into such a prompt, and none should ever be added.
When `poll_completion` sees one, it raises `Rejected("approval_is_an_error", ...)`
whose message spells out the only correct remedy -- interrupt the turn,
reply with ``[ERROR]``, correct the grant with ``add_permissions``, and send
the work again -- because routing an approval prompt to a human to "answer"
is exactly the failure mode this is guarding against.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from extension.base import RemoteExtension, RemoteFailure
from messaging_core.errors import Rejected

__all__ = ["AntigravityExtension", "TmuxBinaryMissing"]

# Fixed footer rows agy paints in every state (lib/control.js: FOOTER_BUSY /
# FOOTER_IDLE). Checked case-insensitively, as substrings of the captured
# pane.
_FOOTER_BUSY_MARKER = "esc to cancel"
_FOOTER_IDLE_MARKER = "for shortcuts"  # real regex: /\?\s+for shortcuts/i

# The pane border/prompt-arrow characters agy draws around the chat box. A
# trailing line made of nothing else is frame, not content -- read_remote_result
# strips it for the same reason it strips the footer: neither side ever typed it.
_CHROME_ONLY_CHARS = set("│╰╯─╮╭> ")

# Real permission/trust prompt headers (lib/control.js: PROMPT_HEADERS).
# Checked case-insensitively, as substrings of the captured pane. The real
# classifier also requires the pane to look like a numbered-options menu
# before calling this a prompt (see module docstring) -- not reimplemented
# here.
_APPROVAL_PROMPT_MARKERS = (
    "do you want to proceed?",
    "requesting permission for:",
    "do you trust the contents of this project?",
    "allow access to this file?",
    "reason: outside workspace",
)

# Where agy keeps project-scoped permissions, and where it keeps the id of the
# project a CLI conversation belongs to. Both confirmed against a live
# install; see the module docstring for how.
_PROJECT_ID_FILE = "~/.gemini/antigravity-cli/cache/default_project_id.txt"
_PROJECT_CONFIG_DIR = "~/.gemini/config/projects"

# How long to let the TUI repaint after a keystroke before reading the pane. A
# capture taken too early returns the screen from BEFORE the key, which during
# live verification produced a pane showing half a typed rule and looked exactly
# like the terminal having swallowed the rest of it.
_KEY_SETTLE = 0.15

# The two screens `/permissions` actually goes through, confirmed against a live
# session. Each is checked before anything is typed into it: if the pane does not
# say what it should, whatever is on screen is not the editor, and a rule typed
# into it would be posted into the chat instead -- to an agent that would then
# try to act on it.
#
# Screen 1, the scope selector. Its default highlight is already "Project",
# which is the scope this adapter wants ("Rules that apply only to this
# project"), so Enter takes it without any navigation.
_SCOPE_SELECTOR_MARKERS = ("permission config editor", "select a config scope")

# Screen 2, the rule list. This is the one the user's screenshot shows.
_PERMISSION_VIEW_MARKERS = ("allowlist (", "a add rule", "switch view")

# Screen 3, reached with `a`, where a rule is typed.
_ADD_RULE_MARKERS = ("add rule —", "enter a permission rule", "format: action(target)")


class TmuxBinaryMissing(RemoteFailure):
    """The ``tmux`` binary is not on PATH (and no explicit path was given)."""

    def __init__(self, detail: str = "") -> None:
        message = "the 'tmux' binary was not found on PATH (checked shutil.which('tmux'))"
        if detail:
            message = f"{message}: {detail}"
        message += (
            "; install tmux or construct AntigravityExtension(tmux_path=...) with an "
            "explicit path."
        )
        super().__init__(message)


class AntigravityExtension(RemoteExtension):
    source_prefix = "gemini_"

    def __init__(self, *, tmux_path: str | None = None, session_prefix: str = "agy-") -> None:
        """
        Args:
            tmux_path: Explicit path to the ``tmux`` binary. If omitted,
                resolved via ``shutil.which("tmux")`` on every call (not
                cached), so tests can monkeypatch `shutil.which` freely.
            session_prefix: Prefix applied to a conversation id's first 8
                characters to form its tmux session name (see module
                docstring -- real `sessionFor` in `index.js` truncates to 8).
        """
        self._tmux_path_override = tmux_path
        self.session_prefix = session_prefix
        self._delivery_count = 0
        # Failures to close the permissions editor that were swallowed to avoid
        # masking a more useful exception. Not part of the contract; kept so a
        # session stuck in the editor is at least discoverable afterwards.
        self.close_errors: list[Exception] = []
        # session name -> last body typed into it by deliver_message. There is
        # no other way for read_remote_result to know where in the pane THIS
        # turn's answer starts -- the pane itself has no structure to key off
        # of, only the echo of what was sent.
        self._last_delivered: dict[str, str] = {}

    # -- binary + tmux plumbing --------------------------------------------

    def _resolve_binary(self) -> str:
        path = self._tmux_path_override or shutil.which("tmux")
        if path is None:
            raise TmuxBinaryMissing()
        return path

    def _tmux(self, *args: str) -> subprocess.CompletedProcess:
        binary = self._resolve_binary()
        return subprocess.run([binary, *args], capture_output=True, text=True)

    def _session_name(self, partner_id_in_remote: str) -> str:
        # Real `sessionFor = (id) => `agy-${id.slice(0, 8)}`` in index.js --
        # only the first 8 characters of the conversation id.
        return f"{self.session_prefix}{partner_id_in_remote[:8]}"

    # -- Path/session verification ------------------------------------------

    def verify_project_system_id(self, project_system_id: str) -> bool:
        """`project_system_id` is a folder path; it exists iff it is a directory.

        Mirrors the real `agy_create_conversation`'s own check
        (`fs.existsSync(folder)`) closely enough: `Path.is_dir()` additionally
        rejects a path that exists but is a plain file, which `existsSync`
        alone would not -- a strictly safer version of the same real check.
        """
        return Path(project_system_id).is_dir()

    def verify_partner_id_in_remote(self, project_system_id: str, partner_id_in_remote: str) -> bool:
        """The conversation's tmux session must actually be running.

        Matches the real `sessionExists` in `index.js`
        (`tmux has-session -t <sess>`) exactly.
        """
        session = self._session_name(partner_id_in_remote)
        result = self._tmux("has-session", "-t", session)
        return result.returncode == 0

    # -- RemoteExtension surface --------------------------------------------

    def deliver_message(self, *, partner_id_in_remote: str, behavior: str, body: str) -> str:
        """Type `body` into the conversation's tmux session and press Enter.

        Real `agy_send` sends this as TWO separate tmux calls -- literal
        text first (`send-keys -t <sess> -l <message>`, `-l` so nothing in
        `body` is read as a key name), then Enter on its own
        (`send-keys -t <sess> Enter`) -- not one call with the message and
        `"Enter"` as trailing arguments the way this was previously guessed.
        The real client also sleeps ~800ms between the two to avoid racing
        agy's own input redraw; not replicated here since this adapter's
        calls are synchronous or fake within a single test process, so
        there's no equivalent race to guard against.
        """
        session = self._session_name(partner_id_in_remote)
        result = self._tmux("send-keys", "-t", session, "-l", body)
        if result.returncode == 0:
            result = self._tmux("send-keys", "-t", session, "Enter")
        if result.returncode != 0:
            raise Rejected(
                "antigravity_session_unreachable",
                f"could not deliver to tmux session {session!r} for conversation "
                f"{partner_id_in_remote!r}: {result.stderr.strip()}",
            )
        # Wait for the pane to actually go busy before returning.
        #
        # Without this the method returns the instant Enter is pressed, while
        # the TUI has not yet repainted out of its idle state -- so the very
        # next `poll_completion` reads a stale idle footer, reports the turn
        # FINISHED, and the drain thread closes a task the agent has not begun.
        # Observed live: a [QUERY] round trip "completed" in 0 seconds with a
        # placeholder body, while the pane went on to show the real answer that
        # nobody was still waiting for. Nothing errored, which is what made it
        # worth guarding rather than noticing later.
        #
        # Bounded, and a timeout is NOT an error: a turn short enough to finish
        # inside the window never shows a busy footer at all, and for that case
        # returning is exactly right -- the answer is already on the pane.
        self._await_busy(session)
        # Recorded on success only: a session read_remote_result later keys
        # off of should match what actually reached the pane, not a body that
        # never made it there.
        self._last_delivered[session] = body
        self._delivery_count += 1
        return f"agy-turn-{partner_id_in_remote}-{self._delivery_count}"

    def _await_busy(self, session: str, attempts: int = 24, delay: float = 0.25) -> bool:
        """Poll the pane until it shows the busy footer. True if it was seen."""
        for _ in range(attempts):
            pane = self._capture(session).lower()
            if _FOOTER_BUSY_MARKER in pane:
                return True
            time.sleep(delay)
        return False

    def stop_remote_execution(self, *, partner_id_in_remote: str, reason: str) -> None:
        """Send Escape, not Ctrl-C, to interrupt the running turn.

        `index.js` has no dedicated interrupt/cancel tool and no `C-c` send
        anywhere in it -- the only real, concrete signal about how a turn is
        meant to be cancelled is `lib/control.js`'s own footer text, which it
        labels `esc to cancel`, "shown while streaming AND while prompting".

        Verified live: a busy turn went idle 2 seconds after this, and the
        pane read `Interrupted - What should Antigravity CLI do instead?`.
        The absence of a better mechanism is also confirmed rather than
        assumed -- `index.js` sends `Escape` only from `escapePicker` and
        `agy_dismiss`, neither of which cancels a turn, so there is no
        documented cancel call being passed over here.
        """
        session = self._session_name(partner_id_in_remote)
        result = self._tmux("send-keys", "-t", session, "Escape")
        if result.returncode != 0:
            raise Rejected(
                "antigravity_session_unreachable",
                f"could not interrupt tmux session {session!r} for conversation "
                f"{partner_id_in_remote!r} (reason: {reason}): {result.stderr.strip()}",
            )

    # -- permissions ---------------------------------------------------------

    def _project_config_path(self) -> Path:
        """The JSON file holding this install's project-scoped permissions.

        Two files, in order: the cached project id, then the project config
        named by it. Both are read fresh on every call rather than cached,
        because `agy` rewrites the config whenever a rule changes and a cached
        copy would be exactly as stale as the thing it is meant to verify.
        """
        id_file = Path(os.path.expanduser(_PROJECT_ID_FILE))
        try:
            project_id = id_file.read_text().strip()
        except OSError as exc:
            raise Rejected(
                "antigravity_project_unknown",
                f"could not read the active Antigravity project id from {id_file}: {exc}. "
                "Without it there is no way to tell which project's permissions to change.",
            ) from exc
        if not project_id:
            raise Rejected(
                "antigravity_project_unknown",
                f"{id_file} is empty; Antigravity has no active project to configure.",
            )
        return Path(os.path.expanduser(_PROJECT_CONFIG_DIR)) / f"{project_id}.json"

    def _read_project_rules(self) -> list[str]:
        """The allowlist as the file holds it. A missing file means no rules yet.

        A missing or empty `projectResources` is a real answer -- it is what a
        fresh install looks like, and it is what was observed alongside the
        TUI's own `allowlist (0)`. Treating it as an error would make the
        commonest starting state indistinguishable from a broken one.
        """
        path = self._project_config_path()
        try:
            raw = path.read_text()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise Rejected(
                "antigravity_project_unreadable",
                f"could not read {path}: {exc}.",
            ) from exc
        try:
            config = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise Rejected(
                "antigravity_project_unreadable",
                f"{path} is not valid JSON ({exc}); refusing to guess what it allows.",
            ) from exc
        # Valid JSON is not the same as the shape this expects. A top-level array
        # parses fine and then fails on `.get`, which would surface as an
        # AttributeError four frames away from the file that caused it -- and
        # would propagate out of add_permissions' verify step, where it would
        # read as "the grant did not land" rather than "the config is wrong".
        if not isinstance(config, dict):
            raise Rejected(
                "antigravity_project_unreadable",
                f"{path} holds a JSON {type(config).__name__}, not an object; refusing to "
                "guess what it allows.",
            )
        grants = config.get("permissionGrants")
        # Doubly nested, as agy writes it: {"permissionGrants": {"permissionGrants":
        # {"allow": [...]}}}. Unwrap one level if it is there and keep going if it
        # is not, rather than hardcoding a depth -- the outer key is a message
        # wrapper and a future version dropping it should not read as "no rules".
        if isinstance(grants, dict) and isinstance(grants.get("permissionGrants"), dict):
            grants = grants["permissionGrants"]
        if grants is None:
            return []
        if not isinstance(grants, dict):
            raise Rejected(
                "antigravity_project_unreadable",
                f"{path} has a permissionGrants that is a JSON {type(grants).__name__}, "
                "not an object; refusing to guess what it allows.",
            )
        allowed = grants.get("allow") or []
        if not isinstance(allowed, list):
            raise Rejected(
                "antigravity_project_unreadable",
                f"{path} has an allow that is a JSON {type(allowed).__name__}, not a list; "
                "refusing to guess what it allows.",
            )
        return [str(rule) for rule in allowed]

    def get_permissions(self, *, partner_id_in_remote: str) -> list[str]:
        """Read the conversation's allowlist out of the project config file.

        Reads the file rather than the pane. The file is the store `agy`
        itself writes (`persistProjectPermissions: saved permissions to
        project %q`), and reading it is exact; parsing a terminal pane means
        parsing whatever fits on screen, wrapped and truncated. The pane is
        used for WRITING, where there is no alternative, and nowhere else.

        `partner_id_in_remote` is accepted for interface symmetry and is not
        used: Antigravity permissions are scoped to the project, not the
        conversation, so every conversation under one project sees one list.
        That is a property of Antigravity, not a shortcut here.
        """
        return self._read_project_rules()

    def _capture(self, session: str, history: int | None = None) -> str:
        """One pane read, raising if the session is gone.

        `history`, when given, adds `-S -<history>` to pull that much
        scrollback along with the visible screen -- without it, capture-pane
        returns only what currently fits on screen, and an answer long enough
        to have scrolled past that would be silently missing from the read.
        """
        args = ["capture-pane", "-t", session, "-p"]
        if history is not None:
            args += ["-S", f"-{history}"]
        result = self._tmux(*args)
        if result.returncode != 0:
            raise Rejected(
                "antigravity_session_unreachable",
                f"could not read tmux session {session!r}: {result.stderr.strip()}",
            )
        return result.stdout

    def _await_pane(
        self, session: str, markers: tuple[str, ...], attempts: int = 12, delay: float = 0.2
    ) -> str | None:
        """Capture the pane until every marker appears, or give up.

        A TUI repaints asynchronously, so the first capture after a keystroke
        usually shows the screen from BEFORE it. During live verification that
        produced a pane holding half a typed rule, which looked exactly like the
        terminal having swallowed the rest -- the rule was in fact complete a
        moment later. Retrying is what makes a pane read mean anything.

        ALL markers must be present, not any: the rule-list screen and the
        add-rule screen share words, and matching on one of them would accept
        the wrong screen.
        """
        for _ in range(attempts):
            pane = self._capture(session)
            lowered = pane.lower()
            if all(marker in lowered for marker in markers):
                return pane
            time.sleep(delay)
        return None

    def _open_permissions_view(self, session: str) -> str:
        """Walk `/permissions` to the rule list, and return that pane.

        Three steps, not one, and the count was got wrong before it was checked
        against a live session. `/permissions` + Enter only selects the command
        from the palette; a SECOND Enter is needed to take the scope selector's
        default, which is already "Project". An adapter that sent one Enter
        would sit on the scope selector believing it was on the rule list, and
        the next `a` would do something else entirely.

        Every screen is confirmed before the next key is sent. Raises rather
        than continuing if any of them is not what it should be.
        """
        result = self._tmux("send-keys", "-t", session, "-l", "/permissions")
        if result.returncode == 0:
            result = self._tmux("send-keys", "-t", session, "Enter")
        if result.returncode != 0:
            raise Rejected(
                "antigravity_session_unreachable",
                f"could not open the permissions view in tmux session {session!r}: "
                f"{result.stderr.strip()}",
            )
        if self._await_pane(session, _SCOPE_SELECTOR_MARKERS) is None:
            raise Rejected(
                "permissions_view_did_not_open",
                f"sent /permissions to {session!r} but the pane never showed the scope "
                f"selector (looked for {', '.join(_SCOPE_SELECTOR_MARKERS)}). Refusing to "
                "send further keys into an unknown screen.",
            )

        # Take the default scope, which is Project.
        self._tmux("send-keys", "-t", session, "Enter")
        pane = self._await_pane(session, _PERMISSION_VIEW_MARKERS)
        if pane is None:
            raise Rejected(
                "permissions_view_did_not_open",
                f"selected the Project scope in {session!r} but the pane never showed the "
                f"rule list (looked for {', '.join(_PERMISSION_VIEW_MARKERS)}).",
            )
        return pane

    @staticmethod
    def _listed_rules(pane: str) -> list[str]:
        """The rules on the list screen, in display order.

        Scoped to the region between the `allowlist (` header and the `Keyboard:`
        footer. That bound matters: the chat prompt is also a line beginning with
        `>`, and the selection marker is `>` too, so an unscoped scan would treat
        the prompt as a rule and every index after it would be off by one.
        """
        rules: list[str] = []
        inside = False
        for line in pane.splitlines():
            stripped = line.strip()
            low = stripped.lower()
            if low.startswith("allowlist (") or " allowlist (" in low:
                inside = True
                continue
            if not inside:
                continue
            if low.startswith("keyboard:"):
                break
            body = stripped[1:].strip() if stripped.startswith(">") else stripped
            if "(" in body and body.endswith(")"):
                rules.append(body)
        return rules

    @staticmethod
    def _selected_index(pane: str) -> int:
        """Which rule row currently carries the `>` cursor. 0 if none does.

        Walks `pane` with the exact same rule-recognizing logic as
        `_listed_rules`, so the row count it counts against is always the one
        implied by `pane` itself -- there is no separate `rules` list to pass
        in, because any list a caller could hand over is either this same
        walk repeated or a stale one from a different pane, and a stale list
        is precisely what must not bound this answer.
        """
        inside = False
        index = 0
        for line in pane.splitlines():
            stripped = line.strip()
            low = stripped.lower()
            if low.startswith("allowlist (") or " allowlist (" in low:
                inside = True
                continue
            if not inside:
                continue
            if low.startswith("keyboard:"):
                break
            body = stripped[1:].strip() if stripped.startswith(">") else stripped
            if "(" in body and body.endswith(")"):
                if stripped.startswith(">"):
                    return index
                index += 1
        return 0

    def _close_quietly(self, session: str) -> None:
        """Close the editor from a `finally`, without masking why we got here.

        An exception raised inside a `finally` REPLACES the one already
        propagating. Closing can fail (the session may be gone, which is often
        why the operation failed in the first place), and if that replacement
        happens the caller is handed "stuck in the editor" instead of "could not
        reach the add screen" -- a symptom instead of the cause, and the two
        have different fixes.

        So a close failure during unwinding is recorded on `.close_errors` and
        swallowed. On the success path, `_close_permissions_view` is called
        directly and its failure does propagate, because there is nothing to
        mask and a session left in the editor must be reported.
        """
        try:
            self._close_permissions_view(session)
        except Exception as exc:  # noqa: BLE001 - deliberately not masking
            self.close_errors.append(exc)

    def _close_permissions_view(self, session: str) -> None:
        """Escape until the session is back at the chat prompt, not just once.

        The editor is TWO screens deep -- rule list, then scope selector -- so a
        single Escape lands on the scope selector and leaves the session still
        inside the editor. Everything typed afterwards is read as editor input,
        which means the very next `deliver_message` would be swallowed rather
        than delivered, and nothing downstream would report a thing.

        So this presses Escape until the idle chat footer is back, bounded. It
        is called from a `finally`, including on the failure paths, because a
        session left in the editor is worse than a failed permission change: the
        change is reported as failed, the swallowed message is not.
        """
        for _ in range(4):
            if _FOOTER_IDLE_MARKER in self._capture(session).lower():
                return
            self._tmux("send-keys", "-t", session, "Escape")
            time.sleep(_KEY_SETTLE * 2)
        if _FOOTER_IDLE_MARKER not in self._capture(session).lower():
            raise Rejected(
                "antigravity_session_stuck_in_editor",
                f"could not return tmux session {session!r} to the chat prompt after the "
                "permissions editor. Anything sent to it now would be read as editor input, "
                "so it must not be messaged until a human closes the view.",
            )

    def _type_rule(self, session: str, rule: str) -> None:
        """Press the add key, type one rule, and confirm it.

        The add screen is confirmed before the rule is typed. `-l` is used so
        nothing in the rule -- the parentheses especially -- is read as a key
        name.
        """
        self._tmux("send-keys", "-t", session, "a")
        if self._await_pane(session, _ADD_RULE_MARKERS) is None:
            raise Rejected(
                "permissions_view_did_not_open",
                f"pressed the add key in {session!r} but the pane never showed the rule "
                "input; refusing to type a rule into an unknown screen.",
            )
        result = self._tmux("send-keys", "-t", session, "-l", rule)
        if result.returncode != 0:
            raise Rejected(
                "antigravity_session_unreachable",
                f"could not type a rule into tmux session {session!r}: {result.stderr.strip()}",
            )
        self._tmux("send-keys", "-t", session, "Enter")
        self._await_pane(session, _PERMISSION_VIEW_MARKERS)

    def add_permissions(self, *, partner_id_in_remote: str, rules: list[str]) -> None:
        """Add rules through the `/permissions` editor, then let the caller verify.

        This method does NOT report success. It types, and the caller reads
        `get_permissions` back from the project config file to find out what
        actually landed -- see `MessagingCore._apply_and_verify`. That split
        is deliberate: this is the one operation in the adapter that cannot
        confirm its own result, because a TUI accepts keystrokes whether or
        not they mean anything, and a method that returned "ok" here would be
        reporting that it typed rather than that it worked.
        """
        session = self._session_name(partner_id_in_remote)
        self._open_permissions_view(session)
        try:
            for rule in rules:
                self._type_rule(session, rule)
        except BaseException:
            # Close, but never let the close replace the reason we are here.
            self._close_quietly(session)
            raise
        # Success: a failure to close IS the story, so let it propagate.
        self._close_permissions_view(session)

    def delete_permissions(self, *, partner_id_in_remote: str, rules: list[str]) -> None:
        """Remove rules through the `/permissions` editor, one at a time, by name.

        The editor's footer advertises `d/⌫ Delete rule`, and `d` deletes the
        **selected** rule immediately with no confirmation. So the whole
        difficulty is selection: pressing `d` without first moving the cursor
        onto the intended rule deletes whichever one happens to be highlighted.

        This therefore re-reads the list before every deletion, finds the target
        by its exact text, and moves the cursor there with `↑`/`↓` -- rather than
        pressing a key as many times as there are rules and trusting the order.
        A rule that is not listed is skipped, not guessed at.
        """
        session = self._session_name(partner_id_in_remote)
        self._open_permissions_view(session)
        try:
            for rule in rules:
                # Re-read the list before EVERY deletion. The previous one
                # renumbered it and moved the cursor, and a pane captured before
                # that is a list that no longer exists -- which is how the first
                # version of this method computed a correct-looking index into
                # stale rows and then refused, correctly, at the safety check.
                time.sleep(_KEY_SETTLE * 2)
                pane = self._capture(session)
                listed = self._listed_rules(pane)
                if rule not in listed:
                    continue
                target = listed.index(rule)
                current = self._selected_index(pane)
                key = "Down" if target > current else "Up"
                for _ in range(abs(target - current)):
                    self._tmux("send-keys", "-t", session, key)
                    time.sleep(_KEY_SETTLE)

                # Confirm the cursor is on the intended rule before pressing a
                # key that deletes without asking. If it is not, refuse: deleting
                # the wrong permission is worse than deleting none.
                moved = self._capture(session)
                moved_rules = self._listed_rules(moved)
                selected = self._selected_index(moved)
                if not moved_rules or moved_rules[selected] != rule:
                    raise Rejected(
                        "permissions_view_did_not_open",
                        f"could not move the cursor onto {rule!r} in {session!r} (it is on "
                        f"{moved_rules[selected] if moved_rules else 'nothing'!r}); refusing "
                        "to press delete on whatever is selected instead.",
                    )
                self._tmux("send-keys", "-t", session, "d")
        except BaseException:
            self._close_quietly(session)
            raise
        self._close_permissions_view(session)

    def poll_completion(self, *, partner_id_in_remote: str) -> bool:
        """Inspect the pane and decide idle vs. busy.

        Raises `Rejected("approval_is_an_error", ...)` if the pane shows a
        blocking approval/permission prompt -- that is never treated as
        "busy" or answered here; see the module docstring.
        """
        session = self._session_name(partner_id_in_remote)
        result = self._tmux("capture-pane", "-t", session, "-p")
        if result.returncode != 0:
            raise Rejected(
                "antigravity_session_unreachable",
                f"could not read tmux session {session!r} for conversation "
                f"{partner_id_in_remote!r}: {result.stderr.strip()}",
            )
        pane = result.stdout.lower()

        if any(marker in pane for marker in _APPROVAL_PROMPT_MARKERS):
            raise Rejected(
                "approval_is_an_error",
                f"Antigravity conversation {partner_id_in_remote!r} is blocked on an "
                "approval/permission prompt. That is always an error, never a question "
                "for this extension to answer -- there is no method here that responds "
                "to one. The only correct remedy is to interrupt the turn "
                "(stop_remote_execution), reply with an [ERROR] message naming what was "
                "missing, correct the grant with add_permissions, and send the work again.",
            )

        if _FOOTER_BUSY_MARKER in pane:
            return False
        return True

    def read_remote_result(self, *, partner_id_in_remote: str) -> str:
        """Screen-scrape the pane for whatever agy printed after the last prompt.

        There is no API for this: the only readable surface Antigravity
        offers at all is the tmux pane, so this is a best-effort read of a
        TUI transcript, not a structured result, and it degrades to
        "the whole pane" rather than failing outright when it can't do
        better (see below). Captured with `history=500` scrollback (see
        `_capture`) so an answer long enough to have scrolled off the
        currently visible screen is not lost.

        The pane has no structure to key off of -- no delimiter marks where
        an answer begins -- except that agy echoes back whatever was typed
        into it when the screen repaints. So the last body this adapter typed
        into the session (`_last_delivered`, set by `deliver_message`) is
        used as a start marker: its first non-empty line is searched for, and
        the LAST pane line containing it is taken, since the same text can
        already appear earlier in the transcript (e.g. quoted back by agy
        itself) and only the most recent echo actually marks where THIS
        turn's reply starts. If no body was ever recorded for this session,
        or its echo cannot be found (scrolled past even the extended history,
        or agy reformatted it beyond recognition), the whole captured pane is
        kept instead -- a whole pane is a poor answer but a real one, and
        guessing a narrower slice with nothing to anchor it would be worse.

        What's kept still has agy's own UI chrome trailing it -- blank lines,
        the idle/busy footer, a bare pane border -- which is stripped before
        returning, looped rather than checked once, since removing one layer
        (say a blank line) can expose another (the footer beneath it). An
        empty result is a real answer, not a failure.
        """
        session = self._session_name(partner_id_in_remote)
        lines = self._capture(session, history=500).splitlines()

        start_index = 0
        last_body = self._last_delivered.get(session)
        if last_body is not None:
            first_line = next((line for line in last_body.splitlines() if line.strip()), "")
            if first_line:
                echo_index = None
                for index, line in enumerate(lines):
                    if first_line in line:
                        echo_index = index
                if echo_index is not None:
                    start_index = echo_index + 1

        kept = lines[start_index:]
        while kept:
            candidate = kept[-1]
            lowered = candidate.lower()
            if not candidate.strip():
                kept.pop()
                continue
            if _FOOTER_IDLE_MARKER in lowered or _FOOTER_BUSY_MARKER in lowered:
                kept.pop()
                continue
            if all(ch in _CHROME_ONLY_CHARS for ch in candidate):
                kept.pop()
                continue
            break

        return "\n".join(kept).strip()
