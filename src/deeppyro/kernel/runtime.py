"""Kernel runtime service: live plugin registry and config application.

The core bundle mounts this module as its very first row, so every plugin after
it may inject the "kernel" service and ask what is mounted:

    from deeppyro.api import Plugin

    class MyPlugin(Plugin):
        inject = ["kernel"]

        def setup(self, ctx, config):
            for plugin in ctx.get("kernel").mounted_plugins():
                ctx.logger.info(plugin["id"], plugin["state"])

Two methods form the stable contract with the plugin manager, the settings
plugin and the TUI inspector:

* mounted_plugins() -> list[dict] returns every composition row with its live
  state (active, failed, disabled, overridden, restart-required).
* apply_plugin_config(plugin_id, config) -> bool applies a config patch to an
  already mounted plugin when every changed field is listed in the plugin's
  volatile class attribute. It returns False without touching the instance
  when the plugin is unknown or unmounted, when it declares no volatile
  fields, when the patch changes a non-volatile field, or when the new config
  is invalid. A rejected row is reported as restart-required so the UI can ask
  the user to restart instead.

Non-volatile configuration still needs a restart: bundle rows are the source of
truth at boot and hot-reloading a module is out of scope (see
docs/spec/plugin-core.md section 9).
"""

from __future__ import annotations

import copy
import inspect
import logging
from typing import Any, ClassVar

from deeppyro.api import Service

log = logging.getLogger("deeppyro.kernel")

_MISSING = object()


def _config_dict(value: Any) -> dict[str, Any]:
    """Render a plugin config (pydantic model or mapping) as a plain dict."""
    if value is None:
        return {}
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dict(copy.deepcopy(dump(mode="python")))
        except TypeError:  # pragma: no cover - very old pydantic models
            return dict(copy.deepcopy(dump()))
    if isinstance(value, dict):
        return copy.deepcopy(value)
    return {"value": copy.deepcopy(value)}


def _same(left: Any, right: Any) -> bool:
    if left is _MISSING:
        return False
    try:
        return bool(left == right)
    except Exception:  # noqa: BLE001 - exotic equality must not break comparison
        return left is right


class KernelRuntime(Service):
    """The kernel service: live plugin states and volatile config updates."""

    provide: ClassVar[str] = "kernel"
    plugin_id: ClassVar[str] = "kernel"
    version: ClassVar[str] = "0.1.0"
    description: ClassVar[str] = "Kernel runtime: mounted plugin registry and live config"
    inject: ClassVar[list[str]] = ["config"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx, config)
        self._app: Any = None

    # -- attachment --------------------------------------------------------
    def attach(self, app: Any) -> None:
        """Bind the booted App this service reports on (called by create_app)."""
        self._app = app

    @property
    def app(self) -> Any:
        """The booted App, or None when the service is not attached yet."""
        return self._app

    # -- observation -------------------------------------------------------
    def mounted_plugins(self) -> list[dict[str, Any]]:
        """Every composition row with its live loader state.

        Mirrors App.row_states(): each entry has id, plugin, state, config and
        the surrounding bookkeeping (critical, origin, error, group, parent,
        overridden, disabled, children). Rows that are disabled or waiting for
        a restart are included so inspectors can show the whole picture.
        Returns an empty list before the app attaches (plugins mounted before a
        boot completes see no rows rather than an error).
        """
        app = self._app
        if app is None:
            return []
        try:
            states = app.row_states()
        except Exception:  # pragma: no cover - a broken app must not break plugins
            log.exception("kernel runtime could not read app row states")
            return []
        return [dict(state) for state in states]

    # -- live configuration ------------------------------------------------
    def apply_plugin_config(self, plugin_id: str, config: dict[str, Any]) -> bool:
        """Apply a volatile-only config change to a mounted plugin.

        Args:
            plugin_id: row id of the already mounted plugin.
            config: full or partial config table to merge over the current one.

        Returns:
            True when the plugin now runs with the new config, False when the
            patch could not be applied live (unknown/unmounted plugin, no
            volatile fields, a non-volatile field changed, or invalid config).
            A mounted plugin whose patch needs a restart is marked
            restart-required.
        """
        if not isinstance(config, dict):
            log.warning("kernel: config for %r must be a mapping", plugin_id)
            return False
        app = self._app
        if app is None:
            log.warning("kernel: cannot apply config for %r before boot completes", plugin_id)
            return False
        record = app.mount_record(plugin_id)
        if record is None or record.fiber is None:
            log.warning("kernel: plugin %r is not mounted", plugin_id)
            return False
        row = record.row
        if row.group or row.disabled is True:
            log.warning("kernel: plugin %r is not a mounted plugin row", plugin_id)
            return False
        cls = record.cls
        if cls is None:
            return False
        volatile = tuple(getattr(cls, "volatile", ()) or ())
        if not volatile:
            self._mark_restart(app, plugin_id, "declares no volatile fields")
            return False
        instance = record.instance
        current = _config_dict(getattr(instance, "config", None) if instance is not None else record.config)
        changed = {
            key: value for key, value in config.items() if not _same(current.get(key, _MISSING), value)
        }
        if not changed:
            # Already running with these values: nothing to do, but the caller
            # may treat the config as applied.
            app.clear_restart_required(plugin_id)
            return True
        non_volatile = sorted(key for key in changed if key not in volatile)
        if non_volatile:
            self._mark_restart(
                app,
                plugin_id,
                "non-volatile field(s) changed: " + ", ".join(non_volatile),
            )
            return False
        merged = copy.deepcopy(current)
        for key in volatile:
            if key in config:
                merged[key] = copy.deepcopy(config[key])
        config_cls = getattr(cls, "Config", None)
        try:
            validated = config_cls.model_validate(merged) if config_cls is not None else merged
        except Exception as exc:  # noqa: BLE001 - user validators may raise anything
            log.warning("kernel: invalid config for %r: %s", plugin_id, exc)
            return False
        hook = getattr(instance, "on_config", None)
        if callable(hook):
            try:
                result = hook(validated)
            except Exception:
                log.exception("kernel: on_config hook failed for %r", plugin_id)
                return False
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                log.warning("kernel: on_config for %r must be synchronous", plugin_id)
                return False
        if instance is not None:
            instance.config = validated
            context = getattr(instance, "ctx", None)
            if context is not None:
                try:
                    context.config = validated
                except Exception:  # noqa: BLE001 - context without a config attribute
                    log.debug("kernel: context for %r rejected the live config", plugin_id)
        record.config = _config_dict(validated)
        app.clear_restart_required(plugin_id)
        return True

    def _mark_restart(self, app: Any, plugin_id: str, reason: str) -> None:
        app.mark_restart_required(plugin_id)
        log.info("kernel: plugin %r needs a restart to apply config (%s)", plugin_id, reason)


__all__ = ["KernelRuntime"]
