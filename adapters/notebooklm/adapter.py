"""NotebookLM adapter: a :class:`~extension.base.NonExecutingExtension` that drives
Google NotebookLM through the local ``nlm`` command-line tool.

NotebookLM is a source of context, never an actor: a notebook answers
questions about the material loaded into it, but it never "does work" the
way a coding agent or a chat partner does. That is exactly what
:class:`~extension.base.NonExecutingExtension` exists to model -- this
adapter only has to implement verification, delivery, and result-reading;
"stop" and "resume" stay the clean refusals the base class already provides.

Everything here shells out to the ``nlm`` CLI via :func:`subprocess.run`. If
that binary cannot be found, every method that would need it raises
:class:`NlmBinaryMissing` up front rather than doing something that looks
like it worked but silently didn't.

Ground truth is ``notebooklm_server.py`` (the real MCP server wrapping this
same ``nlm`` CLI), not the commands originally guessed here. Two corrections
matter most:

- **There is no ``nlm source list`` subcommand.** The real CLI surface
  wrapped by ``notebooklm_server.py`` has no command that lists a
  notebook's sources on their own -- the closest real thing is ``notebook
  get <notebook>``, whose own docstring there says it returns "details and
  the source list for one notebook". ``verify_partner_id_in_remote`` uses
  that instead.
- **There is no per-source query or chat surface at all.** The real
  ``notebook query <notebook> <question>`` (`notebook_query` in
  ``notebooklm_server.py``) takes a *notebook* id, not a source id, and has
  no ``--source`` flag -- nothing in the ground truth supports scoping a
  query to one source. Per ``notebooklm_server.py``'s own instructions to
  its caller, disambiguating between sources in a multi-source notebook is
  done by naming the specific document (title + author) *inside the
  question text itself*, not through any CLI flag. Likewise, there is no
  ``notebook chat --source --latest``; the real way to read back the latest
  answer is ``chats get <notebook> --json``, whose payload is a dict keyed
  by ``"transcript"`` (a list of turn objects for the notebook's active
  conversation) -- see ``notebook_get_all_chats``/``notebook_get_latest_chats``.

Both of those real commands take a *notebook* id, but this extension's
``deliver_message``/``read_remote_result`` are only ever given
``partner_id_in_remote`` (a source id -- see ``extension.base.RemoteExtension``),
never the notebook id (``project_system_id``). This is solved the same way
the Claude Science adapter in this same package solves the analogous gap:
``verify_partner_id_in_remote`` IS given both ids, and messaging_core always
calls it once, when a partner is registered, before any delivery -- so it
caches the source's notebook id for later calls to consult (see
``_notebook_id_by_source``). A cache miss raises ``NlmNotebookIdUnknown``
rather than guessing or silently querying the wrong notebook.

Even once the right notebook is found, there is no way for this adapter to
scope a query to just one of its sources -- the real CLI has no such
concept. Whatever `body` text is sent is asked of the *whole notebook*; if
the notebook has several sources and the caller needs an answer grounded in
one specific document, naming that document inside `body` itself (title +
author -- see `notebooklm_server.py`'s own strategy) is the caller's
responsibility, not something this adapter can do on its own, since it only
ever sees an opaque source id, never a source's title or author.

A quirk of the remote drives the shape of :meth:`deliver_message` and
:meth:`read_remote_result`: a ``notebook query`` call reports failure (a
non-zero exit code, or an error string on stdout/stderr) on the large
majority of calls that in fact went through and produced a real answer on
NotebookLM's side. Retrying on that reported failure would mean re-asking a
question that was already answered, multiplying notebook chat history for
no reason and risking the retried call itself being mis-reported the same
way forever. The working protocol -- confirmed against real usage, not
guessed, and unchanged by the routing corrections above -- is: fire the
query exactly once, do not inspect its exit code as a signal of anything,
wait a bounded amount of time for NotebookLM to actually finish processing,
and then separately harvest the real answer from the latest chat turn. That
wait/harvest split is implemented across two different `RemoteExtension`
methods (`deliver_message` does the wait, `read_remote_result` does the
harvest) because that is the shape the abstract contract offers -- the
Polling Server calls `deliver_message` once per turn and later calls
`read_remote_result` once it believes the turn is done (which, per
`poll_completion` below, is immediately: NotebookLM has no independent
notion of "still running").
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time

from extension.base import NonExecutingExtension

__all__ = ["NotebookLMExtension", "NlmBinaryMissing", "NlmNotebookIdUnknown"]


class NlmBinaryMissing(RuntimeError):
    """The ``nlm`` CLI is not on PATH (and no explicit path was given)."""

    def __init__(self, detail: str = "") -> None:
        message = "the 'nlm' CLI was not found on PATH (checked shutil.which('nlm'))"
        if detail:
            message = f"{message}: {detail}"
        message += (
            "; install NotebookLM's CLI or construct NotebookLMExtension(nlm_path=...) "
            "with an explicit path."
        )
        super().__init__(message)


class NlmNotebookIdUnknown(RuntimeError):
    """`deliver_message`/`read_remote_result` need to know which notebook
    `partner_id_in_remote` (a source id) belongs to, but no
    `verify_partner_id_in_remote` call has ever cached one for it on this
    extension instance (see the module docstring) -- the real `nlm` CLI has
    no command that queries or looks up a notebook FROM a source id alone.
    """

    def __init__(self, partner_id_in_remote: str) -> None:
        super().__init__(
            f"Don't know which NotebookLM notebook source {partner_id_in_remote!r} "
            "belongs to -- `nlm notebook query`/`nlm chats get` both take a notebook "
            "id, not a source id, and this adapter only learns a source's notebook id "
            "from verify_partner_id_in_remote (which messaging_core calls once, at "
            "partner registration). Register this partner in this same process "
            "first, or call verify_partner_id_in_remote directly, before delivering "
            "to it."
        )


class NotebookLMExtension(NonExecutingExtension):
    """RemoteExtension for NotebookLM notebooks, spoken to via the ``nlm`` CLI.

    ``project_system_id`` is a NotebookLM notebook id. ``partner_id_in_remote``
    is a source id within that notebook -- but see the module docstring: the
    real CLI has no way to scope a query to one source, only to a whole
    notebook, so every source under the same notebook resolves to the exact
    same underlying `nlm notebook query <notebook_id> ...` call.
    """

    source_prefix = "nlm_"

    def __init__(self, *, nlm_path: str | None = None, harvest_wait_seconds: float = 20.0) -> None:
        """
        Args:
            nlm_path: Explicit path to the ``nlm`` binary. If omitted, resolved
                via ``shutil.which("nlm")`` on every call (not cached), so a
                test can monkeypatch `shutil.which` freely regardless of when
                this extension was constructed.
            harvest_wait_seconds: How long `deliver_message` waits after firing
                a query before returning, to give NotebookLM real time to
                finish before `read_remote_result` harvests the answer. Tests
                should pass ``0`` here.
        """
        self._nlm_path_override = nlm_path
        self.harvest_wait_seconds = harvest_wait_seconds
        self._delivery_count = 0
        # partner_id_in_remote (source id) -> project_system_id (notebook
        # id). Populated by verify_partner_id_in_remote; see module docstring.
        self._notebook_id_by_source: dict[str, str] = {}

    # -- binary + subprocess plumbing ------------------------------------

    def _resolve_binary(self) -> str:
        path = self._nlm_path_override or shutil.which("nlm")
        if path is None:
            raise NlmBinaryMissing()
        return path

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        binary = self._resolve_binary()
        return subprocess.run([binary, *args], capture_output=True, text=True)

    def _require_notebook_id(self, partner_id_in_remote: str) -> str:
        notebook_id = self._notebook_id_by_source.get(partner_id_in_remote)
        if notebook_id is None:
            raise NlmNotebookIdUnknown(partner_id_in_remote)
        return notebook_id

    # -- RemoteExtension surface ------------------------------------------

    def verify_project_system_id(self, project_system_id: str) -> bool:
        """`nlm notebook get <id>` -- exit 0 means the notebook exists."""
        result = self._run("notebook", "get", project_system_id)
        return result.returncode == 0

    def verify_partner_id_in_remote(self, project_system_id: str, partner_id_in_remote: str) -> bool:
        """The source must appear in that notebook's own source list.

        Uses `nlm notebook get <notebook_id>` -- there is no separate
        `nlm source list` command in the real CLI (that was a guess);
        `notebook get`'s own real docstring in `notebooklm_server.py` says
        it returns "details and the source list for one notebook", so this
        is the real substitute. Still a substring check rather than a JSON
        parse, tolerating either a plain-text or JSON listing format.

        On success, also caches `project_system_id` as this source's
        notebook id, since `deliver_message`/`read_remote_result` need it
        later and have no other way to learn it (see module docstring).
        """
        result = self._run("notebook", "get", project_system_id)
        if result.returncode != 0:
            return False
        found = partner_id_in_remote in result.stdout
        if found:
            self._notebook_id_by_source[partner_id_in_remote] = project_system_id
        return found

    def deliver_message(self, *, partner_id_in_remote: str, behavior: str, body: str) -> str:
        """Fire a `notebook query <notebook_id> <question>` once, and wait.

        The real CLI has no `--source` flag (that was a guess) -- queries
        are addressed to a whole notebook, found here via the id cached by
        `verify_partner_id_in_remote` (see module docstring; raises
        `NlmNotebookIdUnknown` on a cache miss rather than guessing).

        Does NOT branch on `result.returncode` or inspect stdout/stderr for
        "did it work" -- see the module docstring for why a reported failure
        here is not trustworthy. There is exactly one subprocess call in this
        method; the harvest of the actual answer happens later, in
        `read_remote_result`.
        """
        notebook_id = self._require_notebook_id(partner_id_in_remote)
        self._run("notebook", "query", notebook_id, body)
        # Bounded wait, never a retry loop: give NotebookLM time to actually
        # finish before anyone calls read_remote_result.
        if self.harvest_wait_seconds:
            time.sleep(self.harvest_wait_seconds)
        self._delivery_count += 1
        return f"nlm-query-{partner_id_in_remote}-{self._delivery_count}"

    def poll_completion(self, *, partner_id_in_remote: str) -> bool:
        """A notebook query is synchronous from our side -- always done."""
        return True

    def read_remote_result(self, *, partner_id_in_remote: str) -> str:
        """Harvest the answer from the notebook's latest chat turn.

        Real command: `nlm chats get <notebook_id> --json` -- there is no
        `notebook chat --source --latest` (that was a guess). The real
        payload is a dict keyed by `"transcript"` (a list of turn objects
        for the notebook's active conversation) -- see
        `notebook_get_all_chats`/`notebook_get_latest_chats` in
        `notebooklm_server.py`, which tolerate a `"sessions"` key or a bare
        list too, in case the CLI shape differs.

        NotebookLM answers are never stored by this system -- reading a
        result means asking NotebookLM again, via this CLI call, rather
        than looking anything up locally.
        """
        notebook_id = self._require_notebook_id(partner_id_in_remote)
        result = self._run("chats", "get", notebook_id, "--json")
        raw_stdout = result.stdout.strip()
        if not raw_stdout:
            return ""
        try:
            parsed = json.loads(raw_stdout)
        except json.JSONDecodeError:
            # Not JSON -- something unexpected happened (e.g. an error
            # message on stdout instead of the --json payload). Return it
            # as-is rather than guess a shape it doesn't have.
            return raw_stdout

        if isinstance(parsed, dict) and "transcript" in parsed:
            turns = parsed.get("transcript") or []
        elif isinstance(parsed, dict) and "sessions" in parsed:
            turns = parsed.get("sessions") or []
        elif isinstance(parsed, list):
            turns = parsed
        else:
            return raw_stdout

        if not turns:
            return ""
        latest = turns[-1]
        if isinstance(latest, str):
            return latest.strip()
        if isinstance(latest, dict):
            # UNKNOWN, and left as such rather than guessed at:
            # notebooklm_server.py never names the field a turn's own
            # answer text lives under -- notebook_get_all_chats /
            # notebook_get_latest_chats return these dicts completely raw,
            # with no field access of their own to observe from. Try the
            # most plausible key names rather than committing to one; if
            # none match, return the raw turn as JSON so a caller at least
            # sees real data instead of a silently wrong guess.
            for key in ("text", "content", "answer", "message", "response"):
                value = latest.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return json.dumps(latest)
        return str(latest)
