"""Shared helpers for the external plugin tests.

Everything here builds throwaway trees under pytest tmp_path and fake
importlib metadata objects, so tests never read the real user config directory
or depend on installed distributions.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from deeppyro.plugins.model import (
    SOURCE_DIR,
    PluginInfo,
    ValidationResult,
)


def toml_string(value: str) -> str:
    """Serialize one TOML basic string (JSON escapes are a TOML subset)."""
    return json.dumps(value, ensure_ascii=False)


def write_plugin(
    directory: Path,
    *,
    name: str = "Demo Plugin",
    module: str = "demo_plugin",
    class_name: str = "DemoPlugin",
    version: str = "0.1.0",
    description: str = "a demo plugin",
    provides: Sequence[str] | None = ("demo",),
    class_provide: str | list[str] | None = "demo",
    module_source: str | None = None,
) -> Path:
    """Create directory/plugin.toml plus the plugin module it points at."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        f"name = {toml_string(name)}",
        f"version = {toml_string(version)}",
        f"description = {toml_string(description)}",
        f"module = {toml_string(module)}",
        f"class = {toml_string(class_name)}",
    ]
    if provides is not None:
        lines.append("provides = " + json.dumps(list(provides), ensure_ascii=False))
    (directory / "plugin.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if module_source is None:
        source = f"class {class_name}:\n"
        if class_provide is not None:
            source += f"    provide = {class_provide!r}\n"
        source += "\n"
    else:
        source = module_source
    (directory / f"{module}.py").write_text(source, encoding="utf-8")
    return directory


def write_manifest_text(path: Path, text: str) -> Path:
    """Write raw manifest TOML (used to exercise loader errors)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_info(
    plugin_id: str = "demo",
    *,
    name: str | None = None,
    version: str = "0.1.0",
    source: str = SOURCE_DIR,
    origin: str = "C:/plugins/demo/plugin.toml",
    description: str = "a demo plugin",
    enabled: bool = False,
    error: str | None = None,
    provides: tuple[str, ...] = (),
    target: str | None = "demo_plugin:DemoPlugin",
) -> PluginInfo:
    """Build one PluginInfo row with convenient defaults."""
    return PluginInfo(
        id=plugin_id,
        name=name or plugin_id,
        version=version,
        source=source,  # type: ignore[arg-type]
        origin=origin,
        description=description,
        enabled=enabled,
        error=error,
        provides=provides,
        target=target,
    )


class FakeDist:
    """Minimal stand-in for importlib.metadata.Distribution."""

    def __init__(
        self,
        name: str = "fake-dist",
        version: str = "1.0.0",
        summary: str = "a fake plugin distribution",
    ) -> None:
        self.name = name
        self.version = version
        self.metadata: dict[str, str] = {} if summary is None else {"Summary": summary}


class FakeEntryPoint:
    """Minimal stand-in for importlib.metadata.EntryPoint."""

    def __init__(
        self,
        name: str,
        value: str = "fake_plugin:FakePlugin",
        *,
        dist: FakeDist | None = None,
        group: str = "deeppyro.plugins",
        loaded: Any = None,
        load_error: Exception | None = None,
    ) -> None:
        self.name = name
        self.value = value
        self.dist = dist
        self.group = group
        self._loaded = loaded
        self._load_error = load_error

    def load(self) -> Any:
        if self._load_error is not None:
            raise self._load_error
        return self._loaded


class FakeEntryPoints:
    """Minimal stand-in for the modern EntryPoints collection."""

    def __init__(self, entries: Iterable[FakeEntryPoint]) -> None:
        self._entries = list(entries)

    def select(self, *, group: str | None = None) -> list[FakeEntryPoint]:
        if group is None:
            return list(self._entries)
        return [entry for entry in self._entries if entry.group == group]


def entry_points_factory(
    entries: Iterable[FakeEntryPoint],
) -> Callable[[], FakeEntryPoints]:
    """Return a factory shaped like importlib.metadata.entry_points."""
    frozen = list(entries)
    return lambda: FakeEntryPoints(frozen)


class FakeManager:
    """Duck typed PluginManager used by the PluginScreen tests.

    It records every call, can be told to raise for one operation and can
    replace its rows during refresh, which is what the screen assertions need
    without touching the real plugin sources.
    """

    def __init__(
        self,
        rows: Iterable[PluginInfo] = (),
        *,
        errors: Iterable[str] = (),
        failures: dict[str, Exception] | None = None,
        refresh_rows: Iterable[PluginInfo] | None = None,
    ) -> None:
        self.rows = list(rows)
        self.errors = list(errors)
        self.failures = dict(failures or {})
        self.refresh_rows = None if refresh_rows is None else list(refresh_rows)
        self.calls: list[tuple[str, str | None]] = []

    def _raise_if(self, operation: str) -> None:
        failure = self.failures.get(operation)
        if failure is not None:
            raise failure

    def list(self) -> list[PluginInfo]:
        return list(self.rows)

    def get(self, plugin_id: str) -> PluginInfo | None:
        return next((row for row in self.rows if row.id == plugin_id), None)

    def refresh(self) -> list[PluginInfo]:
        self.calls.append(("refresh", None))
        self._raise_if("refresh")
        if self.refresh_rows is not None:
            self.rows = list(self.refresh_rows)
        return self.list()

    def enable(self, plugin_id: str) -> PluginInfo:
        self.calls.append(("enable", plugin_id))
        self._raise_if("enable")
        self.rows = [replace(row, enabled=True) if row.id == plugin_id else row for row in self.rows]
        info = self.get(plugin_id)
        assert info is not None
        return info

    def disable(self, plugin_id: str) -> PluginInfo:
        self.calls.append(("disable", plugin_id))
        self._raise_if("disable")
        self.rows = [replace(row, enabled=False) if row.id == plugin_id else row for row in self.rows]
        info = self.get(plugin_id)
        assert info is not None
        return info

    def validate(self, plugin_id: str) -> ValidationResult:
        self.calls.append(("validate", plugin_id))
        self._raise_if("validate")
        info = self.get(plugin_id)
        if info is None:
            return ValidationResult(id=plugin_id, ok=False, error="not found")
        return ValidationResult(
            id=plugin_id,
            ok=True,
            target=info.target,
            provides=info.provides,
        )


__all__ = [
    "FakeDist",
    "FakeEntryPoint",
    "FakeEntryPoints",
    "FakeManager",
    "entry_points_factory",
    "make_info",
    "toml_string",
    "write_manifest_text",
    "write_plugin",
]
