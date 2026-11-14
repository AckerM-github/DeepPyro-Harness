"""Built-in agent presets: PTC, Minimal and Cordis.

These presets are registered from one plugin so a host can add or remove the
DSH-style preset set as one composition row.  The PTC preset reuses the
run_code tool and prompt sections contributed by the ptc plugin through its
preset scope; this plugin only declares the preset rows and the two prompt
sections that belong to a single preset.
"""

from __future__ import annotations

import os
from typing import Any, ClassVar

from deeppyro.api import (
    AGENT_MODE_DEFAULT,
    AGENT_PRESENTATION_NATIVE,
    AGENT_PRESENTATION_PTC,
    CORDIS_PRESET_ID,
    MINIMAL_PRESET_ID,
    PLUGIN_AUTHORING_TOOL_NAMES,
    PTC_PRESET_ID,
    Plugin,
    PresetSpec,
)

MINIMAL_PERSONA_SECTION = "minimal-persona"
MINIMAL_PERSONA_ORDER = 5
CORDIS_GUIDE_SECTION = "cordis-guide"
CORDIS_GUIDE_ORDER = 60

MINIMAL_PERSONA = "You are a helpful software engineer assistant."

CORDIS_GUIDE = """## Extending DeepPyro with Cordis plugins

- A bundle is a directory with bundle.toml: one [bundle] table (id, name, version, order, critical) and one [[plugins]] table per row (id, plugin = "module:Class", config, disabled, before, after, critical, name, isolate, intercept, group, rows, include).
- Rows load in composition order: the core bundle first, then bundles under ~/.dpro/bundles sorted by order and id, then explicit bundle paths, then user manifest patches from ~/.dpro/plugins.toml.
- Rows with the same id are patched: config is replaced as a whole, disabled and critical are overridden, isolate and intercept merge, and the plugin class cannot change.
- A new row is inserted before or after its anchor id, otherwise appended at the end.
- A plugin subclasses deeppyro.api.Plugin or deeppyro.api.Service, sets plugin_id, declares inject names, and implements setup(ctx, config). A Service subclass declares provide and is reachable with ctx.service(name).
- Register contributions with ctx.register_tool, ctx.register_prompt_section, ctx.register_command, ctx.register_provider and ctx.register_preset. Their disposers are scoped to the plugin fiber, so cleanup is automatic.
- Prefer editing or adding bundle rows over hand wiring: the loader validates rows and reports load state per row id.
- Self-extension tools: plugin_inspect reports services, plugins, tools, prompts, commands and providers; plugin_manager lists, installs, sets and removes bundles; plugin_create scaffolds a new bundle; settings_read and settings_write inspect and patch plugin config with a revision check.
- Installing or creating a plugin executes third-party code on the next boot, so it needs explicit human approval.
"""


def builtin_presets() -> tuple[PresetSpec, ...]:
    """Return the PTC, Minimal and Cordis preset rows."""
    shell = "pwsh" if os.name == "nt" else "bash"
    return (
        PresetSpec(
            id=PTC_PRESET_ID,
            name="PTC",
            description="standard tools through one run_code program (programmatic tool calling)",
            order=10,
            mode=AGENT_MODE_DEFAULT,
            presentation=AGENT_PRESENTATION_PTC,
            tools_exclude=PLUGIN_AUTHORING_TOOL_NAMES,
        ),
        PresetSpec(
            id=MINIMAL_PRESET_ID,
            name="Minimal",
            description="a single platform shell and a minimal prompt",
            order=20,
            mode=AGENT_MODE_DEFAULT,
            presentation=AGENT_PRESENTATION_NATIVE,
            tools=(shell,),
            tools_exclude=(),
            prompt_sections=(MINIMAL_PERSONA_SECTION, "tools", "rules"),
            runtime_context=False,
        ),
        PresetSpec(
            id=CORDIS_PRESET_ID,
            name="Cordis",
            description="standard tools plus plugin authoring and cordis guidance",
            order=30,
            mode=AGENT_MODE_DEFAULT,
            presentation=AGENT_PRESENTATION_NATIVE,
            tools_exclude=(),
            runtime_context=True,
        ),
    )


def _render_minimal_persona(tools: Any, prompt_context: Any) -> str:
    return MINIMAL_PERSONA


def _render_cordis_guide(tools: Any, prompt_context: Any) -> str:
    return CORDIS_GUIDE


class AgentPresetsPlugin(Plugin):
    """Registers the built-in PTC, Minimal and Cordis agent presets."""

    plugin_id = "agent-presets"
    version = "0.1.0"
    description = "built-in agent presets: PTC, Minimal and Cordis"
    inject: ClassVar[list[str]] = ["presets", "prompt"]

    def setup(self, ctx: Any, config: Any) -> None:
        for preset in builtin_presets():
            ctx.register_preset(preset)
        ctx.register_prompt_section(
            MINIMAL_PERSONA_SECTION,
            MINIMAL_PERSONA_ORDER,
            _render_minimal_persona,
            scope=MINIMAL_PRESET_ID,
        )
        ctx.register_prompt_section(
            CORDIS_GUIDE_SECTION,
            CORDIS_GUIDE_ORDER,
            _render_cordis_guide,
            scope=CORDIS_PRESET_ID,
        )


__all__ = [
    "CORDIS_GUIDE",
    "CORDIS_GUIDE_ORDER",
    "CORDIS_GUIDE_SECTION",
    "MINIMAL_PERSONA",
    "MINIMAL_PERSONA_ORDER",
    "MINIMAL_PERSONA_SECTION",
    "AgentPresetsPlugin",
    "builtin_presets",
]
