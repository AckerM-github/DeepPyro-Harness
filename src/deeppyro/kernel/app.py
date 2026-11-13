"""Application bootstrap: compose and load plugins.

The kernel is a plugin host. Boot has four stages:

1. build the composition (core bundle, user bundles, patch manifests);
2. create the root cordis Context and register the kernel config service;
3. mount every enabled row in order, awaiting each fiber so load errors surface
   at boot instead of mid-turn;
4. expose App.ctx / App.service(name) and dispose fibers in reverse order.

Rows marked critical (the core bundle) abort boot on failure and unwind the
plugins that already started. Non-critical rows from external bundles are
recorded in App.failures and the rest of the app keeps running.

Group rows are containers, not plugin instances: the loader builds a child
cordis context for the group (applying the group's isolate/intercept layers),
mounts the nested rows in that context in order, and exposes the whole group as
one App.rows entry. The live state of a group is the aggregate of its children.

Every mount attempt is captured in a MountRecord, and App.row_states() renders
the full row tree with one of the live states: active, failed, disabled,
overridden or restart-required.
"""

from __future__ import annotations

import copy
import inspect
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from deeppyro._vendor.cordis_port import Context, Fiber
from deeppyro.api import API_VERSION, Plugin, PluginConfigError, PluginContext, PluginStatus
from deeppyro.config import Config, load_config
from deeppyro.kernel.composition import (
    Composition,
    CompositionError,
    PluginRow,
    default_composition,
    resolve_row,
    row_to_spec,
    rows_from_specs,
)
from deeppyro.kernel.config_service import provide_config
from deeppyro.kernel.manifest import PluginSpec, load_manifest

log = logging.getLogger("deeppyro.kernel")


class KernelBootError(RuntimeError):
    """Raised when a critical plugin fails to load during app bootstrap."""


@dataclass
class MountRecord:
    """One attempted row mount: the plugin instance, fiber and failure reason."""

    row: PluginRow
    fiber: Fiber | None = None
    parent: MountRecord | None = None
    children: list[MountRecord] = field(default_factory=list)
    error: str = ""
    critical: bool = True
    config: dict[str, Any] = field(default_factory=dict)
    cls: type | None = None
    instance: Any = None

    def walk(self) -> Iterable[MountRecord]:
        """Yield this record and every nested child record, depth first."""
        yield self
        for child in self.children:
            yield from child.walk()

    @property
    def plugin_id(self) -> str:
        return self.row.id


def _validate_config(cls: type, raw: Any, row: PluginRow) -> Any:
    config_cls = getattr(cls, "Config", None)
    if config_cls is None:
        return raw if raw is not None else {}
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise PluginConfigError(f"plugin {row.id!r} config must be a table, got {type(raw).__name__}")
    try:
        return config_cls.model_validate(raw)
    except Exception as exc:
        raise PluginConfigError(f"plugin {row.id!r} config is invalid: {exc}") from exc


def _teardown(instance: Any) -> Callable[[], Any] | None:
    stop = getattr(instance, "stop", None)
    if not callable(stop):
        return None

    def run() -> Any:
        result = stop()
        if inspect.isawaitable(result):

            async def drain() -> None:
                await result

            return drain()
        return None

    return run


def _api_adapter(cls: type, row: PluginRow, record: MountRecord | None = None) -> Callable[..., Any]:
    async def apply(raw_ctx: Any, raw_config: Any) -> Any:
        config = _validate_config(cls, raw_config, row)
        ctx = PluginContext(raw_ctx, plugin_id=row.id, config=config)
        instance = cls(ctx, config)
        if record is not None:
            record.instance = instance
            record.config = _config_dict(config)
        setup = getattr(instance, "setup", None)
        if callable(setup):
            result = setup(ctx, config)
            if inspect.isawaitable(result):
                await result
        teardown = _teardown(instance)
        if teardown is not None:
            return teardown

        def _keep_alive() -> None:
            # No stop hook: the returned callable is the fiber's disposer and
            # also keeps the plugin instance referenced for the fiber lifetime.
            return None

        return _keep_alive

    return apply


def _config_dict(value: Any) -> dict[str, Any]:
    """Render a plugin config (pydantic model or mapping) as a plain dict."""
    if value is None:
        return {}
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dict(copy.deepcopy(dump(mode="python")))
        except TypeError:
            return dict(copy.deepcopy(dump()))
    if isinstance(value, dict):
        return copy.deepcopy(value)
    return {"value": copy.deepcopy(value)}


def _check_name(row: PluginRow, cls: type) -> None:
    if not row.name:
        return
    accepted = {cls.__name__, f"{cls.__module__}:{cls.__name__}"}
    plugin_id = getattr(cls, "plugin_id", "")
    if plugin_id:
        accepted.add(plugin_id)
    if row.name not in accepted:
        raise CompositionError(
            f"plugin {row.id!r} name assertion failed: {row.name!r} does not match {type_name(cls)}"
        )


def type_name(cls: type) -> str:
    return f"{getattr(cls, '__module__', '?')}:{getattr(cls, '__name__', '?')}"


def _plugin_for(
    row: PluginRow,
    record: MountRecord | None = None,
) -> tuple[Any, dict[str, Any], type]:
    cls = resolve_row(row)
    _check_name(row, cls)
    if record is not None:
        record.cls = cls
    raw_config = dict(row.config or {})
    if inspect.isclass(cls) and issubclass(cls, Plugin):
        api_version = int(getattr(cls, "api_version", API_VERSION) or API_VERSION)
        if api_version > API_VERSION:
            raise CompositionError(
                f"plugin {row.id!r} targets api_version {api_version}, this kernel supports {API_VERSION}"
            )
        plugin: Any = {
            "name": row.id,
            "apply": _api_adapter(cls, row, record),
            "inject": list(getattr(cls, "inject", []) or []),
        }
        return plugin, raw_config, cls
    return cls, raw_config, cls


def _mount_context(ctx: Context, row: PluginRow) -> Context:
    """Apply a row's isolate/intercept layers to a context."""
    mount_ctx = ctx
    for name in row.isolate:
        mount_ctx = mount_ctx.isolate(name)
    for name, overrides in row.intercept.items():
        mount_ctx = mount_ctx.intercept(name, overrides)
    return mount_ctx


def _mount(ctx: Context, row: PluginRow, plugin: Any, config: dict[str, Any]) -> Fiber:
    """Mount a plugin on ctx after applying the row's isolate/intercept layers."""
    return _mount_context(ctx, row).plugin(plugin, config)


def _record_only(row: PluginRow, parent: MountRecord | None, critical: bool) -> MountRecord:
    """Build records for rows that are not mounted (disabled rows/groups)."""
    record = MountRecord(row=row, parent=parent, critical=critical, config=copy.deepcopy(row.config or {}))
    for child in row.children:
        record.children.append(_record_only(child, record, critical))
    return record


def _effective_critical(row: PluginRow, parent_critical: bool) -> bool:
    return row.critical if row.critical is not None else parent_critical


async def _mount_row(
    parent_ctx: Context,
    row: PluginRow,
    *,
    parent: MountRecord | None,
    parent_critical: bool,
    records: list[MountRecord],
    fibers: list[Fiber],
    failures: list[PluginStatus],
) -> MountRecord:
    """Mount one row (recursively for groups) and return its mount record."""
    critical = _effective_critical(row, parent_critical)
    if row.disabled is True:
        record = _record_only(row, parent, critical)
        records.append(record)
        return record
    record = MountRecord(row=row, parent=parent, critical=critical, config=copy.deepcopy(row.config or {}))
    records.append(record)
    if row.group:
        try:
            group_ctx = _mount_context(parent_ctx, row)
        except Exception as exc:
            record.error = str(exc)
            if critical:
                raise KernelBootError(f"plugin group {row.id!r} failed to mount: {exc}") from exc
            failures.append(
                PluginStatus(
                    id=row.id,
                    plugin=row.plugin,
                    state="failed",
                    critical=False,
                    origin=row.origin,
                    error=str(exc),
                )
            )
            log.warning("plugin group %s failed to mount: %s", row.id, exc)
            return record
        for child in row.children:
            child_record = await _mount_row(
                group_ctx,
                child,
                parent=record,
                parent_critical=critical,
                records=records,
                fibers=fibers,
                failures=failures,
            )
            record.children.append(child_record)
        return record
    try:
        plugin, plugin_config, _ = _plugin_for(row, record)
        fiber = _mount(parent_ctx, row, plugin, plugin_config)
        fibers.append(fiber)
        record.fiber = fiber
        await fiber
    except Exception as exc:
        record.error = str(exc)
        if critical:
            if isinstance(exc, KernelBootError):
                raise
            raise KernelBootError(
                f"plugin {row.id!r} ({row.plugin or row.name}) failed to load: {exc}"
            ) from exc
        failures.append(
            PluginStatus(
                id=row.id,
                plugin=row.plugin,
                state="failed",
                critical=False,
                origin=row.origin,
                error=str(exc),
            )
        )
        log.warning("plugin %s failed to load: %s", row.id, exc)
    return record


class App:
    """A booted DeepPyro application: root context plus loaded plugin fibers."""

    def __init__(
        self,
        ctx: Context,
        rows: Iterable[PluginRow],
        fibers: list[Fiber],
        config: Config,
        *,
        composition: Composition | None = None,
        records: list[MountRecord] | None = None,
    ) -> None:
        self.ctx = ctx
        self.config = config
        self._rows = tuple(rows)
        self._fibers = fibers
        self._composition = composition
        if records is None:
            records = [
                MountRecord(row=row, fiber=fiber, config=copy.deepcopy(row.config or {}))
                for row, fiber in zip(self._rows, self._fibers)
            ]
        self._records = records
        self._top = [record for record in records if record.parent is None]
        #: Plugin rows that failed to load while non-critical.
        self.failures: list[PluginStatus] = []
        #: Plugin ids whose config changed but could not be applied live.
        self._restart_required: set[str] = set()

    @property
    def rows(self) -> tuple[PluginRow, ...]:
        """Every loaded top-level row, in mount order (groups include children)."""
        return self._rows

    @property
    def specs(self) -> tuple[PluginSpec, ...]:
        """Loaded rows rendered as legacy plugin specs."""
        return tuple(row_to_spec(row) for row in self._rows)

    @property
    def composition(self) -> Composition | None:
        return self._composition

    @property
    def mount_records(self) -> tuple[MountRecord, ...]:
        """Every mount record (loaded, disabled and failed), in tree order."""
        return tuple(self._records)

    def mount_record(self, plugin_id: str) -> MountRecord | None:
        """Return the mount record for plugin_id, searching nested groups."""
        for record in self._records:
            if record.row.id == plugin_id:
                return record
            for nested in record.walk():
                if nested.row.id == plugin_id:
                    return nested
        return None

    # -- live row states ---------------------------------------------------
    @property
    def restart_required(self) -> frozenset[str]:
        return frozenset(self._restart_required)

    def mark_restart_required(self, plugin_id: str) -> None:
        """Remember that plugin_id needs a restart to apply a config change."""
        self._restart_required.add(plugin_id)

    def clear_restart_required(self, plugin_id: str) -> None:
        self._restart_required.discard(plugin_id)

    def row_states(self) -> list[dict[str, Any]]:
        """Render every composition row with its live state.

        States are one of active, failed, disabled, overridden or
        restart-required (plus pending for rows still waiting on injected
        services). A group row reports the aggregate of its children and the
        children themselves are listed separately with their group id as
        parent. Overridden means a later patch layer replaced the row config or
        disabled flag; the row still runs with the patched values.
        """
        rows: list[PluginRow]
        if self._composition is not None:
            rows = self._composition.flat()
        else:
            rows = [record.row for record in self._walk_records()]
        by_id = {record.row.id: record for record in self._walk_records()}
        states: list[dict[str, Any]] = []
        for row in rows:
            record = by_id.get(row.id)
            states.append(self._row_state_entry(row, record))
        return states

    def _walk_records(self) -> Iterable[MountRecord]:
        for record in self._records:
            yield from record.walk()

    def _row_state_entry(self, row: PluginRow, record: MountRecord | None) -> dict[str, Any]:
        config = record.config if record is not None else copy.deepcopy(row.config or {})
        return {
            "id": row.id,
            "plugin": row.plugin,
            "name": row.name,
            "state": self._record_state(record, row),
            "runtime_state": self._runtime_state(record),
            "critical": record.critical if record is not None else bool(row.critical),
            "origin": row.origin,
            "error": record.error if record is not None else "",
            "group": row.group,
            "parent": record.parent.row.id if record is not None and record.parent is not None else None,
            "overridden": row.overridden,
            "disabled": row.disabled is True,
            "config": copy.deepcopy(config),
            "children": [child.id for child in row.children],
        }

    def _runtime_state(self, record: MountRecord | None) -> str:
        if record is None:
            return "unmounted"
        if record.row.group:
            return "group"
        if record.fiber is None:
            return "unmounted"
        try:
            return record.fiber.get_state().name.lower()
        except Exception:  # noqa: BLE001 - a broken fiber state must not crash reporting
            return "unknown"

    def _ancestor_disabled(self, record: MountRecord | None) -> bool:
        current = record
        while current is not None:
            if current.row.disabled is True:
                return True
            current = current.parent
        return False

    def _record_state(self, record: MountRecord | None, row: PluginRow) -> str:
        if record is None:
            return "disabled" if row.disabled is True else "pending"
        if self._ancestor_disabled(record):
            return "disabled"
        if row.group:
            child_states = [self._record_state(child, child.row) for child in record.children]
            if record.error:
                return "failed"
            if any(state == "failed" for state in child_states):
                return "failed"
            if row.id in self._restart_required or any(state == "restart-required" for state in child_states):
                return "restart-required"
            if row.overridden or any(state == "overridden" for state in child_states):
                return "overridden"
            if all(state in ("active", "overridden", "disabled") for state in child_states):
                return "active"
            return "pending"
        runtime_state = self._runtime_state(record)
        if record.error or runtime_state == "failed":
            return "failed"
        if row.id in self._restart_required:
            return "restart-required"
        if row.overridden and runtime_state in ("active", "loading"):
            return "overridden"
        if runtime_state in ("active", "loading"):
            return "active"
        if runtime_state == "unmounted":
            return "disabled" if row.disabled is True else "pending"
        return "pending"

    # -- service access ----------------------------------------------------
    def service(self, name: str) -> Any:
        """Return an active service by name, or raise LookupError.

        cordis ctx.foo returns None for unknown services instead of raising,
        so we must go through ctx.reflect.get to distinguish a missing service
        from a falsy one.
        """
        value = self.ctx.reflect.get(name)
        if value is None:
            raise LookupError(f"service {name!r} is not registered or not active")
        return value

    def has_service(self, name: str) -> bool:
        return self.ctx.reflect.get(name) is not None

    @property
    def started(self) -> dict[str, str]:
        """id -> fiber state name for every loaded top-level plugin."""
        started: dict[str, str] = {}
        loaded = {row.id for row in self._rows}
        for record in self._top:
            if record.row.id not in loaded:
                continue
            started[record.row.id] = self._started_name(record)
        return started

    def _started_name(self, record: MountRecord) -> str:
        state = self._record_state(record, record.row)
        mapping = {
            "active": "ACTIVE",
            "overridden": "ACTIVE",
            "restart-required": "ACTIVE",
            "disabled": "DISABLED",
            "pending": "PENDING",
            "failed": "FAILED",
        }
        return mapping.get(state, state.upper())

    # -- lifecycle ---------------------------------------------------------
    async def stop(self) -> None:
        """Dispose plugin fibers in reverse load order."""
        await _dispose_all(self._fibers)
        self._fibers.clear()


async def _dispose_all(fibers: list[Fiber]) -> None:
    for fiber in reversed(fibers):
        try:
            result = fiber.dispose()
            if inspect.isawaitable(result):
                await result
        except Exception:
            log.exception("plugin dispose failed")


def _resolve_config(config: Config | dict[str, Any] | None) -> Config:
    if config is None:
        return load_config()
    if isinstance(config, Config):
        return config
    return Config.model_validate(config)


def _build_composition(
    plugins: Iterable[PluginSpec],
    manifest_paths: Iterable[str],
    extra_plugins: Iterable[PluginSpec],
    bundle_paths: Iterable[str],
) -> Composition:
    plugins = tuple(plugins)
    manifest_paths = tuple(manifest_paths)
    extra_plugins = tuple(extra_plugins)
    if plugins:
        composition = Composition()
        composition.apply(rows_from_specs(plugins, origin="plugins"), critical=True)
        for path in manifest_paths:
            composition.apply(
                rows_from_specs(load_manifest(str(path)), origin=f"manifest:{path}"),
                critical=True,
            )
    else:
        composition = default_composition(
            bundle_paths=bundle_paths,
            manifest_paths=manifest_paths,
        )
    if extra_plugins:
        composition.apply(rows_from_specs(extra_plugins, origin="extra"), critical=True)
    return composition


def _attach_kernel(app: App) -> None:
    """Give the kernel runtime service a handle on the booted app."""
    try:
        service = app.service("kernel")
    except LookupError:
        return
    attach = getattr(service, "attach", None)
    if not callable(attach):
        return
    try:
        attach(app)
    except Exception:  # pragma: no cover - a broken runtime must not fail boot
        log.exception("kernel runtime service failed to attach to the app")


async def create_app(
    config: Config | dict[str, Any] | None = None,
    *,
    plugins: Iterable[PluginSpec] = (),
    manifest_paths: Iterable[str] = (),
    extra_plugins: Iterable[PluginSpec] = (),
    composition: Composition | None = None,
    bundle_paths: Iterable[str] = (),
) -> App:
    """Boot a DeepPyro app.

    Args:
        config: validated config object, raw dict, or None to load defaults.
        plugins: explicit bundle-layer specs; when given they replace the core
            bundle (tests and embedders use this).
        manifest_paths: patch-layer TOML manifests applied on top.
        extra_plugins: patch entries applied last (same replacement semantics).
        composition: a pre-built composition; overrides the three above.
        bundle_paths: additional bundle directories/files to load on top of the
            core bundle before user bundles.
    """
    resolved = _resolve_config(config)
    if composition is None:
        composition = _build_composition(plugins, manifest_paths, extra_plugins, bundle_paths)

    ctx = Context()
    provide_config(ctx, resolved)

    fibers: list[Fiber] = []
    records: list[MountRecord] = []
    failures: list[PluginStatus] = []
    rows: list[PluginRow] = []
    try:
        for row in composition.rows():
            record = await _mount_row(
                ctx,
                row,
                parent=None,
                parent_critical=True,
                records=records,
                fibers=fibers,
                failures=failures,
            )
            if row.disabled is True or record.error:
                continue
            rows.append(row)
    except KernelBootError:
        await _dispose_all(fibers)
        raise

    app = App(ctx, rows, fibers, resolved, composition=composition, records=records)
    app.failures = failures
    _attach_kernel(app)
    return app


__all__ = ["App", "KernelBootError", "MountRecord", "create_app"]
