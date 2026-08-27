"""Env-driven wiring for one MCP server process: Database + MessagingCore + extension.

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

import os

from adapters.registry import build_extension as _build_real_extension
from extension.base import RemoteExtension, StubExtension
from messaging_core.core import MessagingCore
from messaging_core.db import Database

ENV_SOURCE = "MESSAGING_MCP_SOURCE"
ENV_DB_PATH = "MESSAGING_MCP_DB"
ENV_STUB = "MESSAGING_MCP_STUB"

KNOWN_SOURCES: tuple[str, ...] = ("nlm_", "code_", "science_", "gemini_")


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


def build_extension(source_prefix: str) -> RemoteExtension:
    """Return the `RemoteExtension` a live MCP server process wires up.

    Delegates to `adapters.registry.build_extension` for every real source --
    that registry is the one place a `source_prefix` maps to a concrete
    adapter class, and this function does not re-implement that mapping.

    The only behavior added here is the stub escape hatch: set
    `MESSAGING_MCP_STUB=1` in the environment to get a `StubExtension`
    instead (e.g. for a local/manual run that must not touch a live remote).
    Left unset -- the default, and what production always does -- this
    always returns the real adapter.

    `"code_"` has no real adapter; `adapters.registry.build_extension` raises
    `Rejected("no_adapter_for_code", ...)` for it, and that exception is
    allowed to propagate unchanged.
    """
    if os.environ.get(ENV_STUB):
        return StubExtension(source_prefix=source_prefix)
    return _build_real_extension(source_prefix)


def build_core_from_env() -> MessagingCore:
    """Build a `MessagingCore` wired entirely from environment configuration."""
    source_prefix = source_prefix_from_env()
    db_path = os.environ.get(ENV_DB_PATH)
    db = Database(path=db_path) if db_path else Database()
    extension = build_extension(source_prefix)
    return MessagingCore(db, extension)
