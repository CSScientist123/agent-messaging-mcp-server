"""Env-driven wiring for one MCP server process: Database, core, extension, Polling Server.

Reads three environment variables:

``MESSAGING_MCP_SOURCE``
    Required. One of ``"nlm_"``, ``"code_"``, ``"science_"``, ``"gemini_"`` -- which
    remote family this particular MCP server process speaks for. One MCP server
    instance exists per adapter (see ``mcp_server.server.build_server``); this is
    how that one instance knows which extension to load.

``MESSAGING_MCP_DB``
    Optional. A filesystem path (or ``":memory:"``) for the sqlite database. If
    unset, :class:`messaging_core.db.Database` falls back to its own default
    (``messaging_core.config.db_path()``).

``MESSAGING_MCP_STUB``
    Optional. If set to a truthy value (e.g. ``"1"``), :func:`build_extension`
    returns an :class:`extension.base.StubExtension` instead of a real adapter.
    This is an explicit opt-in escape hatch for local/manual runs that must not
    touch a live remote -- production and every ordinary run leave this unset
    and get the real adapter.

:func:`build_extension` is the one seam that turns a ``source_prefix`` into a
:class:`extension.base.RemoteExtension`. The actual `source_prefix -> class`
dispatch lives in exactly one place, :func:`adapters.registry.build_extension`
-- this module does not duplicate that mapping, it only adds the stub
override on top of it. ``"code_"`` has no adapter at all (Claude Code, running
locally, has no messaging presence of its own to be reached through a
`RemoteExtension`); the registry raises `Rejected` for it, and that error is
allowed to surface here unchanged rather than being papered over.
"""

from __future__ import annotations

import functools
import os

from adapters.registry import build_extension as _build_real_extension
from extension.base import RemoteExtension, StubExtension
from messaging_core.core import MessagingCore
from messaging_core.db import Database

ENV_SOURCE = "MESSAGING_MCP_SOURCE"
ENV_DB_PATH = "MESSAGING_MCP_DB"
ENV_STUB = "MESSAGING_MCP_STUB"

KNOWN_SOURCES: tuple[str, ...] = ("nlm_", "code_", "science_", "gemini_")

# The two sources whose adapters keep a partner_id_in_remote -> project_system_id
# cache that is empty after a restart (see NotebookLMExtension._notebook_id_by_source
# and ClaudeScienceExtension._project_id_by_frame) -- these are the only adapter
# constructors that accept `resolve_project_system_id`.
_RESOLVABLE_SOURCES = frozenset({"nlm_", "science_"})

_RESOLVE_SQL = """
    SELECT pr.project_system_id AS psid
      FROM partners p JOIN projects pr ON pr.id = p.project_id
     WHERE p.partner_id_in_remote = ? AND pr.source_prefix = ?
     ORDER BY (p.archived_at IS NULL) DESC, p.id DESC
     LIMIT 1
"""


def _resolve_project_system_id(db: Database, source_prefix: str, partner_id_in_remote: str) -> str | None:
    """Look up the project a partner's remote id belongs to, straight from
    the row `verify_partner_id_in_remote` would already have written on a
    live Partner (see the two adapters' module docstrings). Filtered on
    `source_prefix` because `partner_id_in_remote` is only unique WITHIN a
    project -- an unfiltered lookup could hand back another source's
    container that happens to reuse the same remote id string. Preferring a
    live Partner over an archived one carrying the same remote id is what the
    ORDER BY does.

    Never raises: this backs a cache-miss fallback inside a `RemoteExtension`
    adapter, and an exception escaping here would turn a recoverable miss
    into the exact unrecoverable failure this resolver exists to avoid.
    """
    try:
        row = db.read_one(_RESOLVE_SQL, (partner_id_in_remote, source_prefix))
    except Exception:
        return None
    return row["psid"] if row is not None else None


def source_prefix_from_env() -> str:
    """Read and validate ``MESSAGING_MCP_SOURCE``.

    Raises:
        ValueError: if the variable is unset or not one of `KNOWN_SOURCES`.
    """
    value = os.environ.get(ENV_SOURCE)
    if value is None:
        raise ValueError(f"{ENV_SOURCE} is not set; expected one of {KNOWN_SOURCES}.")
    if value not in KNOWN_SOURCES:
        raise ValueError(f"{ENV_SOURCE}={value!r} is not one of {KNOWN_SOURCES}.")
    return value


def build_extension(source_prefix: str, db: Database | None = None) -> RemoteExtension:
    """Return the `RemoteExtension` a live MCP server process wires up.

    Delegates to `adapters.registry.build_extension` for every real source --
    that registry is the one place a `source_prefix` maps to a concrete
    adapter class, and this function does not re-implement that mapping.

    The only behavior added here is the stub escape hatch: set
    `MESSAGING_MCP_STUB=1` in the environment to get a `StubExtension`
    instead (e.g. for a local/manual run that must not touch a live remote).
    Left unset -- the default, and what production always does -- this
    always returns the real adapter.

    `db` is optional and, when given, is only used for `source_prefix` in
    `_RESOLVABLE_SOURCES`: those adapters get a `resolve_project_system_id`
    callable so a `partner_id_in_remote` -> `project_system_id` cache miss --
    inevitable in a fresh process, since that cache is only ever populated by
    `verify_partner_id_in_remote` -- can self-heal from the database instead
    of failing forever (see the two adapters' module docstrings). Omitting
    `db` leaves those adapters exactly as they were before this argument
    existed, which is what the stub path and any other existing caller need.
    """
    if os.environ.get(ENV_STUB):
        return StubExtension(source_prefix=source_prefix)
    if db is not None and source_prefix in _RESOLVABLE_SOURCES:
        resolver = functools.partial(_resolve_project_system_id, db, source_prefix)
        return _build_real_extension(source_prefix, resolve_project_system_id=resolver)
    return _build_real_extension(source_prefix)


def build_core_from_env() -> MessagingCore:
    """Build a `MessagingCore` wired entirely from environment configuration."""
    core, _polling = build_stack_from_env()
    return core


def build_stack_from_env() -> tuple[MessagingCore, "PollingServer"]:
    """Build the whole running stack: a core AND the Polling Server behind it.

    Both halves, together, because neither is any use alone. The core admits a
    message and hands it to a remote; the Polling Server is what waits for the
    remote to finish and pushes the answer back into the Caller's queue. A
    process holding only the core delivers messages that are never followed up:
    a `[QUERY]` is handed over and abandoned, a `[RESEARCH]` never reaches its
    summary, and nothing errors, because from the core's point of view the
    message was delivered exactly as asked.

    The two share ONE `WorkingSlots`. That is the whole reason this function
    exists rather than two independent constructors: the working slot is the
    single fact both halves reason about, and two objects each holding their own
    copy of it is not a slot -- the core would admit against one view and the
    drain thread would advance against another.

    The Polling Server is given the extension under this process's own
    `source_prefix`. One process speaks for one remote family, so its drain
    threads only ever serve Partners of that family; a Partner of another source
    is another process's business, reached through the same database.
    """
    from polling.server import PollingServer

    source_prefix = source_prefix_from_env()
    db_path = os.environ.get(ENV_DB_PATH)
    db = Database(path=db_path) if db_path else Database()
    extension = build_extension(source_prefix, db=db)

    core = MessagingCore(db, extension)
    polling = PollingServer(db, extensions={source_prefix: extension}, core=core)
    return core, polling
