"""The presets service: named per-session agent compositions.

A preset is the smallest per-session composition unit. Every registration is an
immutable revision: tools and prompt sections registered with scope=<preset id>
stay visible to agents created with that preset, and editing a preset only
affects agents created afterwards. Live agents keep the revision they were
created with.

A preset revision may also carry plugin rows. Those rows are materialized once
(on registration and/or first resolve) inside a child of the host composition,
so preset plugins see host services while their contributions are pinned to the
revision that created them. A failing row is recorded and skipped instead of
taking the host down.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field, replace
from typing import Any

from deeppyro.api import (
    AGENT_MODE_DEFAULT,
    AGENT_MODE_TEAM,
    API_VERSION,
    DEFAULT_PRESET_ID,
    TEAM_PRESET_ID,
    Plugin,
    PluginConfigError,
    PluginContext,
    PluginDependencyError,
    PresetPluginRow,
    PresetSpec,
    Service,
)

logger = logging.getLogger("deeppyro.presets")


@dataclass(frozen=True, slots=True)
class RealmFailure:
    """One preset plugin row that failed to mount.

    A failed row never takes the host or the other rows down: the failure is
    recorded on the realm so hosts can surface it (inspect, doctor, logs).
    """

    preset_id: str
    revision: int
    row_id: str
    plugin: str
    error: str


@dataclass(slots=True)
class _RealmState:
    """Materialization state for one (preset id, revision) pair."""

    preset_id: str
    revision: int
    ready: bool = False
    fibers: list[Any] = field(default_factory=list)
    failures: list[RealmFailure] = field(default_factory=list)
    task: asyncio.Task[Any] | None = None


def _validate_config(plugin_cls: type, raw: Any, row_id: str) -> Any:
    """Validate one realm row config the same way the kernel validates a bundle row."""
    config_cls = getattr(plugin_cls, "Config", None)
    if config_cls is None:
        return raw if raw is not None else {}
    if not isinstance(raw, dict):
        raise PluginConfigError(f"plugin {row_id!r} config must be a table, got {type(raw).__name__}")
    if not isinstance(config_cls, type):
        return raw
    try:
        return config_cls.model_validate(raw)
    except Exception as exc:
        raise PluginConfigError(f"plugin {row_id!r} config is invalid: {exc}") from exc


def _api_adapter(preset: PresetSpec, plugin_cls: type) -> Any:
    """Build the cordis apply callback for one api.Plugin realm row.

    Mirrors the kernel adapter, except the PluginContext carries the preset
    scope/revision defaults so contributions are pinned to this revision.
    """

    async def apply(raw_ctx: Any, raw_config: Any) -> Any:
        config = _validate_config(plugin_cls, raw_config, plugin_cls.plugin_id or plugin_cls.__name__)
        ctx = PluginContext(
            raw_ctx,
            plugin_id=plugin_cls.plugin_id,
            config=config,
            default_scope=preset.id,
            default_revision=preset.revision,
        )
        instance = plugin_cls(ctx, config)
        setup = getattr(instance, "setup", None)
        if callable(setup):
            result = setup(ctx, config)
            if inspect.isawaitable(result):
                await result
        stop = getattr(instance, "stop", None)
        if callable(stop):

            async def teardown() -> None:
                result = stop()
                if inspect.isawaitable(result):
                    await result

            return teardown

        def keep_alive() -> None:
            return None

        return keep_alive

    return apply


class PresetsPlugin(Service):
    """Registry of agent presets; always offers the host-wide default."""

    provide = "presets"
    plugin_id = "presets"
    version = "0.2.0"
    description = "agent preset revision registry with per-preset plugin realms"

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        # The registry is a Cordis service, so methods invoked through a tracked
        # service proxy receive the proxy as ``self``; on a proxy ``self.ctx``
        # resolves to the *use-site* context. Keep the fiber's own raw context
        # captured at construction time for mounting preset realms.
        self._raw_ctx: Any = getattr(ctx, "raw", None)
        if self._raw_ctx is None:
            self._raw_ctx = ctx
        default = PresetSpec(
            id=DEFAULT_PRESET_ID,
            name="Default",
            description="host-wide tools and prompt sections",
            order=0,
            plugin_id="presets",
            revision=0,
            mode=AGENT_MODE_DEFAULT,
        )
        #: The team preset is the explicit team-mode switch (/preset team):
        #: it exposes the team tools and hides one-shot delegation instead.
        team = PresetSpec(
            id=TEAM_PRESET_ID,
            name="Team",
            description="explicit team mode: orchestrate persistent members instead of one-shot delegation",
            order=200,
            plugin_id="presets",
            revision=0,
            mode=AGENT_MODE_TEAM,
        )
        #: preset id -> {revision: immutable spec}
        self._presets: dict[str, dict[int, PresetSpec]] = {
            DEFAULT_PRESET_ID: {0: default},
            TEAM_PRESET_ID: {0: team},
        }
        #: preset id -> latest revision
        self._latest: dict[str, int] = {DEFAULT_PRESET_ID: 0, TEAM_PRESET_ID: 0}
        #: host-assigned monotonic revision counter
        self._next_revision = 1
        #: (preset id, revision) -> realm materialization state
        self._realms: dict[tuple[str, int], _RealmState] = {}

    # -- registration ----------------------------------------------------
    def add(self, preset: PresetSpec, *, plugin_id: str = "") -> Any:
        """Register a new immutable revision; the returned callable removes it.

        Registering an existing id adds a revision instead of raising, so a
        plugin can ship an updated composition without a restart. The host
        assigns the revision number; any value on the incoming spec is ignored.
        """
        if not isinstance(preset, PresetSpec):
            raise PluginDependencyError(f"expected PresetSpec, got {type(preset).__name__}")
        preset_id = (preset.id or "").strip()
        if not preset_id:
            raise PluginDependencyError("preset id must not be empty")
        revision = self._next_revision
        self._next_revision += 1
        stored = replace(
            preset,
            id=preset_id,
            plugin_id=preset.plugin_id or plugin_id,
            revision=revision,
        )
        history = self._presets.setdefault(preset_id, {})
        history[revision] = stored
        self._latest[preset_id] = revision
        self._schedule(stored)

        def remove() -> None:
            state = self._drop(preset_id, revision)
            if state is None:
                return
            if state.task is not None and not state.task.done():
                state.task.cancel()
            self._dispose_soon(state)

        return remove

    def _drop(self, preset_id: str, revision: int) -> _RealmState | None:
        """Forget one revision and return its realm state (if any)."""
        history = self._presets.get(preset_id)
        if not history or revision not in history:
            return None
        del history[revision]
        state = self._realms.pop((preset_id, revision), None)
        if not history:
            self._presets.pop(preset_id, None)
            self._latest.pop(preset_id, None)
        elif self._latest.get(preset_id) == revision:
            self._latest[preset_id] = max(history)
        return state

    async def remove_revision(self, preset_id: str, revision: int) -> bool:
        """Remove one revision and await the teardown of its realm."""
        state = self._drop(preset_id, revision)
        if state is None:
            return False
        if state.task is not None and not state.task.done():
            state.task.cancel()
        await self._dispose_state(state)
        return True

    # -- lookup ----------------------------------------------------------
    def get(self, preset_id: str | None) -> PresetSpec | None:
        """Return the latest revision of one preset, or None."""
        if not preset_id:
            return None
        revision = self._latest.get(preset_id)
        if revision is None:
            return None
        return self._presets[preset_id][revision]

    def resolve(self, preset_id: str | None = None, *, revision: int | None = None) -> PresetSpec:
        """Return one preset spec; unknown ids or revisions raise.

        With revision=None the latest revision is returned (the stable view for
        new agents); passing a revision pins the caller to that snapshot, which
        is how live agents keep seeing the composition they started with.
        """
        wanted = (preset_id or DEFAULT_PRESET_ID).strip() or DEFAULT_PRESET_ID
        history = self._presets.get(wanted)
        if history is None:
            raise PluginDependencyError(f"unknown agent preset: {wanted!r}")
        if revision is None:
            spec = history[self._latest[wanted]]
        else:
            spec = history.get(revision)
            if spec is None:
                available = sorted(history)
                raise PluginDependencyError(
                    f"unknown agent preset revision: {wanted!r} revision {revision} (available: {available})"
                )
        self._schedule(spec)
        return spec

    def resolve_revision(self, preset_id: str, revision: int) -> PresetSpec:
        """Return one exact, immutable preset revision."""
        return self.resolve(preset_id, revision=revision)

    def latest(self, preset_id: str | None = None) -> PresetSpec:
        """Return the latest revision of a preset (alias of resolve())."""
        return self.resolve(preset_id)

    def revision_of(self, preset_id: str) -> int | None:
        """Latest revision number of one preset, or None when unknown."""
        return self._latest.get(preset_id)

    def revisions(self, preset_id: str) -> list[int]:
        """Every still-registered revision number of one preset, ascending."""
        history = self._presets.get(preset_id)
        return sorted(history) if history else []

    def default(self) -> PresetSpec:
        """The latest revision of the host-wide default preset."""
        return self.resolve(None)

    def all(self) -> list[PresetSpec]:
        """Latest revision of every registered preset, ordered for display."""
        specs = [self._presets[preset_id][revision] for preset_id, revision in self._latest.items()]
        return sorted(specs, key=lambda item: (item.order, item.id))

    def ids(self) -> list[str]:
        return [spec.id for spec in self.all()]

    # -- realm materialization -------------------------------------------
    def _schedule(self, spec: PresetSpec) -> None:
        """Start materializing a revision's realm once, without blocking.

        Registration happens in synchronous plugin setup code, so the eager
        mount can only be scheduled. Hosts that need the realm before creating
        agents call ensure()/ensure_all().
        """
        if not spec.plugins:
            return
        key = (spec.id, spec.revision)
        if key in self._realms:
            return
        state = _RealmState(preset_id=spec.id, revision=spec.revision)
        self._realms[key] = state
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop yet: ensure() will mount later
            return
        state.task = loop.create_task(self._mount(state, spec))

    async def ensure(self, preset_id: str | None = None, *, revision: int | None = None) -> PresetSpec:
        """Materialize (if needed) and return one preset revision.

        Idempotent: concurrent callers share the same mount. Hosts must await
        this (or ensure_all) before creating agents for a revision that carries
        plugin rows, otherwise realm contributions are not registered yet.
        """
        spec = self.resolve(preset_id, revision=revision)
        if not spec.plugins:
            return spec
        state = self._realms.get((spec.id, spec.revision))
        if state is None:  # pragma: no cover - resolve() always schedules
            return spec
        task = state.task
        if task is None or task.done():
            if state.ready:
                return spec
            task = asyncio.ensure_future(self._mount(state, spec))
            state.task = task
        await asyncio.shield(task)
        return spec

    async def ensure_all(self) -> None:
        """Materialize every realm of every preset's latest revision."""
        for preset_id, revision in list(self._latest.items()):
            spec = self._presets[preset_id][revision]
            if spec.plugins:
                await self.ensure(preset_id, revision=revision)

    def is_materialized(self, preset_id: str, revision: int | None = None) -> bool:
        """Whether a preset revision's realm rows were mounted (or none exist)."""
        wanted = self._latest.get(preset_id) if revision is None else revision
        if wanted is None:
            return False
        spec = self._presets.get(preset_id, {}).get(wanted)
        if spec is None:
            return False
        if not spec.plugins:
            return True
        state = self._realms.get((preset_id, wanted))
        return bool(state and state.ready)

    def realm_failures(self, preset_id: str, revision: int | None = None) -> list[RealmFailure]:
        """Failures recorded while mounting a revision's realm."""
        wanted = self._latest.get(preset_id) if revision is None else revision
        if wanted is None:
            return []
        state = self._realms.get((preset_id, wanted))
        return list(state.failures) if state else []

    def materialized_revisions(self) -> list[tuple[str, int]]:
        """Every realm that has been (or is being) materialized, for diagnostics."""
        return sorted(self._realms)

    async def _mount(self, state: _RealmState, spec: PresetSpec) -> None:
        base_ctx = self._raw_ctx
        for row in spec.plugins:
            if row.disabled:
                continue
            fiber: Any = None
            try:
                plugin, config = self._row_plugin(spec, row)
                mount_ctx = base_ctx
                for name in row.isolate:
                    mount_ctx = mount_ctx.isolate(name)
                fiber = mount_ctx.plugin(plugin, config)
                await fiber
            except asyncio.CancelledError:
                if fiber is not None:
                    await self._dispose_fiber(fiber)
                raise
            except Exception as exc:  # noqa: BLE001 - one bad row must not kill the host
                failure = RealmFailure(
                    preset_id=spec.id,
                    revision=spec.revision,
                    row_id=row.id,
                    plugin=row.plugin,
                    error=f"{type(exc).__name__}: {exc}",
                )
                state.failures.append(failure)
                if fiber is not None:
                    # A partially loaded fiber still owns effects; keep it so the
                    # realm teardown can dispose it together with the others.
                    state.fibers.append(fiber)
                logger.warning(
                    "preset %r revision %d: plugin row %r failed to mount: %s",
                    spec.id,
                    spec.revision,
                    row.id,
                    failure.error,
                )
                continue
            state.fibers.append(fiber)
        state.ready = True

    def _row_plugin(self, spec: PresetSpec, row: PresetPluginRow) -> tuple[Any, Any]:
        """Resolve one row into (cordis plugin, config), like the kernel does."""
        from deeppyro.kernel.composition import PluginRow, resolve_row

        plugin_row = PluginRow(
            id=row.id,
            plugin=row.plugin,
            config=dict(row.config or {}),
            base=spec.base,
            origin=f"preset:{spec.id}@{spec.revision}",
        )
        plugin_cls = resolve_row(plugin_row)
        raw_config = dict(row.config or {})
        if not isinstance(plugin_cls, type) or not issubclass(plugin_cls, Plugin):
            return plugin_cls, raw_config
        api_version = int(getattr(plugin_cls, "api_version", API_VERSION) or API_VERSION)
        if api_version > API_VERSION:
            raise PluginConfigError(
                f"plugin {row.id!r} requires api version {api_version} (host supports {API_VERSION})"
            )
        plugin = {
            "name": row.id,
            "apply": _api_adapter(spec, plugin_cls),
            "inject": list(getattr(plugin_cls, "inject", []) or []),
        }
        return plugin, raw_config

    # -- disposal --------------------------------------------------------
    def _dispose_soon(self, state: _RealmState) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._dispose_no_loop(state)
            return
        loop.create_task(self._dispose_state(state))

    def _dispose_no_loop(self, state: _RealmState) -> None:
        for fiber in reversed(state.fibers):
            try:
                fiber.dispose()
            except Exception:
                logger.exception("preset realm %r teardown failed", state.preset_id)
        state.fibers.clear()

    async def _dispose_state(self, state: _RealmState) -> None:
        for fiber in reversed(state.fibers):
            await self._dispose_fiber(fiber)
        state.fibers.clear()

    async def _dispose_fiber(self, fiber: Any) -> None:
        try:
            result = fiber.dispose()
        except Exception:
            logger.exception("preset realm fiber teardown failed")
            return
        if inspect.isawaitable(result):
            try:
                await result
            except Exception:
                logger.exception("preset realm fiber teardown failed")


__all__ = ["DEFAULT_PRESET_ID", "TEAM_PRESET_ID", "PresetsPlugin", "RealmFailure"]
