"""Tests for the `extension.base` boundary: `RemoteExtension`,
`NonExecutingExtension`, and `StubExtension`.

`RemoteExtension` now has exactly four abstract methods
(`verify_project_system_id`, `verify_partner_id_in_remote`, `deliver_message`,
`stop_remote_execution`); everything else on it is concrete and refuses by
default (`NeedsRemote` for `poll_completion`/`read_remote_result`,
`Rejected("not_path_configurable", ...)` for the permission trio).
`resume_remote_execution` does not exist anywhere in this module -- there is
deliberately no resume operation (see the module's own docstring: correcting
a grant and sending the work again IS the resumption).
"""

from __future__ import annotations

import inspect

import pytest

from extension.base import NonExecutingExtension, RemoteExtension, StubExtension
from messaging_core.errors import NeedsRemote, Rejected

#: The only four methods a concrete remote MUST supply for itself.
EXPECTED_ABSTRACT_METHODS = frozenset(
    {
        "verify_project_system_id",
        "verify_partner_id_in_remote",
        "deliver_message",
        "stop_remote_execution",
    }
)

#: The complete public method surface of RemoteExtension: the four abstract
#: methods above, plus the five concrete-but-refusing-by-default ones. Used
#: below to prove, by enumeration, that nothing else (in particular nothing
#: that "answers a permission prompt") is present on any of these classes.
EXPECTED_PUBLIC_METHODS = EXPECTED_ABSTRACT_METHODS | frozenset(
    {
        "get_permissions",
        "add_permissions",
        "delete_permissions",
        "poll_completion",
        "read_remote_result",
    }
)


def _public_methods(cls: type) -> set[str]:
    return {
        name
        for name, _ in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    }


def _make_partial_extension(omit: str | None):
    """A RemoteExtension subclass implementing all four abstract methods
    except `omit` (or all four, if `omit` is None). Trivial no-op bodies --
    only whether the class CAN be instantiated is under test here."""
    members = {}
    for name in EXPECTED_ABSTRACT_METHODS:
        if name == omit:
            continue
        members[name] = lambda self, *args, **kwargs: None
    return type(f"Partial_missing_{omit}", (RemoteExtension,), members)


class _MinimalExtension(RemoteExtension):
    """Implements only the four abstract methods, so the concrete defaults
    for everything else can be exercised untouched."""

    source_prefix = "code_"

    def verify_project_system_id(self, project_system_id: str) -> bool:
        return True

    def verify_partner_id_in_remote(self, project_system_id: str, partner_id_in_remote: str) -> bool:
        return True

    def deliver_message(self, *, partner_id_in_remote: str, behavior: str, body: str) -> str:
        return "call-1"

    def stop_remote_execution(self, *, partner_id_in_remote: str, reason: str) -> None:
        return None


class _MinimalNonExecuting(NonExecutingExtension):
    """Supplies the three methods NonExecutingExtension leaves abstract, so
    its own `stop_remote_execution` override can be exercised untouched."""

    source_prefix = "nlm_"

    def verify_project_system_id(self, project_system_id: str) -> bool:
        return True

    def verify_partner_id_in_remote(self, project_system_id: str, partner_id_in_remote: str) -> bool:
        return True

    def deliver_message(self, *, partner_id_in_remote: str, behavior: str, body: str) -> str:
        return "call-1"


# ---------------------------------------------------------------------------
# RemoteExtension: exactly four abstract methods, nothing more, nothing less.
# ---------------------------------------------------------------------------


def test_remote_extension_has_exactly_four_abstract_methods():
    assert RemoteExtension.__abstractmethods__ == EXPECTED_ABSTRACT_METHODS, (
        f"expected exactly {sorted(EXPECTED_ABSTRACT_METHODS)} to be abstract, got "
        f"{sorted(RemoteExtension.__abstractmethods__)}"
    )


def test_remote_extension_itself_cannot_be_instantiated():
    with pytest.raises(TypeError):
        RemoteExtension()


@pytest.mark.parametrize("omitted", sorted(EXPECTED_ABSTRACT_METHODS))
def test_a_subclass_missing_any_one_abstract_method_cannot_be_instantiated(omitted):
    cls = _make_partial_extension(omit=omitted)
    with pytest.raises(TypeError):
        cls()


def test_a_subclass_implementing_all_four_can_be_instantiated():
    cls = _make_partial_extension(omit=None)
    instance = cls()
    assert isinstance(instance, RemoteExtension)


# ---------------------------------------------------------------------------
# resume_remote_execution is gone. SHAPE, not behaviour, deliberately: this
# is one of the two absence checks called out by name in this file's brief.
# The property worth pinning is that nothing still answers to that name at
# all, not merely that calling it would fail -- so this checks presence, not
# what happens when it's called.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", [RemoteExtension, NonExecutingExtension, StubExtension])
def test_resume_remote_execution_does_not_exist(cls):
    assert not hasattr(cls, "resume_remote_execution"), (
        f"{cls.__name__} still defines resume_remote_execution; it was dropped from the "
        "design -- correcting a grant and sending the work again IS the resumption."
    )


# ---------------------------------------------------------------------------
# The concrete defaults refuse rather than fabricating a remote answer.
# ---------------------------------------------------------------------------


def test_poll_completion_default_raises_needs_remote():
    ext = _MinimalExtension()
    with pytest.raises(NeedsRemote):
        ext.poll_completion(partner_id_in_remote="r1")


def test_read_remote_result_default_raises_needs_remote():
    ext = _MinimalExtension()
    with pytest.raises(NeedsRemote):
        ext.read_remote_result(partner_id_in_remote="r1")


@pytest.mark.parametrize(
    "method,kwargs",
    [
        ("get_permissions", {"partner_id_in_remote": "r1"}),
        ("add_permissions", {"partner_id_in_remote": "r1", "rules": ["read_file(/x)"]}),
        ("delete_permissions", {"partner_id_in_remote": "r1", "rules": ["read_file(/x)"]}),
    ],
)
def test_permission_defaults_refuse_with_not_path_configurable(method, kwargs):
    ext = _MinimalExtension()
    with pytest.raises(Rejected) as exc_info:
        getattr(ext, method)(**kwargs)
    assert exc_info.value.code == "not_path_configurable", (
        f"expected code 'not_path_configurable' from the default {method}, got "
        f"{exc_info.value.code!r}"
    )


# ---------------------------------------------------------------------------
# NonExecutingExtension: stop_remote_execution refuses; the other three
# abstract methods are left for a concrete remote to supply.
# ---------------------------------------------------------------------------


def test_non_executing_extension_is_still_abstract():
    with pytest.raises(TypeError):
        NonExecutingExtension()


def test_non_executing_extension_leaves_the_other_three_abstract():
    expected = EXPECTED_ABSTRACT_METHODS - {"stop_remote_execution"}
    assert NonExecutingExtension.__abstractmethods__ == expected, (
        f"expected {sorted(expected)} still abstract on NonExecutingExtension, got "
        f"{sorted(NonExecutingExtension.__abstractmethods__)}"
    )


def test_non_executing_extension_refuses_stop_remote_execution():
    ext = _MinimalNonExecuting()
    with pytest.raises(Rejected) as exc_info:
        ext.stop_remote_execution(partner_id_in_remote="r1", reason="testing")
    assert exc_info.value.code == "not_executable", (
        f"expected code 'not_executable', got {exc_info.value.code!r}"
    )


# ---------------------------------------------------------------------------
# StubExtension: fully concrete, including the permission trio.
# ---------------------------------------------------------------------------


def test_stub_extension_is_fully_concrete():
    assert StubExtension.__abstractmethods__ == frozenset(), (
        f"StubExtension must implement every abstract method; still abstract: "
        f"{sorted(StubExtension.__abstractmethods__)}"
    )
    StubExtension(source_prefix="code_")  # must not raise


def test_stub_extension_implements_the_permission_trio_without_refusing():
    ext = StubExtension(source_prefix="gemini_")
    ext.add_permissions(partner_id_in_remote="r1", rules=["read_file(/a)"])
    allowed = ext.get_permissions(partner_id_in_remote="r1")
    assert allowed == ["read_file(/a)"], (
        f"expected the granted rule to be readable back, got: {allowed}"
    )
    ext.delete_permissions(partner_id_in_remote="r1", rules=["read_file(/a)"])
    allowed_after = ext.get_permissions(partner_id_in_remote="r1")
    assert allowed_after == [], f"expected the rule to be gone after delete, got: {allowed_after}"


def test_stub_extension_poll_completion_and_read_remote_result_do_not_raise():
    ext = StubExtension(source_prefix="code_")
    ext.completed = False
    assert ext.poll_completion(partner_id_in_remote="r1") is False, (
        "StubExtension.poll_completion must return .completed, not raise NeedsRemote like the "
        "base default -- a stub has to be able to stand in for ANY remote, executing or not"
    )
    ext.completed = True
    assert ext.poll_completion(partner_id_in_remote="r1") is True

    ext.read_remote_result_value = "the answer"
    result = ext.read_remote_result(partner_id_in_remote="r1")
    assert result == "the answer", f"expected the configured value back, got: {result!r}"


def test_stub_extension_records_every_call_as_name_and_kwargs():
    ext = StubExtension(source_prefix="code_")
    ext.deliver_message(partner_id_in_remote="r1", behavior="[QUERY]", body="hi")
    assert ext.calls == [
        (
            "deliver_message",
            {"partner_id_in_remote": "r1", "behavior": "[QUERY]", "body": "hi"},
        )
    ], f"expected exactly one recorded (name, kwargs) call, got: {ext.calls}"


def test_stub_extension_permissions_refuse_makes_add_silently_not_apply():
    """The fixture MessagingCore._apply_and_verify's verify-after-write path
    depends on: a write the remote appears to accept, that does not actually
    land -- unreachable with a stub that always cooperates."""
    ext = StubExtension(source_prefix="gemini_")
    ext.permissions_refuse.add("write_file(/blocked)")

    ext.add_permissions(partner_id_in_remote="r1", rules=["write_file(/blocked)", "read_file(/ok)"])

    allowed = ext.get_permissions(partner_id_in_remote="r1")
    assert allowed == ["read_file(/ok)"], (
        f"the refused rule must not appear after add_permissions; got: {allowed}"
    )


def test_stub_extension_permissions_refuse_makes_delete_silently_not_apply():
    ext = StubExtension(source_prefix="gemini_")
    ext.permissions["r1"] = ["write_file(/stuck)"]
    ext.permissions_refuse.add("write_file(/stuck)")

    ext.delete_permissions(partner_id_in_remote="r1", rules=["write_file(/stuck)"])

    allowed = ext.get_permissions(partner_id_in_remote="r1")
    assert allowed == ["write_file(/stuck)"], (
        f"a refused rule must survive delete_permissions untouched; got: {allowed}"
    )


# ---------------------------------------------------------------------------
# Enforcement by absence: no class here has a method that answers a
# permission prompt. SHAPE, not behaviour, deliberately (the second of the
# two absence checks called out by name): permissions are only ever
# configured in advance via add_permissions/delete_permissions and read back
# via get_permissions. The only way to show that no "answer this pending
# prompt" method exists is to enumerate everything that DOES exist and show
# the set matches exactly what the design calls for.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", [RemoteExtension, NonExecutingExtension, StubExtension])
def test_no_class_in_extension_base_answers_a_permission_prompt(cls):
    public = _public_methods(cls)
    assert public == EXPECTED_PUBLIC_METHODS, (
        f"{cls.__name__} exposes an unexpected public method (possibly one that answers a "
        f"permission prompt instead of configuring paths in advance): "
        f"extra={sorted(public - EXPECTED_PUBLIC_METHODS)}, "
        f"missing={sorted(EXPECTED_PUBLIC_METHODS - public)}"
    )
