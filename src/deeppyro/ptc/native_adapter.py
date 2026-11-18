"""Native host adapter for PTC presets.

The native agent factory refuses the PTC presentation because the v4 agent
plugin used to own the run_code surface. On the native runtime the same rule
can be expressed as a host post-step: build the host with a presentation-masked
preset spec (so the factory does not refuse), then narrow the live loop's
surface registry to run_code while keeping the full registry available for the
nested dispatcher and for the tools:sdk prompt section.

This module is the smallest adapter that expresses that post-step. It is used
by the TUI session layer (and nothing else) so the v4 path stays untouched;
it never falls back to the v4 agent when a PTC preset is selected.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Sequence
from typing import Any, cast

from deeppyro.agent.native_factory import AgentBuild, _filter_registry, build_native_host
from deeppyro.api.errors import PluginDependencyError
from deeppyro.api.types import AGENT_MODE_DEFAULT, AGENT_PRESENTATION_NATIVE, AGENT_PRESENTATION_PTC
from deeppyro.core.llm import ToolSpec
from deeppyro.ptc.tool import RUN_CODE_NAME
from deeppyro.session.schema import NativeSession, SessionHeader

__all__ = ["build_ptc_host"]


class _MaskedPresets:
    """Preset resolver that reports the PTC preset as a native presentation.

    build_native_host refuses a PTC spec before it builds the host, so the
    adapter resolves the real spec once (raising the same errors the factory
    would) and hands the factory a frozen copy with the presentation masked.
    Every other method is delegated to the real resolver.
    """

    __slots__ = ("_masked", "_presets")

    def __init__(self, presets: Any, spec: Any) -> None:
        self._presets = presets
        self._masked = dataclasses.replace(spec, presentation=AGENT_PRESENTATION_NATIVE)

    def resolve(self, preset_id: str | None = None, *, revision: int | None = None) -> Any:
        return self._masked

    def is_materialized(self, preset_id: str, revision: int | None = None) -> bool:
        return bool(self._presets.is_materialized(preset_id, revision))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._presets, name)


class _SurfacePromptSource:
    """Prompt source that keeps full tool schemas but narrows direct tools.

    The PTC prompt needs every tool schema (the tools:sdk section renders the
    SDK from them) while direct_tool_names must list only run_code, the single
    tool the model may call directly.
    """

    __slots__ = ("_base", "_direct_names")

    def __init__(self, base: Any, direct_names: Sequence[str]) -> None:
        self._base = base
        self._direct_names = tuple(direct_names)

    def context(self, session: SessionHeader, tools: Sequence[ToolSpec] = ()) -> Any:
        context = self._base.context(session, tools)
        return dataclasses.replace(context, direct_tool_names=self._direct_names)

    def render(self, session: SessionHeader, tools: Sequence[ToolSpec]) -> str:
        spec = getattr(self._base, "spec", None)
        return self._base.prompt.build(
            list(tools),
            self.context(session, tools),
            scope=getattr(spec, "id", None),
            revision=getattr(spec, "revision", None),
            include=getattr(spec, "prompt_sections", None),
        )


def _full_registry(build: AgentBuild, spec: Any, tools: Any) -> Any:
    """The preset's full registry, filtered exactly like the factory does."""
    if tools is None:
        return ()
    registry = tools.registry_for(str(build.cwd), scope=getattr(spec, "id", None), revision=spec.revision)
    return _filter_registry(registry, spec)


def _ptc_surface(registry: Any) -> Any:
    """The run_code-only surface, or None when run_code is not registered."""
    names = getattr(registry, "names", None)
    without = getattr(registry, "without", None)
    if not callable(names) or not callable(without):
        return None
    registered = [str(name) for name in cast(Iterable[Any], names())]
    if RUN_CODE_NAME not in registered:
        return None
    return without([name for name in registered if name != RUN_CODE_NAME])


def build_ptc_host(
    session: NativeSession,
    build: AgentBuild,
    *,
    prompt: Any,
    presets: Any,
    llm: Any,
    tools: Any = None,
    ptc: Any = None,
    default_model: str = "",
    default_max_steps: int | None = None,
) -> Any:
    """Build a NativeAgentHost for a PTC preset without the v4 agent path.

    Raises PluginDependencyError when the preset cannot be resolved, when the
    ptc runtime is absent from this bundle, or when run_code is not registered
    for the preset. The returned host has presentation 'ptc', a run_code-only
    loop surface, and a prompt rendered against the full registry.
    """
    if presets is None:
        raise PluginDependencyError("agent presets are not available in this bundle")
    spec = presets.resolve(build.preset, revision=build.preset_revision)
    if ptc is None:
        raise PluginDependencyError(
            f"preset {getattr(spec, 'id', '')!r} needs the ptc runtime, which this bundle does not provide"
        )
    # The host is annotated Any because the adapter decorates it with the
    # presentation marker below; NativeAgentHost has no declared attribute.
    host: Any = build_native_host(
        session,
        build,
        prompt=prompt,
        presets=_MaskedPresets(presets, spec),
        llm=llm,
        tools=tools,
        default_model=default_model,
        default_max_steps=default_max_steps,
        ptc_mode=None,
    )
    host.presentation = AGENT_PRESENTATION_NATIVE
    full = _full_registry(build, spec, tools)
    if str(getattr(spec, "mode", "") or "") != AGENT_MODE_DEFAULT:
        # v4 keeps the native surface for non-default (team) presets; only the
        # presentation flag is meaningful here, and it stays native.
        return host
    surface = _ptc_surface(full)
    if surface is None:
        raise PluginDependencyError(
            f"preset {getattr(spec, 'id', '')!r} selects the ptc presentation, "
            "but run_code is not registered in its tool registry"
        )
    loop = getattr(host, "_loop", None)
    if loop is not None:
        loop._registry = surface
        loop._surface = surface
        loop.surface_registry = surface
        services = getattr(loop, "_services", None)
        if isinstance(services, dict):
            services["tool_registry"] = full
    host._tools = tuple(full)
    source = getattr(host, "_prompt_source", None)
    if source is not None:
        facts = getattr(host, "_prompt_facts", None)
        host.attach_prompt(_SurfacePromptSource(source, tuple(tool.name for tool in surface)), facts)
    host.presentation = AGENT_PRESENTATION_PTC
    return host
