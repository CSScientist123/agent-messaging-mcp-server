"""Tests for one Antigravity conversation continuing another.

A long research effort outlives a single conversation. A Project holds at most
`source_caps.max_live_partners` live Partners and that ceiling is deliberate, so
work at scale needs more Projects rather than a larger ceiling -- and
`project_extension` is how two Projects are declared parts of one effort.

Inheritance is exactly that and nothing more: a handshake across an extension.
No permissions move, and no queued work moves.

Two rules in the existing chain block it, and both are about roles. A `gemini_`
Partner holds none -- all three orchestrator roles are Claude Science roles --
so `requester_not_orchestrator` fires first, and the cross-project branch past
it demands both ends hold the *same* role. A `gemini_` pair is therefore decided
before either, the way a `code_` pair already is.
"""

from __future__ import annotations

import pytest

from extension.base import StubExtension
from messaging_core.core import MessagingCore
from messaging_core.db import Database
from messaging_core.errors import Rejected

_counter = 0


def _unique(prefix: str) -> str:
    global _counter
    _counter += 1
    return f"inh-{prefix}-{_counter}"


@pytest.fixture
def db():
    database = Database(path=":memory:")
    yield database
    database.close()


@pytest.fixture
def core(db):
    return MessagingCore(db, extension=StubExtension(source_prefix="science_"))


@pytest.fixture
def world(core):
    """A science_ project directing two gemini_ projects, one conversation each."""
    core.extension = StubExtension(source_prefix="science_")
    sci = core.create_project(title=_unique("sci"), source_prefix="science_",
                              project_system_id=_unique("psid"))
    po = core.create_partner(project_id=sci, title=_unique("po"),
                             partner_id_in_remote=_unique("rem"), descr="d")
    core.claim_orchestrator(requester_uuid=po["uuid"], project_id=sci,
                            orchestrator_type="project-orchestrator")
    go = core.create_partner(project_id=sci, title=_unique("go"),
                             partner_id_in_remote=_unique("rem"), descr="d")
    core.claim_orchestrator(requester_uuid=go["uuid"], project_id=sci,
                            orchestrator_type="gemini-orchestrator")
    core.grant_gemini_budget(requester_uuid=po["uuid"], grantee_uuid=go["uuid"],
                             budget_count=3)

    core.extension = StubExtension(source_prefix="gemini_")
    gem_a = core.create_project(title=_unique("gemA"), source_prefix="gemini_",
                                project_system_id=_unique("psid"))
    gem_b = core.create_project(title=_unique("gemB"), source_prefix="gemini_",
                                project_system_id=_unique("psid"))
    first = core.create_partner(project_id=gem_a, title=_unique("conv-first"),
                                partner_id_in_remote=_unique("rem"), descr="d")
    second = core.create_partner(project_id=gem_b, title=_unique("conv-second"),
                                 partner_id_in_remote=_unique("rem"), descr="d")
    sibling = core.create_partner(project_id=gem_a, title=_unique("conv-sibling"),
                                  partner_id_in_remote=_unique("rem"), descr="d")
    third = core.create_partner(project_id=gem_b, title=_unique("conv-third"),
                                partner_id_in_remote=_unique("rem"), descr="d")
    core.extension = StubExtension(source_prefix="science_")
    return {"sci": sci, "po": po, "go": go, "gem_a": gem_a, "gem_b": gem_b,
            "first": first, "second": second, "sibling": sibling, "third": third}


def link(core: MessagingCore, world, requester):
    """Declare the two gemini_ projects extensions of one another."""
    core.extend_project(requester_uuid=requester["uuid"],
                        project_title=_project_title(core, world["gem_b"]))


def _project_title(core: MessagingCore, project_id: int) -> str:
    return core.db.read_one("SELECT title FROM projects WHERE id = ?", (project_id,))["title"]


# ---------------------------------------------------------------------------
# 1. Two conversations in one Project are peers, not a lineage.
# ---------------------------------------------------------------------------


def test_two_conversations_in_one_project_cannot_handshake(core, world):
    """They already answer to the same orchestrator; there is nothing to inherit."""
    with pytest.raises(Rejected) as exc_info:
        core.handshake(requester_uuid=world["sibling"]["uuid"],
                       partner_title=world["first"]["title"])

    assert exc_info.value.code == "no_handshake_between_gemini", (
        f"got {exc_info.value.code!r}"
    )


# ---------------------------------------------------------------------------
# 2. Across an extension it is legal, and no role is required.
# ---------------------------------------------------------------------------


def test_a_conversation_inherits_across_a_project_extension(core, world):
    """The whole point: neither side holds a role, and neither needs one."""
    core.extend_project(requester_uuid=world["go"]["uuid"],
                        project_title=_project_title(core, world["gem_b"]),
                        other_project_title=_project_title(core, world["gem_a"]))

    result = core.handshake(requester_uuid=world["second"]["uuid"],
                            partner_title=world["first"]["title"])

    assert result["to_partner_title"] == world["first"]["title"]


def test_without_an_extension_the_two_projects_stay_separate(core, world):
    with pytest.raises(Rejected) as exc_info:
        core.handshake(requester_uuid=world["second"]["uuid"],
                       partner_title=world["first"]["title"])

    assert exc_info.value.code == "different_project", f"got {exc_info.value.code!r}"


# ---------------------------------------------------------------------------
# 3. A lineage is a line, not a fork.
# ---------------------------------------------------------------------------


def test_a_conversation_can_be_inherited_from_only_once(core, world):
    """So that "which conversation continues this one" has exactly one answer."""
    core.extend_project(requester_uuid=world["go"]["uuid"],
                        project_title=_project_title(core, world["gem_b"]),
                        other_project_title=_project_title(core, world["gem_a"]))
    core.handshake(requester_uuid=world["second"]["uuid"],
                   partner_title=world["first"]["title"])

    with pytest.raises(Rejected) as exc_info:
        core.handshake(requester_uuid=world["third"]["uuid"],
                       partner_title=world["first"]["title"])

    assert exc_info.value.code == "gemini_already_inherited", (
        f"a second successor must be refused; got {exc_info.value.code!r}"
    )


# ---------------------------------------------------------------------------
# 4. Inheriting does not cost the orchestrator its own reach.
# ---------------------------------------------------------------------------


def test_an_inherited_conversation_is_still_reachable_by_its_orchestrator(core, world):
    """`gemini_single_science_source` counts science_ sources, not every inbound row.

    Without that scoping, an inheritance handshake would make the conversation
    permanently unreachable by the orchestrator that pays for it.
    """
    core.extend_project(requester_uuid=world["go"]["uuid"],
                        project_title=_project_title(core, world["gem_b"]),
                        other_project_title=_project_title(core, world["gem_a"]))
    # The successor claims the predecessor FIRST, so the orchestrator's own
    # handshake is the one that has to survive an existing inbound row. Doing
    # it the other way round never exercises the rule: the check only looks at
    # handshakes already pointing AT its target.
    core.handshake(requester_uuid=world["second"]["uuid"],
                   partner_title=world["first"]["title"])

    result = core.handshake(requester_uuid=world["go"]["uuid"],
                            partner_title=world["first"]["title"])

    assert result["to_partner_title"] == world["first"]["title"], (
        "an inherited conversation must still be reachable by the orchestrator that "
        "pays budget for it"
    )


def test_one_conversation_still_serves_only_one_science_master(core, world):
    """The rule that check actually states, unchanged."""
    core.extension = StubExtension(source_prefix="science_")
    sci2 = core.create_project(title=_unique("sci"), source_prefix="science_",
                               project_system_id=_unique("psid"))
    po2 = core.create_partner(project_id=sci2, title=_unique("po"),
                              partner_id_in_remote=_unique("rem"), descr="d")
    core.claim_orchestrator(requester_uuid=po2["uuid"], project_id=sci2,
                            orchestrator_type="project-orchestrator")
    go2 = core.create_partner(project_id=sci2, title=_unique("go"),
                              partner_id_in_remote=_unique("rem"), descr="d")
    core.claim_orchestrator(requester_uuid=go2["uuid"], project_id=sci2,
                            orchestrator_type="gemini-orchestrator")
    core.grant_gemini_budget(requester_uuid=po2["uuid"], grantee_uuid=go2["uuid"],
                             budget_count=2)
    core.handshake(requester_uuid=world["go"]["uuid"], partner_title=world["first"]["title"])

    with pytest.raises(Rejected) as exc_info:
        core.handshake(requester_uuid=go2["uuid"], partner_title=world["first"]["title"])

    assert exc_info.value.code == "gemini_single_science_source"


def test_the_two_project_form_is_reachable_from_the_tool_surface(core, world):
    """A rule an agent cannot invoke is a rule that does not exist for agents.

    The whole inheritance flow depends on a `project_extension` row between two
    gemini_ projects, and only the two-project form of `extend_project` can
    create one. If the tool exposes only the one-project form, the capability
    is reachable from the library and unreachable from anything an agent can
    call -- which is the same as absent.
    """
    import asyncio

    from mcp_server.server import build_server

    server = build_server(name="messaging-test", core=core)
    tool = next(t for t in asyncio.run(server.list_tools()) if t.name == "extend_project")

    assert "other_project_title" in tool.inputSchema["properties"], (
        "the two-project form must be callable by an agent; the tool takes "
        f"{sorted(tool.inputSchema['properties'])}"
    )


def test_a_gemini_orchestrator_links_two_gemini_projects_through_the_tool(core, world):
    """End to end through the surface an agent actually uses."""
    import asyncio

    from mcp_server.server import build_server

    server = build_server(name="messaging-test", core=core)
    result = asyncio.run(server.call_tool("extend_project", {
        "requester_uuid": world["go"]["uuid"],
        "project_title": _project_title(core, world["gem_b"]),
        "other_project_title": _project_title(core, world["gem_a"]),
    }))
    content = result[0] if isinstance(result, tuple) else result
    body = "\n".join(b.text for b in content if hasattr(b, "text"))

    assert body.startswith("[ok]"), f"linking two gemini_ projects failed: {body!r}"
    core.handshake(requester_uuid=world["second"]["uuid"],
                   partner_title=world["first"]["title"])
