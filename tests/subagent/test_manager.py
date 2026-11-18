"""Tests for the native-only subagent service facade."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.api.types import CHILD_DISABLED_TOOLS
from deeppyro.subagent import (
    NativeSubagentService,
    SubagentError,
    SubagentService,
    filter_child_tools,
)
from tests.subagent.helpers import make_subagent


class FakeRegistry:
    """Registry double that exposes the without() refinement hook."""

    def __init__(self, names: list[str]) -> None:
        self.names = list(names)
        self.filtered: list[tuple[str, ...]] = []

    def without(self, names: Any) -> FakeRegistry:
        banned = tuple(names)
        self.filtered.append(banned)
        return FakeRegistry([name for name in self.names if name not in banned])


class FakeTool:
    """Minimal tool double carrying only the registry-visible name."""

    def __init__(self, name: str) -> None:
        self.name = name


def test_service_is_a_native_facade() -> None:
    service = SubagentService()

    assert isinstance(service, NativeSubagentService)
    assert service.native is service
    assert service.max_runs == 4


def test_service_accepts_the_plugin_sandbox_handle() -> None:
    service = SubagentService(agents=object(), sessions=object(), sandbox=object(), max_runs=2)

    assert service.max_runs == 2
    assert isinstance(service.native, NativeSubagentService)


def test_bind_session_accepts_a_native_parent(tmp_path) -> None:
    env = make_subagent(tmp_path)

    assert env.service.parent_session_id == env.parent.header.id
    assert env.service.native.parent_session is env.parent


def test_bind_session_rejects_a_retired_v4_session() -> None:
    service = SubagentService()
    retired = SimpleNamespace(header=SimpleNamespace(id="session-v4"))

    with pytest.raises(SubagentError, match="requires a native session"):
        service.bind_session(retired)


def test_bind_session_none_clears_the_parent(tmp_path) -> None:
    env = make_subagent(tmp_path)

    env.service.bind_session(None)

    assert env.service.parent_session_id == ""
    assert env.service.live_runs == 0


def test_filter_child_tools_uses_the_registry_refinement() -> None:
    registry = FakeRegistry(["read", "write", "subagent", "subagent_fork"])

    filtered = filter_child_tools(registry)

    assert isinstance(filtered, FakeRegistry)
    assert registry.filtered == [tuple(CHILD_DISABLED_TOOLS)]
    remaining = set(filtered.names)
    assert remaining <= {"read", "write"}
    assert remaining.isdisjoint(CHILD_DISABLED_TOOLS)


def test_filter_child_tools_falls_back_to_a_new_registry() -> None:
    tools = [FakeTool("read"), FakeTool("subagent"), FakeTool("write")]

    filtered = filter_child_tools(tools)

    assert filtered.names() == ["read", "write"]
