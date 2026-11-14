"""Tests for the builtin prompt-extension consumer (task-68)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.builtin.prompt_extension import PromptExtensionPlugin
from deeppyro.config import Config
from deeppyro.core.errors import ConfigError
from deeppyro.hooks import SYSTEM_PROMPT_ASSEMBLE, HookService


class FakeCtx:
    """Minimal PluginContext: service lookup and effect bookkeeping."""

    def __init__(self, services: dict[str, Any]) -> None:
        self._services = dict(services)
        self.effects: list[str] = []
        self.disposers: list[Any] = []

    def service(self, name: str) -> Any:
        if name not in self._services:
            raise LookupError(f"service {name!r} is not registered")
        return self._services[name]

    def effect(self, execute: Any, label: str = "anonymous") -> Any:
        dispose = execute()
        self.effects.append(label)
        self.disposers.append(dispose)
        return dispose


def make_config(prompt: Any = None) -> Config:
    data: dict[str, Any] = {}
    if prompt is not None:
        data["prompt"] = prompt
    return Config.model_validate(data)


def make_plugin(
    extension: Any = None, *, cfg: Config | None = None
) -> tuple[PromptExtensionPlugin, HookService, FakeCtx]:
    hooks = HookService()
    if cfg is None:
        table = None if extension is None else {"extension": extension}
        cfg = make_config(table)
    ctx = FakeCtx({"hooks": hooks, "config": SimpleNamespace(value=cfg)})
    plugin = PromptExtensionPlugin(ctx, None)
    plugin.setup(ctx, None)
    return plugin, hooks, ctx


async def assemble(
    hooks: HookService,
    rendered: Any = "base",
    *,
    fallback: Any = None,
) -> Any:
    """Run the waterfall with one downstream listener returning rendered."""
    calls: list[dict[str, Any]] = []

    def downstream(payload: dict[str, Any], next_step: Any) -> Any:
        calls.append(dict(payload))
        return rendered

    hooks.on(SYSTEM_PROMPT_ASSEMBLE, downstream)
    result = await hooks.waterfall(SYSTEM_PROMPT_ASSEMBLE, {"turn": 1}, fallback=fallback)
    assert calls, "the downstream listener must run before the extension is applied"
    return result


# -- configuration ---------------------------------------------------------
def test_plugin_metadata() -> None:
    assert PromptExtensionPlugin.plugin_id == "prompt-extension"
    assert PromptExtensionPlugin.inject == ["hooks", "config"]


def test_missing_prompt_table_means_no_extension() -> None:
    plugin, _hooks, _ctx = make_plugin(cfg=make_config())
    assert plugin.text == ""


def test_reads_the_extension_text() -> None:
    plugin, _hooks, _ctx = make_plugin("remember: be brief")
    assert plugin.text == "remember: be brief"


def test_invalid_extension_type_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="extension must be a string"):
        make_plugin(cfg=make_config({"extension": 5}))


def test_prompt_section_must_be_a_table() -> None:
    with pytest.raises(ConfigError, match="prompt.*must be a table"):
        make_plugin(cfg=make_config("not a table"))


# -- registration ---------------------------------------------------------
def test_setup_registers_the_waterfall_listener() -> None:
    _plugin, hooks, ctx = make_plugin("extra")
    assert hooks.has(SYSTEM_PROMPT_ASSEMBLE) is True
    assert ctx.effects == ["prompt-extension"]


def test_disposer_removes_the_listener() -> None:
    _plugin, hooks, ctx = make_plugin("extra")
    ctx.disposers[0]()
    assert hooks.has(SYSTEM_PROMPT_ASSEMBLE) is False


# -- waterfall behaviour --------------------------------------------------
async def test_empty_extension_returns_the_downstream_text() -> None:
    _plugin, hooks, _ctx = make_plugin(cfg=make_config())

    assert await assemble(hooks, "base") == "base"


async def test_empty_rendered_text_is_returned_unchanged() -> None:
    _plugin, hooks, _ctx = make_plugin(cfg=make_config())

    assert await hooks.waterfall(SYSTEM_PROMPT_ASSEMBLE, {"turn": 1}, fallback="") == ""


async def test_extension_is_appended_after_the_downstream_text() -> None:
    _plugin, hooks, _ctx = make_plugin("extra")

    assert await assemble(hooks, "base") == "base\n\nextra"


async def test_extension_text_is_stripped() -> None:
    _plugin, hooks, _ctx = make_plugin("  extra \n")

    assert await assemble(hooks, "base") == "base\n\nextra"


async def test_whitespace_only_extension_is_ignored() -> None:
    _plugin, hooks, _ctx = make_plugin("   \n\t")

    assert await assemble(hooks, "base") == "base"


async def test_non_string_downstream_result_is_returned_unchanged() -> None:
    _plugin, hooks, _ctx = make_plugin("extra")
    structured = {"sections": [1, 2]}

    assert await assemble(hooks, structured) is structured


async def test_missing_downstream_result_stays_none() -> None:
    _plugin, hooks, _ctx = make_plugin("extra")

    assert await hooks.waterfall(SYSTEM_PROMPT_ASSEMBLE, {"turn": 1}, fallback=None) is None


async def test_extension_appends_to_the_fallback_when_no_other_listener() -> None:
    _plugin, hooks, _ctx = make_plugin("extra")

    assert await hooks.waterfall(SYSTEM_PROMPT_ASSEMBLE, {"turn": 1}, fallback="base") == ("base\n\nextra")


async def test_payload_is_untouched_by_the_extension() -> None:
    _plugin, hooks, _ctx = make_plugin("extra")
    seen: list[dict[str, Any]] = []

    def downstream(payload: dict[str, Any], next_step: Any) -> str:
        seen.append(dict(payload))
        return "base"

    hooks.on(SYSTEM_PROMPT_ASSEMBLE, downstream)

    assert await hooks.waterfall(SYSTEM_PROMPT_ASSEMBLE, {"turn": 3, "step": 2}) == "base\n\nextra"
    assert seen == [{"turn": 3, "step": 2}]
