"""`adapters.registry`: the one seam that turns a `source_prefix` into a real
`RemoteExtension`.

This is the ONLY place a `source_prefix` is mapped to a concrete adapter
class. `mcp_server.config.build_extension` delegates to `build_extension`
here for every real source rather than re-implementing this dispatch --
two functions doing the same mapping, with different answers, is how a
production server previously ended up talking to stubs unnoticed.

``"code_"`` gets no adapter on purpose: Claude Code, running locally, has no
messaging presence of its own to be reached through a `RemoteExtension` --
there is nothing on the other end of that prefix to build a client for.
"""

from __future__ import annotations

from extension.base import RemoteExtension
from messaging_core.errors import Rejected

from .antigravity.adapter import AntigravityExtension
from .claude_science.adapter import ClaudeScienceExtension
from .notebooklm.adapter import NotebookLMExtension

__all__ = ["build_extension"]

_BUILDERS: dict[str, type[RemoteExtension]] = {
    "nlm_": NotebookLMExtension,
    "science_": ClaudeScienceExtension,
    "gemini_": AntigravityExtension,
}


def build_extension(source_prefix: str, **kwargs) -> RemoteExtension:
    """Return the real `RemoteExtension` for `source_prefix`.

    Any keyword arguments are forwarded to the concrete adapter's
    constructor (e.g. `nlm_path=`, `base_url=`, `cookie=`, `tmux_path=`).

    Raises:
        Rejected: if `source_prefix == "code_"` (no adapter exists -- Claude
            Code has no messaging presence of its own), or if `source_prefix`
            is not one of the four recognized prefixes at all.
    """
    if source_prefix == "code_":
        raise Rejected(
            "no_adapter_for_code",
            "'code_' has no RemoteExtension: Claude Code has no messaging presence "
            "of its own for an adapter to reach -- there is nothing remote to build "
            "a client for.",
        )
    builder = _BUILDERS.get(source_prefix)
    if builder is None:
        raise Rejected(
            "unknown_source_prefix",
            f"{source_prefix!r} is not a recognized source prefix; expected one of "
            f"{tuple(_BUILDERS)} or 'code_' (which has no adapter).",
        )
    return builder(**kwargs)
