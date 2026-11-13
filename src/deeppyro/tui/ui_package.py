"""Declarative TUI UI packages (`ui.toml`) for installed plugin bundles.

A bundle can extend the terminal UI without importing Textual or patching the
host: it ships a `ui.toml` next to its `bundle.toml` and names callables by
`module:function` spec. The loader imports those modules *lazily* (at package
load time) and registers their callables in the shared
:class:`~deeppyro.tui.slots.SlotRegistry`; the callables themselves run only
when the host renders a status line, conversation event or panel.

Example bundle layout::

    ~/.dpro/bundles/acme/
        bundle.toml          # [bundle] id = "acme"
        ui.toml              # [[status]] / [[conversation]] / ...
        acme_ui.py           # renderers and factories
        locale/en.json       # {"status.acme": "Acme"}

`ui.toml`::

    [[status]]
    id = "clock"
    order = 50
    renderer = "acme_ui:clock"

    [[conversation]]
    id = "todo"
    predicate = "acme_ui:is_todo_event"
    factory = "acme_ui:todo_row"

    [[panels]]
    id = "acme"
    title = "Acme"
    factory = "acme_ui:panel"

    [[settings]]
    id = "acme-settings"
    title = "Acme settings"
    factory = "acme_ui:settings"

    [theme]
    accent = "#7aa2f7"

Failure isolation is a hard rule: one malformed table, missing module or
raising factory is recorded in :class:`UIPackageReport.errors` and the loader
continues with the next entry, the next package and finally returns normally.
The TUI must boot even when every installed UI package is broken.
"""

from __future__ import annotations

import importlib
import json
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from deeppyro.core.errors import DeeppyroError
from deeppyro.paths import deeppyro_home
from deeppyro.tui.slots import (
    CONVERSATION,
    PANEL,
    SETTINGS,
    STATUS,
    SlotRegistry,
)

__all__ = [
    "UIPackageError",
    "UIPackageReport",
    "default_ui_bundles_root",
    "load_installed_ui_packages",
    "load_ui_package",
    "load_ui_package_report",
]

#: Sections understood by `ui.toml` and the slot kind each one feeds.
_SECTION_KINDS: dict[str, str] = {
    "status": STATUS,
    "conversation": CONVERSATION,
    "panels": PANEL,
    "settings": SETTINGS,
}

#: Type of the optional import seam used by tests and embedders.
ImportModule = Callable[[str], ModuleType]


class UIPackageError(DeeppyroError):
    """Raised for invalid loader arguments (never for a broken package)."""


@dataclass(frozen=True, slots=True)
class UIPackageReport:
    """Outcome of loading one declarative UI package.

    Attributes:
        id: Bundle id (from `bundle.toml`), or the directory name as fallback.
        path: Path to the `ui.toml` that was read.
        slot_ids: Ids of successfully registered slots, in declaration order.
        errors: Human-readable failures; empty means the package loaded cleanly.
    """

    id: str
    path: Path
    slot_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the package loaded without any recorded error."""
        return not self.errors

    def summary(self) -> str:
        """Return a one-line description suitable for diagnostics."""
        state = "ok" if self.ok else f"{len(self.errors)} error(s)"
        return f"{self.id}: {len(self.slot_ids)} slot(s), {state}"


def default_ui_bundles_root() -> Path:
    """Return the directory scanned for user bundles: `<home>/bundles`."""
    return deeppyro_home() / "bundles"


def load_ui_package(
    path: str | Path,
    registry: SlotRegistry,
    *,
    import_module: ImportModule | None = None,
) -> list[str]:
    """Load one `ui.toml` package into *registry*.

    Args:
        path: Bundle directory or the `ui.toml` file itself.
        registry: Registry that receives the contributions.
        import_module: Optional import seam (`importlib.import_module` by
            default). When provided, the package directory is not added to
            `sys.path`, which keeps tests hermetic.

    Returns:
        Ids of the slots that were registered successfully. Errors are
        available from :func:`load_ui_package_report`; this convenience wrapper
        deliberately never raises for a broken package.
    """
    report = load_ui_package_report(path, registry, import_module=import_module)
    return list(report.slot_ids)


def load_ui_package_report(
    path: str | Path,
    registry: SlotRegistry,
    *,
    import_module: ImportModule | None = None,
) -> UIPackageReport:
    """Load one package and return the full report, including failures."""
    if not isinstance(registry, SlotRegistry):
        raise UIPackageError("registry must be a SlotRegistry")
    base, ui_path, locate_errors = _locate_package(Path(path))
    fallback_id = base.name or "ui-package"
    if locate_errors:
        return UIPackageReport(id=fallback_id, path=ui_path, errors=tuple(locate_errors))

    try:
        document = _read_toml(ui_path)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        message = f"{ui_path}: cannot read ui.toml: {type(exc).__name__}: {exc}"
        return UIPackageReport(id=fallback_id, path=ui_path, errors=(message,))

    errors: list[str] = []
    bundle_id = _bundle_id(base, errors)
    slot_ids: list[str] = []

    inserted_path = False
    base_text = str(base)
    if import_module is None and base_text not in sys.path:
        sys.path.insert(0, base_text)
        inserted_path = True
    try:
        _load_theme(document, registry, ui_path, errors)
        _load_locale(base, registry, errors)
        for section, kind in _SECTION_KINDS.items():
            entries = _entries(document, section, errors)
            for index, entry in enumerate(entries):
                slot_id = _load_entry(
                    kind,
                    section,
                    index,
                    entry,
                    base,
                    bundle_id,
                    registry,
                    import_module,
                    ui_path,
                    errors,
                )
                if slot_id is not None:
                    slot_ids.append(slot_id)
    finally:
        if inserted_path:
            try:
                sys.path.remove(base_text)
            except ValueError:  # pragma: no cover - another thread removed it
                pass

    return UIPackageReport(
        id=bundle_id,
        path=ui_path,
        slot_ids=tuple(slot_ids),
        errors=tuple(errors),
    )


def load_installed_ui_packages(
    registry: SlotRegistry,
    *,
    root: str | Path | None = None,
    import_module: ImportModule | None = None,
) -> list[UIPackageReport]:
    """Load every `<root>/<bundle>/ui.toml`, sorted by directory name.

    Args:
        registry: Registry that receives every package's contributions.
        root: Bundle root; defaults to `~/.dpro/bundles`.
        import_module: Optional import seam forwarded to each package.

    Returns:
        One report per bundle that ships a `ui.toml`. A missing or empty root
        returns `[]`; failures are reported per package and never raised.
    """
    if not isinstance(registry, SlotRegistry):
        raise UIPackageError("registry must be a SlotRegistry")
    base = Path(root) if root is not None else default_ui_bundles_root()
    if not base.is_dir():
        return []

    reports: list[UIPackageReport] = []
    for child in sorted(base.iterdir(), key=lambda item: item.name):
        if not child.is_dir():
            continue
        ui_path = child / "ui.toml"
        if not ui_path.is_file():
            continue
        try:
            reports.append(load_ui_package_report(child, registry, import_module=import_module))
        except Exception as exc:  # noqa: BLE001 - isolate every optional package
            message = f"{ui_path}: unexpected loader failure: {type(exc).__name__}: {exc}"
            reports.append(UIPackageReport(id=child.name, path=ui_path, errors=(message,)))
    return reports


# -- internals ----------------------------------------------------------


def _locate_package(path: Path) -> tuple[Path, Path, list[str]]:
    candidate = path.expanduser()
    if candidate.is_dir():
        base = candidate
        ui_path = base / "ui.toml"
    elif candidate.is_file():
        ui_path = candidate
        base = candidate.parent
    else:
        return candidate, candidate, [f"UI package not found: {candidate}"]

    if not ui_path.is_file():
        return base, ui_path, [f"{base}: missing ui.toml"]
    return base, ui_path, []


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _bundle_id(base: Path, errors: list[str]) -> str:
    fallback = base.name or "ui-package"
    bundle_path = base / "bundle.toml"
    if not bundle_path.is_file():
        return fallback
    try:
        with bundle_path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        errors.append(f"{bundle_path}: cannot read bundle.toml: {type(exc).__name__}: {exc}")
        return fallback
    bundle = document.get("bundle")
    if not isinstance(bundle, Mapping):
        errors.append(f"{bundle_path}: missing [bundle] table")
        return fallback
    value = bundle.get("id")
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{bundle_path}: [bundle].id must be a non-empty string")
        return fallback
    return value.strip()


def _entries(document: Mapping[str, Any], section: str, errors: list[str]) -> list[Any]:
    raw = document.get(section)
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append(f"ui.toml: {section} must be an array of tables")
        return []
    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            errors.append(f"ui.toml: {section}[{index}] must be a table")
    return list(raw)


def _load_theme(
    document: Mapping[str, Any],
    registry: SlotRegistry,
    path: Path,
    errors: list[str],
) -> None:
    raw = document.get("theme")
    if raw is None:
        return
    if not isinstance(raw, Mapping):
        errors.append(f"{path}: [theme] must be a table")
        return
    try:
        registry.set_theme({**registry.theme(), **{str(key): value for key, value in raw.items()}})
    except Exception as exc:  # noqa: BLE001 - a broken theme must not stop the package
        errors.append(f"{path}: [theme]: {type(exc).__name__}: {exc}")


def _load_locale(base: Path, registry: SlotRegistry, errors: list[str]) -> None:
    locale_path = base / "locale" / "en.json"
    if not locale_path.is_file():
        return
    try:
        raw = json.loads(locale_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{locale_path}: cannot read locale: {type(exc).__name__}: {exc}")
        return
    if not isinstance(raw, Mapping):
        errors.append(f"{locale_path}: locale must be a JSON object of strings")
        return
    try:
        registry.add_locale({str(key): value for key, value in raw.items()})
    except Exception as exc:  # noqa: BLE001 - translations are optional
        errors.append(f"{locale_path}: invalid locale: {type(exc).__name__}: {exc}")


def _load_entry(
    kind: str,
    section: str,
    index: int,
    entry: Any,
    base: Path,
    bundle_id: str,
    registry: SlotRegistry,
    import_module: ImportModule | None,
    path: Path,
    errors: list[str],
) -> str | None:
    if not isinstance(entry, Mapping):
        return None
    label = f"{section}[{index}]"
    slot_id = _optional_string(entry, "id", label, path, errors)
    if slot_id is None:
        errors.append(f"{path}: {label}: id must be a non-empty string")
        return None
    title = _optional_string(entry, "title", label, path, errors) or slot_id
    order = _order(entry, label, path, errors)
    if order is None:
        return None

    try:
        if kind == STATUS:
            renderer = _callable(entry, "renderer", base, label, import_module, path, errors)
            if renderer is None:
                return None
            registry.add_status_item(renderer, order, id=slot_id, plugin_id=bundle_id)
        elif kind == CONVERSATION:
            factory = _callable(entry, "factory", base, label, import_module, path, errors)
            if factory is None:
                return None
            predicate: Callable[[dict[str, Any]], bool] | None = None
            if "predicate" in entry:
                predicate_spec = _optional_string(entry, "predicate", label, path, errors)
                if predicate_spec is None:
                    return None
                resolved = _resolve_callable(
                    predicate_spec, base, label, import_module, path, errors, key="predicate"
                )
                if resolved is None:
                    return None
                predicate = resolved
            registry.add_conversation_row(
                predicate or _always_true,
                factory,
                order,
                id=slot_id,
                plugin_id=bundle_id,
                title=title,
            )
        elif kind == PANEL:
            factory = _callable(entry, "factory", base, label, import_module, path, errors)
            if factory is None:
                return None
            registry.add_panel(title, factory, order, id=slot_id, plugin_id=bundle_id)
        elif kind == SETTINGS:
            factory = _callable(entry, "factory", base, label, import_module, path, errors)
            if factory is None:
                return None
            registry.add_settings_page(title, factory, order, id=slot_id, plugin_id=bundle_id)
        else:  # pragma: no cover - guarded by _SECTIONS
            errors.append(f"{path}: {label}: unsupported section {kind!r}")
            return None
    except Exception as exc:  # noqa: BLE001 - isolate each declaration
        errors.append(f"{path}: {label} ({slot_id}): {type(exc).__name__}: {exc}")
        return None
    return slot_id


def _always_true(event: Mapping[str, Any]) -> bool:
    return True


def _callable(
    entry: Mapping[str, Any],
    key: str,
    base: Path,
    label: str,
    import_module: ImportModule | None,
    path: Path,
    errors: list[str],
) -> Callable[..., Any] | None:
    spec = _optional_string(entry, key, label, path, errors)
    if spec is None:
        errors.append(f"{path}: {label}: {key} must be a 'module:function' string")
        return None
    return _resolve_callable(spec, base, label, import_module, path, errors, key=key)


def _resolve_callable(
    spec: str,
    base: Path,
    label: str,
    import_module: ImportModule | None,
    path: Path,
    errors: list[str],
    *,
    key: str,
) -> Callable[..., Any] | None:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name.strip() or not attribute.strip():
        errors.append(f"{path}: {label}: {key} must look like 'module:function', got {spec!r}")
        return None
    module_name = module_name.strip()
    attribute = attribute.strip()
    importer = import_module or importlib.import_module
    try:
        module = importer(module_name)
    except Exception as exc:  # noqa: BLE001 - report the import and keep going
        errors.append(
            f"{path}: {label}: cannot import {key} module {module_name!r}: {type(exc).__name__}: {exc}"
        )
        return None
    target: Any = module
    try:
        for part in attribute.split("."):
            target = getattr(target, part)
    except AttributeError as exc:
        errors.append(f"{path}: {label}: {key} target {spec!r} not found: {exc}")
        return None
    if not callable(target):
        errors.append(f"{path}: {label}: {key} target {spec!r} is not callable")
        return None
    # Keep the source module visible in tracebacks.
    if getattr(target, "__module__", None) is None:
        try:
            target.__module__ = module.__name__
        except (AttributeError, TypeError):  # pragma: no cover - builtins
            pass
    return target


def _optional_string(
    entry: Mapping[str, Any],
    key: str,
    label: str,
    path: Path,
    errors: list[str],
) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path}: {label}: {key} must be a non-empty string")
        return None
    return value.strip()


def _order(entry: Mapping[str, Any], label: str, path: Path, errors: list[str]) -> int | None:
    value = entry.get("order", 100)
    if not isinstance(value, int) or isinstance(value, bool):
        errors.append(f"{path}: {label}: order must be an integer")
        return None
    return value
