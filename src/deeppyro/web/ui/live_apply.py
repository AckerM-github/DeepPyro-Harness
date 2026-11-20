"""Push a saved config.toml reload into the running kernel.

:class:`~deeppyro.web.ui.settings.ConfigEditor` writes config.toml atomically,
but the kernel resolves its Config once at boot: plugins copy
``KernelConfig.value`` into their own attributes, the LLM registry keeps the
provider objects (and their HTTP clients) it created, and a native agent host
keeps the provider/model/limits it was built with.  Writing the file alone
therefore does not change the next turn -- the pre-NiceGUI bug where a save
"succeeded" but had no effect.

``apply_settings_live`` bridges that gap after a PATCH: it updates the live
kernel Config, the LLM registry (closing transports whose provider definition
changed), the agent defaults and the permission default, then asks the web
session host to retarget the attached session.  The returned dict states what
actually changed (``applied``), whether the update is deferred until the next
turn (``deferred``), and what would require a restart
(``restart_required``) -- no optimistic "ok" when nothing could be applied.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from deeppyro.config import Config
from deeppyro.kernel.config_service import KernelConfig
from deeppyro.sandbox.types import DEFAULT_PERMISSION_PRESET

__all__ = ["apply_settings_live", "permission_default"]


def permission_default(config: Config) -> str:
    """The ``[permission]`` defaultPreset of a loaded Config, with a fallback."""
    table = getattr(config, "permission", None)
    value = table.get("defaultPreset") if isinstance(table, Mapping) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_PERMISSION_PRESET


async def apply_settings_live(
    *,
    app: Any | None,
    service: Any | None,
    config: Config,
    changed: Iterable[str],
    providers: Iterable[str] = (),
    previous_permission_default: str | None = None,
) -> dict[str, Any]:
    """Apply the reloaded Config to the running kernel and the web host.

    ``app`` is the mounted kernel (or None on headless surfaces); ``service``
    is the WebSessionHost.  ``changed`` names the config keys the editor just
    wrote (dotted, e.g. ``agent.model``, ``provider``,
    ``permission.defaultPreset``).  ``providers`` names provider tables that
    may need their cached transport rebuilt.

    Raises whatever the runtime raises; the HTTP layer turns that into a
    ``live`` failure report instead of a fake success.
    """
    changes = set(changed)
    provider_names = tuple(providers)
    applied: list[str] = []
    notes: list[str] = []

    if app is not None:
        try:
            kernel = app.get(KernelConfig)
        except Exception:  # noqa: BLE001 - an unreadable kernel config is not fatal
            kernel = None
        if kernel is not None:
            kernel.value = config
        app.config = config
        applied.append("config")
    else:
        notes.append("no running kernel on this surface")

    bundles = getattr(service, "services", None)
    if bundles is not None:
        llm = getattr(bundles, "llm", None)
        if llm is not None:
            stale = await llm.apply_config(config)
            applied.append("llm")
            if stale:
                notes.append("rebuilt provider transport: " + ", ".join(sorted(stale)))
            elif provider_names:
                notes.append("provider transport unchanged: " + ", ".join(provider_names))
        agents = getattr(bundles, "agents", None)
        if agents is not None:
            agents.apply_config(config)
            applied.append("agents")
        presets = getattr(bundles, "permission_presets", None)
        default = permission_default(config)
        if (
            presets is not None
            and previous_permission_default is not None
            and default != previous_permission_default
        ):
            presets.set_default(default)
            applied.append("permissionPresets")
            changes.add("permission.defaultPreset")

    deferred = False
    if service is not None and hasattr(service, "apply_runtime_config"):
        outcome = await service.apply_runtime_config(
            config,
            changed=changes,
            previous_permission_default=previous_permission_default,
        )
        for name in outcome.get("applied", ()):
            if name not in applied:
                applied.append(name)
        deferred = bool(outcome.get("deferred", False))
        if outcome.get("detail"):
            notes.append(str(outcome["detail"]))

    parts = ["agent, provider and permission settings apply to the next turn"]
    if deferred:
        parts = ["the active turn is still streaming; the new settings apply before the next turn"]
    if notes:
        parts.append("; ".join(notes))
    return {
        "applied": applied,
        "restart_required": [],
        "deferred": deferred,
        "detail": "; ".join(parts),
    }
