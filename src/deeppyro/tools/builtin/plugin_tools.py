"""Agent-facing plugin management tools.

These tools let a running agent install, inspect and configure plugins. They
resolve their services from ToolContext.services instead of importing the
kernel app, so the runtime decides what capabilities each session exposes.

Installing a bundle executes third-party plugin code on the next boot; the
tool descriptions say so explicitly because the call needs human approval.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, ClassVar

from deeppyro.api import Plugin
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.base import BaseTool, make_parameters


def _as_json(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _as_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_as_json(item) for item in value]
    return value


def _dump(value: Any) -> str:
    return json.dumps(_as_json(value), ensure_ascii=False, indent=2, default=str)


def _missing(service: str) -> ToolResult:
    return ToolResult(
        text=f"{service} service is not available in this session; plugin tools are disabled",
        is_error=True,
    )


class PluginManagerTool(BaseTool):
    """Install, list, enable, disable and remove external plugin bundles."""

    name = "plugin_manager"
    description = (
        "Manage external DeepPyro plugin bundles and installed plugins. "
        "action=list shows installed bundles and external plugins; action=install copies a "
        "bundle directory into the user bundle store and executes third-party plugin code on "
        "the next boot, so it requires explicit human approval; action=set enables or disables "
        "a bundle; action=remove deletes a user bundle. Changes take effect after a restart."
    )
    parallel_safe = False
    parameters = make_parameters(
        {
            "action": {
                "type": "string",
                "enum": ["list", "install", "set", "remove"],
                "description": "operation to perform",
            },
            "source": {
                "type": "string",
                "description": "bundle directory or bundle.toml path (install only)",
            },
            "id": {
                "type": "string",
                "description": "bundle id (set and remove only)",
            },
            "enabled": {
                "type": "boolean",
                "description": "desired bundle state (set only)",
            },
            "force": {
                "type": "boolean",
                "description": "replace an already installed bundle (install only)",
            },
        },
        required=["action"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = (ctx.services or {}).get("plugin_manager")
        if service is None:
            return _missing("plugin_manager")
        action = str(args.get("action") or "").strip()
        try:
            if action == "list":
                payload = {
                    "bundles": service.list_bundles(),
                    "plugins": service.list(),
                }
            elif action == "install":
                source = args.get("source")
                if not isinstance(source, str) or not source.strip():
                    return ToolResult(text="install requires a non-empty source", is_error=True)
                payload = service.install_bundle(source, force=bool(args.get("force", False)))
            elif action == "set":
                bundle_id = args.get("id")
                if not isinstance(bundle_id, str) or not bundle_id.strip():
                    return ToolResult(text="set requires a non-empty id", is_error=True)
                enabled = args.get("enabled")
                if not isinstance(enabled, bool):
                    return ToolResult(text="set requires a boolean enabled flag", is_error=True)
                payload = service.set_bundle(bundle_id, enabled)
            elif action == "remove":
                bundle_id = args.get("id")
                if not isinstance(bundle_id, str) or not bundle_id.strip():
                    return ToolResult(text="remove requires a non-empty id", is_error=True)
                payload = service.remove_bundle(bundle_id)
            else:
                return ToolResult(
                    text="unknown plugin_manager action; expected list, install, set or remove",
                    is_error=True,
                )
        except Exception as exc:  # noqa: BLE001 - surface as a tool error result
            return ToolResult(text=f"plugin_manager failed: {exc}", is_error=True)
        return ToolResult(text=_dump(payload))


class PluginCreateTool(BaseTool):
    """Scaffold and validate a new plugin bundle without installing it."""

    name = "plugin_create"
    description = (
        "Create a new DeepPyro plugin bundle scaffold. Writes bundle.toml, plugin.py and "
        "README.md to dest (defaults to the user bundle store under ~/.dpro/bundles/<bundle_id>), "
        "validates the result and returns the created files. The scaffold is not installed or "
        "enabled yet: call plugin_manager action=install with source=<dest>, then action=set "
        "with enabled=true and restart the app. Creating a bundle writes executable third-party "
        "code, so it requires explicit human approval."
    )
    parallel_safe = False
    parameters = make_parameters(
        {
            "bundle_id": {
                "type": "string",
                "description": "unique bundle id, also the bundle directory name",
            },
            "name": {
                "type": "string",
                "description": "display name (defaults to bundle_id)",
            },
            "dest": {
                "type": "string",
                "description": "destination directory (defaults to the user bundle store)",
            },
        },
        required=["bundle_id"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = (ctx.services or {}).get("plugin_manager")
        if service is None:
            return _missing("plugin_manager")
        bundle_id = args.get("bundle_id")
        if not isinstance(bundle_id, str) or not bundle_id.strip():
            return ToolResult(text="plugin_create requires bundle_id", is_error=True)
        bundle_id = bundle_id.strip()
        name = args.get("name")
        display_name = name if isinstance(name, str) else ""
        dest = args.get("dest")
        dest_text = dest.strip() if isinstance(dest, str) else ""
        try:
            if dest_text:
                path = Path(dest_text).expanduser()
            else:
                root = getattr(service, "bundles_root", None)
                if root is None:
                    return ToolResult(
                        text="plugin_manager has no bundles_root; pass dest explicitly",
                        is_error=True,
                    )
                path = Path(root) / bundle_id
            created = Path(service.create_bundle(path, bundle_id, display_name))
            files = sorted(item.name for item in created.iterdir() if item.is_file())
        except Exception as exc:  # noqa: BLE001 - surface as a tool error result
            return ToolResult(text="plugin_create failed: " + str(exc), is_error=True)
        return ToolResult(
            text=_dump(
                {
                    "bundle_id": bundle_id,
                    "dest": str(created),
                    "files": files,
                    "next": "plugin_manager action=install source=" + str(created),
                }
            )
        )


class PluginInspectTool(BaseTool):
    """Inspect services, plugins, tools, prompt sections, commands and providers."""

    name = "plugin_inspect"
    description = (
        "Inspect the running plugin host: what=services lists registered services, "
        "what=plugins lists plugin rows and their live state, what=tools lists contributed "
        "tools, what=prompts lists system prompt sections, what=commands lists slash commands, "
        "what=providers lists llm providers, what=events lists wire record types, what=slots "
        "lists UI slot contributions, what=theme lists theme tokens and what=guide returns the "
        "plugin authoring guide."
    )
    parallel_safe = False
    parameters = make_parameters(
        {
            "what": {
                "type": "string",
                "enum": [
                    "services",
                    "plugins",
                    "tools",
                    "prompts",
                    "commands",
                    "providers",
                    "events",
                    "slots",
                    "theme",
                    "guide",
                ],
                "description": "inspection target",
            },
            "workspace": {
                "type": "string",
                "description": "workspace path used to instantiate tools (tools only)",
            },
        },
        required=["what"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = (ctx.services or {}).get("inspect")
        if service is None:
            return _missing("inspect")
        what = str(args.get("what") or "").strip()
        workspace = args.get("workspace")
        workspace_path = workspace if isinstance(workspace, str) else None
        actions: dict[str, Any] = {
            "services": service.services,
            "plugins": service.plugins,
            "tools": lambda: service.tools(workspace_path),
            "prompts": service.prompt_sections,
            "commands": service.commands,
            "providers": service.providers,
            "events": service.events,
            "slots": service.slots,
            "theme": service.theme,
            "guide": service.guide,
        }
        handler = actions.get(what)
        if handler is None:
            return ToolResult(
                text="unknown plugin_inspect target; expected services, plugins, tools, "
                "prompts, commands, providers, events, slots, theme or guide",
                is_error=True,
            )
        try:
            payload = handler()
        except Exception as exc:  # noqa: BLE001 - surface as a tool error result
            return ToolResult(text=f"plugin_inspect failed: {exc}", is_error=True)
        return ToolResult(text=_dump(payload))


class SettingsReadTool(BaseTool):
    """Read one plugin's configuration schema and masked current values."""

    name = "settings_read"
    description = (
        "Read a plugin's configuration schema together with current values; secret fields "
        "such as api keys or tokens are masked. Use the returned revision as expected_revision "
        "when calling settings_write."
    )
    parallel_safe = False
    parameters = make_parameters(
        {
            "plugin_id": {
                "type": "string",
                "description": "plugin row id from plugin_inspect plugins",
            },
        },
        required=["plugin_id"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = (ctx.services or {}).get("settings")
        if service is None:
            return _missing("settings")
        plugin_id = args.get("plugin_id")
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            return ToolResult(text="settings_read requires plugin_id", is_error=True)
        try:
            payload = service.read(plugin_id)
        except Exception as exc:  # noqa: BLE001 - surface as a tool error result
            return ToolResult(text=f"settings_read failed: {exc}", is_error=True)
        return ToolResult(text=_dump(payload))


class SettingsWriteTool(BaseTool):
    """Patch one plugin's configuration with optimistic concurrency control."""

    name = "settings_write"
    description = (
        "Patch a plugin's configuration. The patch is merged into the current user manifest "
        "entry, validated against the plugin Config model and persisted atomically. Fields "
        "declared volatile on the plugin class are applied live when the kernel service allows "
        "it (applied_live=true, restart_required=false); other changes take effect on the next "
        "boot. expected_revision must match the revision from settings_read or the write fails "
        "with a conflict. Omitted secret fields keep their stored value."
    )
    parallel_safe = False
    parameters = make_parameters(
        {
            "plugin_id": {
                "type": "string",
                "description": "plugin row id to configure",
            },
            "patch": {
                "type": "object",
                "description": "partial configuration object to merge",
            },
            "expected_revision": {
                "type": "string",
                "description": "revision returned by settings_read",
            },
        },
        required=["plugin_id", "patch", "expected_revision"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = (ctx.services or {}).get("settings")
        if service is None:
            return _missing("settings")
        plugin_id = args.get("plugin_id")
        patch = args.get("patch")
        revision = args.get("expected_revision")
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            return ToolResult(text="settings_write requires plugin_id", is_error=True)
        if not isinstance(patch, dict):
            return ToolResult(text="settings_write requires an object patch", is_error=True)
        if not isinstance(revision, str):
            return ToolResult(text="settings_write requires expected_revision", is_error=True)
        try:
            payload = service.write(plugin_id, patch, revision)
        except Exception as exc:  # noqa: BLE001 - surface as a tool error result
            return ToolResult(text=f"settings_write failed: {exc}", is_error=True)
        return ToolResult(text=_dump(payload))


def plugin_tools() -> list[BaseTool]:
    """The plugin management tool set, for registration by the tools plugin."""
    return [
        PluginManagerTool(),
        PluginCreateTool(),
        PluginInspectTool(),
        SettingsReadTool(),
        SettingsWriteTool(),
    ]


class PluginToolsPlugin(Plugin):
    """Registers the plugin management tools when mounted."""

    plugin_id = "plugin-tools"
    version = "0.1.0"
    description = "agent-facing plugin manager, inspect and settings tools"
    inject: ClassVar[list[str]] = ["tools"]

    def setup(self, ctx: Any, config: Any) -> None:
        ctx.register_tool(lambda _cfg, _workspace: plugin_tools())


__all__ = [
    "PluginCreateTool",
    "PluginInspectTool",
    "PluginManagerTool",
    "PluginToolsPlugin",
    "SettingsReadTool",
    "SettingsWriteTool",
    "plugin_tools",
]
