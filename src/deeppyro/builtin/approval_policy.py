"""Answer approval requests from configuration, before the interactive gate.

approval/request is a waterfall: the sandbox asks it first and only falls back
to the configured approval policy (ask/never/always) when the chain returns
"ask".  This consumer maps the [approval].hookMode setting onto that vocabulary:

    off   (default) - never answer, defer to the approval policy
    ask             - never answer, defer to the approval policy
    allow           - approve matching requests without prompting
    deny            - reject matching requests without prompting

hookTools narrows which tools the answer applies to; an empty list means every
request.  ApprovalRequest.tool is matched exactly.  The gate validates the
returned strings, so this plugin only ever returns "allow"/"deny" or the
downstream result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from deeppyro.api import Plugin
from deeppyro.core.errors import ConfigError
from deeppyro.hooks import APPROVAL_REQUEST
from deeppyro.hooks.types import HookNext, HookPayload

DEFAULT_HOOK_MODE = "off"
_HOOK_MODES = frozenset({"off", "ask", "allow", "deny"})


class ApprovalPolicyPlugin(Plugin):
    """Turn [approval].hookMode into approval/request answers."""

    plugin_id = "approval-policy"
    version = "0.1.0"
    description = "answer approval/request from [approval] hookMode"
    inject: ClassVar[list[str]] = ["hooks", "config"]

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        cfg = ctx.service("config").value
        table = _section(cfg, "approval")
        self._mode = _hook_mode(table)
        self._tools = _hook_tools(table)

    @property
    def mode(self) -> str:
        """The effective hook mode (off, ask, allow or deny)."""
        return self._mode

    @property
    def tools(self) -> tuple[str, ...]:
        """The tool names the policy applies to; empty means every tool."""
        return self._tools

    def setup(self, ctx: Any, config: Any) -> None:
        hooks = ctx.service("hooks")
        ctx.effect(
            lambda: hooks.on(APPROVAL_REQUEST, self._on_request),
            label="approval-policy",
        )

    async def _on_request(self, payload: HookPayload, next_step: HookNext) -> Any:
        """Answer when configured, otherwise continue the waterfall unchanged."""
        if self._mode in ("off", "ask"):
            return await next_step()
        if not self._matches(payload.get("request")):
            return await next_step()
        return self._mode

    def _matches(self, request: Any) -> bool:
        if not self._tools:
            return True
        tool = getattr(request, "tool", None)
        return isinstance(tool, str) and tool in self._tools


def _hook_mode(table: Mapping[str, Any] | None) -> str:
    if table is None:
        return DEFAULT_HOOK_MODE
    raw = _first(table, ("hookMode", "hook_mode"))
    if raw is None:
        return DEFAULT_HOOK_MODE
    if not isinstance(raw, str):
        raise ConfigError(f"[approval] hookMode must be a string, got {type(raw).__name__}")
    mode = raw.strip().lower()
    if mode not in _HOOK_MODES:
        expected = ", ".join(sorted(_HOOK_MODES))
        raise ConfigError(f"[approval] hookMode must be one of {expected}, got {raw!r}")
    return mode


def _hook_tools(table: Mapping[str, Any] | None) -> tuple[str, ...]:
    if table is None:
        return ()
    raw = _first(table, ("hookTools", "hook_tools"))
    if raw is None:
        return ()
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise ConfigError(f"[approval] hookTools must be a list of tool names, got {type(raw).__name__}")
    tools: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ConfigError(f"[approval] hookTools entries must be strings, got {type(item).__name__}")
        tools.append(item)
    return tuple(tools)


def _section(cfg: Any, name: str) -> Mapping[str, Any] | None:
    """Return one optional config table as a mapping."""
    if isinstance(cfg, Mapping):
        value = cfg.get(name)
    else:
        value = getattr(cfg, name, None)
    if value is None:
        return None
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        data = dump()
        if isinstance(data, Mapping):
            return data
    raise ConfigError(f"[{name}] must be a table, got {type(value).__name__}")


def _first(table: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in table:
            return table[name]
    return None


__all__ = ["DEFAULT_HOOK_MODE", "ApprovalPolicyPlugin"]
