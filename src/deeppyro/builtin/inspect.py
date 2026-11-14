"""The inspect service: live view of the mounted plugin host.

Unlike the plugin manager, which looks at installable external plugins, the
inspect service reports the current process: registered services, loaded
plugin rows and their fiber states, contributed tools, prompt sections,
commands and llm providers. It never imports the kernel app; it only reads
through the cordis context it is mounted on.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from deeppyro.api import FiberState, PluginDependencyError, Service
from deeppyro.core.wire import MESSAGE_PROJECTION_TYPES, RECORD_TYPES, SURFACE_EVENT_TYPES
from deeppyro.kernel.bundle import load_composition
from deeppyro.plugins.settings_store import SettingsStore


def _service_name(value: Any) -> str:
    cls = type(value)
    module = getattr(cls, "__module__", "")
    qualname = getattr(cls, "__qualname__", getattr(cls, "__name__", ""))
    if module and qualname:
        return f"{module}.{qualname}"
    return qualname or module or ""


def _fiber_state_name(fiber: Any) -> str:
    try:
        state = fiber.get_state()
    except Exception:  # noqa: BLE001 - inspection must never crash
        return "unknown"
    return getattr(state, "name", str(state)).lower()


def _mounted_item(item: Any) -> tuple[str, str, str]:
    if isinstance(item, str):
        return item.strip(), "", ""
    if isinstance(item, Mapping):
        data = dict(item)
        plugin_id = data.get("id") or data.get("plugin_id") or data.get("name") or ""
        state = data.get("state") or data.get("loader_state") or data.get("status") or ""
        error = data.get("error") or ""
        return str(plugin_id).strip(), str(state), str(error)
    if isinstance(item, (tuple, list)):
        plugin_id = str(item[0] or "").strip() if item else ""
        value = item[1] if len(item) > 1 else None
        error = str(item[2] or "") if len(item) > 2 else ""
        if isinstance(value, Mapping):
            data = dict(value)
            data.setdefault("id", plugin_id)
            return _mounted_item(data)
        if isinstance(value, str):
            return plugin_id, value, error
        if value is not None:
            state = getattr(value, "state", None) or getattr(value, "status", None) or ""
            nested_error = getattr(value, "error", "") or error
            return plugin_id, str(state), str(nested_error)
        return plugin_id, "", error
    plugin_id = getattr(item, "id", None) or getattr(item, "plugin_id", None) or getattr(item, "name", "")
    state = getattr(item, "state", None) or getattr(item, "status", None) or ""
    error = getattr(item, "error", "") or ""
    return str(plugin_id or "").strip(), str(state or ""), str(error or "")


def _event_row(record_type: str) -> dict[str, Any]:
    return {
        "type": record_type,
        "kind": record_type.partition("/")[0],
        "surface": record_type in SURFACE_EVENT_TYPES,
        "projection": record_type in MESSAGE_PROJECTION_TYPES,
    }


_GUIDE = "\n".join(  # noqa: FLY002 - readability of the multi-line guide
    (
        "# DeepPyro 插件开发指南",
        "",
        "## 1. 三类外部来源",
        "- bundle 目录：含 bundle.toml，可含 plugin.py；安装到 ~/.dpro/bundles/<id>/",
        "- 用户目录插件：%APPDATA%/deeppyro/plugins/<name>/plugin.toml",
        "- Python entry point：组 deeppyro.plugins，值为 模块:类",
        "",
        "## 2. 最小插件",
        "    from typing import Any, ClassVar",
        "    from pydantic import BaseModel",
        "    from deeppyro.api import Plugin",
        "    from deeppyro.core.protocols import ToolResult",
        "    from deeppyro.tools.base import BaseTool, make_parameters",
        "",
        "    class MyConfig(BaseModel):",
        '        greeting: str = "hello"',
        '        apiKey: str = ""',
        "",
        "    class MyTool(BaseTool):",
        '        name = "my_tool"',
        '        description = "Say hello."',
        "        parallel_safe = False",
        "        parameters = make_parameters({})",
        "",
        "        def __init__(self, greeting: str) -> None:",
        "            self.greeting = greeting",
        "",
        "        async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:",
        "            return ToolResult(text=self.greeting)",
        "",
        "    class MyPlugin(Plugin):",
        '        plugin_id = "my-plugin"',
        '        version = "0.1.0"',
        '        description = "my first plugin"',
        "        Config = MyConfig",
        '        volatile: ClassVar[tuple[str, ...]] = ("greeting",)',
        '        inject: ClassVar[list[str]] = ["tools"]',
        "",
        "        def setup(self, ctx: Any, config: Any) -> None:",
        '            greeting = config.greeting if config is not None else "hello"',
        "            ctx.register_tool(lambda _cfg, _workspace: [MyTool(greeting)])",
        "",
        "## 3. bundle.toml 与插件行",
        "    [bundle]",
        '    id = "my-bundle"',
        '    name = "My Bundle"',
        '    version = "0.1.0"',
        "    api_version = 1",
        "    order = 500",
        "    critical = false",
        "",
        "    [[plugins]]",
        '    id = "my-plugin"',
        '    plugin = "plugin.py:MyPlugin"',
        "同目录 plugin.py 用相对路径 plugin.py:类名；已安装模块用 module:Class。",
        "可选字段：config 内联表、disabled、before、after、critical。",
        "bundle 目录内多个 bundle 按 order 与 id 排序，同 id 插件行后者覆盖前者。",
        "",
        "## 4. 扩展点",
        "在 setup 中调用 ctx.register_tool、ctx.register_prompt_section、",
        "ctx.register_command、ctx.register_provider 或 ctx.register_preset。",
        '通过 ctx.get(服务名) 读取其它服务，例如 ctx.get("settings")。',
        "ClassVar inject 列出所需服务名，服务就绪后才调用 setup。",
        "",
        "## 5. 配置生效时机",
        "Config 为 pydantic 模型。settings_read 读取掩码值，settings_write 写入并做校验。",
        "volatile 中声明的字段在 kernel 服务提供 apply_plugin_config 时立即生效，",
        "返回 applied_live=true 且 restart_required=false。",
        "其它字段写入用户清单，仅在下次启动生效；代码改动总需要重启。",
        "",
        "## 6. agent 自扩展流程",
        "1. plugin_create(bundle_id=..., name=..., dest=...) 生成经过校验的 bundle 脚手架。",
        "2. plugin_manager(action=install, source=<目录>) 安装到 ~/.dpro/bundles。",
        "3. plugin_manager(action=set, id=..., enabled=true) 写入用户清单启用。",
        "4. 重启 TUI 让 kernel 挂载新 bundle；之后 settings_write 调整 volatile 字段。",
        "5. plugin_inspect 查看 services/plugins/tools/prompts/commands/providers/events/slots/theme。",
        "6. plugin_manager(action=remove, id=...) 卸载 bundle（不删除已安装的 pip 包）。",
        "",
        "## 7. 安全与限制",
        "install 与 create 会执行第三方代码，必须获得人类批准。",
        "非 volatile 配置需要重启；用户清单重写不保留注释与未知键。",
        "目录来源插件需要其目录在 sys.path 上才能导入。",
        "inspect 只读；settings 写入会校验 revision，过期则拒绝。",
    )
)


class InspectPlugin(Service):
    """Read-only inspection service for the running plugin host."""

    provide = "inspect"
    plugin_id = "inspect"
    version = "0.1.0"
    description = "live inspection of services, plugins, tools, prompts and commands"
    inject: ClassVar[list[str]] = []

    # -- helpers -----------------------------------------------------------

    def _require(self, name: str) -> Any:
        value = self.ctx.get(name)
        if value is None:
            raise PluginDependencyError(f"service {name!r} is not available")
        return value

    def _settings(self) -> SettingsStore:
        return SettingsStore()

    # -- services ----------------------------------------------------------

    def services(self) -> list[dict[str, Any]]:
        """Registered cordis services with activation state."""
        reflect = self._require("reflect")
        rows: list[dict[str, Any]] = []
        for impl in reflect.store.values():
            name = getattr(impl, "name", "")
            if not isinstance(name, str) or not name:
                continue
            state = "active"
            check = getattr(impl, "check", None)
            if check is not None:
                try:
                    available = bool(check())
                except Exception:  # noqa: BLE001 - a broken check is unavailable
                    available = False
                if not available:
                    state = "unavailable"
            if state == "active":
                ctx = getattr(impl, "ctx", None)
                fiber = getattr(ctx, "fiber", None) if ctx is not None else None
                if fiber is not None and getattr(fiber, "state", None) is not FiberState.ACTIVE:
                    state = "pending"
            value = getattr(impl, "value", None)
            rows.append(
                {
                    "name": name,
                    "state": state,
                    "provider": _service_name(value),
                }
            )
        rows.sort(key=lambda row: row["name"])
        return rows

    # -- plugins -----------------------------------------------------------

    def plugins(self) -> list[dict[str, Any]]:
        """Composition rows joined with the live runtime/fiber state."""
        try:
            composition = load_composition()
        except Exception as exc:
            raise PluginDependencyError(f"cannot load composition: {exc}") from exc
        runtimes = self._runtimes()
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for plugin_row in composition.rows():
            seen.add(plugin_row.id)
            entry = {
                "id": plugin_row.id,
                "plugin": plugin_row.plugin,
                "state": "disabled" if plugin_row.disabled is True else "configured",
                "critical": plugin_row.critical is True,
                "origin": plugin_row.origin,
                "error": "",
                "loader_state": "",
            }
            if plugin_row.disabled is True:
                rows.append(entry)
                continue
            runtime = runtimes.get(plugin_row.id)
            if runtime is None:
                entry["state"] = "unmounted"
                rows.append(entry)
                continue
            self._apply_runtime_state(entry, runtime)
            rows.append(entry)
        for name, runtime in sorted(runtimes.items()):
            if name in seen:
                continue
            entry = {
                "id": name,
                "plugin": "",
                "state": "configured",
                "critical": False,
                "origin": "",
                "error": "",
                "loader_state": "",
            }
            self._apply_runtime_state(entry, runtime)
            rows.append(entry)
        mounted = self._mounted_plugins()
        if mounted:
            by_id = {row["id"]: row for row in rows}
            for plugin_id, mount in sorted(mounted.items()):
                entry = by_id.get(plugin_id)
                if entry is None:
                    entry = {
                        "id": plugin_id,
                        "plugin": "",
                        "state": "configured",
                        "critical": False,
                        "origin": "",
                        "error": "",
                        "loader_state": "",
                    }
                    rows.append(entry)
                entry["loader_state"] = mount["state"]
                if mount["error"] and not entry["error"]:
                    entry["error"] = mount["error"]
            rows.sort(key=lambda row: row["id"])
        return rows

    def _runtimes(self) -> dict[str, Any]:
        registry = self.ctx.get("registry")
        if registry is None:
            return {}
        runtimes: dict[str, Any] = {}
        for runtime in registry.values():
            name = getattr(runtime, "name", "")
            if isinstance(name, str) and name:
                runtimes[name] = runtime
        return runtimes

    def _apply_runtime_state(self, entry: dict[str, Any], runtime: Any) -> None:
        fibers = list(getattr(runtime, "fibers", ()) or ())
        states = [_fiber_state_name(fiber) for fiber in fibers]
        errors = []
        for fiber in fibers:
            error = getattr(fiber, "_error", None)
            if error:
                errors.append(str(error))
        if errors:
            entry["error"] = "; ".join(errors)
        if not states:
            entry["state"] = "unmounted"
        elif "failed" in states:
            entry["state"] = "failed"
        elif "active" in states:
            entry["state"] = "active"
        elif "loading" in states:
            entry["state"] = "loading"
        elif "unloading" in states:
            entry["state"] = "unloading"
        else:
            entry["state"] = states[0]

    def _mounted_plugins(self) -> dict[str, dict[str, str]]:
        kernel = self.ctx.get("kernel")
        mounted = getattr(kernel, "mounted_plugins", None) if kernel is not None else None
        if not callable(mounted):
            return {}
        try:
            payload = mounted()
        except Exception:  # noqa: BLE001 - optional kernel capability, inspection never crashes
            return {}
        if isinstance(payload, Mapping):
            items: Any = payload.items()
        elif isinstance(payload, (list, tuple)):
            items = payload
        else:
            items = ()
        result: dict[str, dict[str, str]] = {}
        for item in items:
            plugin_id, state, error = _mounted_item(item)
            if plugin_id and plugin_id not in result:
                result[plugin_id] = {"state": state, "error": error}
        return result

    # -- contributions -----------------------------------------------------

    def tools(self, workspace: str | Path | None = None) -> list[dict[str, Any]]:
        """Every tool contributed to the tools service."""
        tools_service = self._require("tools")
        config_service = self.ctx.get("config")
        config = getattr(config_service, "value", None)
        root = Path(workspace) if workspace is not None else Path(".")
        rows: list[dict[str, Any]] = []
        for contribution in tools_service.catalog():
            plugin_id = getattr(contribution, "plugin_id", "")
            scope = getattr(contribution, "scope", None)
            try:
                produced = contribution.factory(config, root)
            except Exception as exc:  # noqa: BLE001 - report, never crash inspection
                rows.append(
                    {
                        "name": "",
                        "plugin_id": plugin_id,
                        "scope": scope,
                        "error": str(exc),
                    }
                )
                continue
            if produced is None:
                continue
            items = list(produced) if isinstance(produced, (list, tuple)) else [produced]
            for tool in items:
                name = getattr(tool, "name", "")
                if not name:
                    continue
                rows.append(
                    {
                        "name": name,
                        "plugin_id": plugin_id,
                        "scope": scope,
                        "parallel_safe": bool(getattr(tool, "parallel_safe", True)),
                    }
                )
        rows.sort(key=lambda row: (row["name"], row["plugin_id"]))
        return rows

    def prompt_sections(self) -> list[dict[str, Any]]:
        """Registered system prompt sections in rendering order."""
        prompt = self._require("prompt")
        return [
            {
                "name": section.name,
                "order": section.order,
                "plugin_id": section.plugin_id,
                "scope": section.scope,
            }
            for section in prompt.sections()
        ]

    def commands(self) -> list[dict[str, Any]]:
        """Registered slash commands."""
        commands = self._require("commands")
        return [
            {
                "name": spec.name,
                "plugin_id": spec.plugin_id,
                "summary": spec.summary,
                "aliases": list(spec.aliases),
            }
            for spec in commands.all()
        ]

    def providers(self) -> list[dict[str, Any]]:
        """Llm providers: configured models plus dynamically registered ones."""
        llm = self._require("llm")
        descriptions = {}
        if hasattr(llm, "descriptions"):
            descriptions = llm.descriptions()
        names = set(llm.provider_names())
        config_service = self.ctx.get("config")
        config = getattr(config_service, "value", None)
        configured = getattr(config, "providers", None)
        if isinstance(configured, dict):
            names.update(str(name) for name in configured)
        rows = []
        for name in sorted(names):
            rows.append(
                {
                    "name": name,
                    "description": descriptions.get(name, ""),
                    "models": _catalog_rows(llm, name),
                }
            )
        return rows

    def events(self) -> list[dict[str, Any]]:
        """Wire record types and the surface/projection subsets."""
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record_type in RECORD_TYPES:
            if record_type in seen:
                continue
            seen.add(record_type)
            rows.append(_event_row(record_type))
        for record_type in (*SURFACE_EVENT_TYPES, *MESSAGE_PROJECTION_TYPES):
            if record_type in seen:
                continue
            seen.add(record_type)
            rows.append(_event_row(record_type))
        return rows

    def slots(self) -> list[dict[str, Any]]:
        """Contributions registered on the optional slots service."""
        slots = self.ctx.get("slots")
        if slots is None or not hasattr(slots, "list"):
            return []
        try:
            contributions = slots.list()
        except Exception:  # noqa: BLE001 - optional service, inspection never crashes
            return []
        rows = []
        for contribution in contributions:
            rows.append(
                {
                    "kind": str(getattr(contribution, "kind", "")),
                    "id": str(getattr(contribution, "id", "")),
                    "plugin_id": str(getattr(contribution, "plugin_id", "")),
                    "order": int(getattr(contribution, "order", 0) or 0),
                    "title": str(getattr(contribution, "title", "") or ""),
                }
            )
        rows.sort(key=lambda row: (row["kind"], row["order"], row["id"]))
        return rows

    def theme(self) -> dict[str, Any]:
        """Theme tokens exposed by the optional slots service."""
        slots = self.ctx.get("slots")
        provider = getattr(slots, "theme", None) if slots is not None else None
        if not callable(provider):
            return {"tokens": {}, "note": "slots service does not expose theme tokens"}
        try:
            tokens = provider()
        except Exception as exc:  # noqa: BLE001 - optional service, inspection never crashes
            return {"tokens": {}, "note": f"theme unavailable: {exc}"}
        if not isinstance(tokens, Mapping):
            return {"tokens": {}, "note": "theme provider returned no token mapping"}
        return {"tokens": {str(key): value for key, value in tokens.items()}, "note": ""}

    def guide(self) -> str:
        """Markdown guide for extending DeepPyro with plugins and bundles."""
        return _GUIDE

    def config_schema(self, plugin_id: str) -> dict[str, Any]:
        """Config schema, masked values and volatile fields for one plugin."""
        described = self._settings().describe(plugin_id)
        return {
            "plugin_id": described["plugin_id"],
            "target": described["target"],
            "schema": described["schema"],
            "revision": described["revision"],
            "apply": described["apply"],
            "volatile_fields": list(described.get("volatile_fields", ())),
        }


def _catalog_rows(llm: Any, provider: str) -> list[dict[str, Any]]:
    """Model catalog snapshot for one provider (discovery is best-effort metadata)."""
    snapshot = getattr(llm, "catalog_snapshot", None)
    if not callable(snapshot):
        return []
    try:
        return [info.to_dict() for info in snapshot(provider)]
    except Exception:  # noqa: BLE001 - optional metadata, inspection never crashes
        return []


__all__ = ["InspectPlugin"]
