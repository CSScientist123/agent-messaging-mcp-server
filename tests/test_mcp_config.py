"""Tests for `mcp_server.config.build_extension`.

The defect this guards against: a production MCP server silently running
against `StubExtension` for every source because `mcp_server.config` carried
its own hard-coded stub-only dispatch instead of delegating to
`adapters.registry.build_extension`. The default must be the real adapter;
a stub must require an explicit opt-in.
"""

from __future__ import annotations

import pytest

from adapters.claude_science.adapter import ClaudeScienceExtension
from extension.base import StubExtension
from mcp_server.config import ENV_STUB, build_extension
from messaging_core.errors import Rejected


def test_build_extension_defaults_to_the_real_adapter(monkeypatch):
    monkeypatch.delenv(ENV_STUB, raising=False)
    ext = build_extension("science_")
    assert isinstance(ext, ClaudeScienceExtension)
    assert not isinstance(ext, StubExtension)


def test_build_extension_returns_stub_only_when_env_var_is_set(monkeypatch):
    monkeypatch.setenv(ENV_STUB, "1")
    ext = build_extension("science_")
    assert isinstance(ext, StubExtension)
    assert ext.source_prefix == "science_"


def test_build_extension_code_has_no_adapter_even_by_default(monkeypatch):
    monkeypatch.delenv(ENV_STUB, raising=False)
    with pytest.raises(Rejected) as exc_info:
        build_extension("code_")
    assert exc_info.value.code == "no_adapter_for_code"
