"""The inspect service: live view of the mounted plugin host.

Unlike the plugin manager, which looks at installable external plugins, the
inspect service reports the current process: registered services, loaded
plugin rows and their fiber states, contributed tools, prompt sections,
commands and llm providers. It never imports the kernel app; it only reads
through the cordis context it is mounted on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from deeppyro._vendor.cordis_port import FiberState
from deeppyro.api import PluginDependencyError, Service
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
            }
            self._apply_runtime_state(entry, runtime)
            rows.append(entry)
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
                }
            )
        return rows

    def config_schema(self, plugin_id: str) -> dict[str, Any]:
        """Config schema and masked current values for one plugin."""
        described = self._settings().describe(plugin_id)
        return {
            "plugin_id": described["plugin_id"],
            "target": described["target"],
            "schema": described["schema"],
            "revision": described["revision"],
            "apply": described["apply"],
        }


__all__ = ["InspectPlugin"]
