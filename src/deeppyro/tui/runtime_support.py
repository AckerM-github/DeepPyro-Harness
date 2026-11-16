"""Module-level helpers for the TUI runtime: workspace and context lookup, plus recorded selections."""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Any

from deeppyro.config import Config
from deeppyro.core.errors import ConfigError

logger = logging.getLogger("deeppyro.tui.runtime")


DEFAULT_CONTEXT_WINDOW = 32768


def resolve_workspace(path: str | Path | None = None) -> Path:
    """Resolve the workspace directory, rejecting missing paths."""
    workspace = Path(path).expanduser() if path else Path.cwd()
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise ConfigError(f"workspace directory does not exist: {workspace}")
    return workspace


def _context_window(provider_cfg: Any, model_id: str) -> int:
    """Pick the model context window, falling back to the provider default."""
    models = list(getattr(provider_cfg, "models", []) or [])
    for entry in models:
        if getattr(entry, "id", None) == model_id and getattr(entry, "contextWindow", 0):
            return int(entry.contextWindow)
    if models and getattr(models[0], "contextWindow", 0):
        return int(models[0].contextWindow)
    return DEFAULT_CONTEXT_WINDOW


def _context_window_for(provider_cfg: Any, model_id: str, info: Any = None) -> int:
    """Context window for a model, honoring a catalog entry for discovered ids.

    Configured models keep the configuration answer exactly as before; only a
    model that config does not declare falls back to the catalog window and
    finally to DEFAULT_CONTEXT_WINDOW (no borrowed provider default, matching
    the headless resolver).
    """
    configured = [str(getattr(entry, "id", "")) for entry in list(getattr(provider_cfg, "models", []) or [])]
    if model_id in configured:
        return _context_window(provider_cfg, model_id)
    window = getattr(info, "context_window", None) if info is not None else None
    if window is not None and int(window) > 0:
        return int(window)
    return DEFAULT_CONTEXT_WINDOW


async def await_maybe(value: Any) -> Any:
    """Await an awaitable or return a plain value (test doubles)."""
    if inspect.isawaitable(value):
        return await value
    return value


def _recorded_preset(session: Any) -> tuple[str, int | None, str | None] | None:
    """Last agent-preset/selected record: (preset, revision, previous).

    The header only carries the preset a session was created with, so the
    record is what makes a later switch (including /team on) survive resume.
    """
    records = getattr(session, "records", None) or []
    for record in reversed(list(records)):
        if getattr(record, "type", "") != "agent-preset/selected":
            continue
        data = getattr(record, "data", None)
        if not isinstance(data, dict):
            return None
        preset = data.get("preset")
        revision = data.get("revision")
        previous = data.get("previous")
        return (
            preset if isinstance(preset, str) else "",
            revision if isinstance(revision, int) else None,
            previous if isinstance(previous, str) and previous else None,
        )
    return None


def _recorded_model_selection(session: Any) -> tuple[str, str] | None:
    """The last model/selection written to the session, if still readable."""
    records = getattr(session, "records", None) or []
    for record in reversed(list(records)):
        if getattr(record, "type", "") != "model/selection":
            continue
        data = getattr(record, "data", None)
        if isinstance(data, dict):
            provider = data.get("provider")
            model = data.get("model")
            if isinstance(provider, str) and isinstance(model, str) and provider and model:
                return provider, model
        return None
    return None


def _resolve_model(cfg: Config, provider_name: str, *, model: str | None) -> str:
    """Resolve the model from an override, the configured provider, or its default.

    The [agent] model belongs to the configured provider: when another provider
    is selected explicitly (--provider or /provider) its own default is used
    instead of leaking the configured model onto a different endpoint.
    """
    provider_cfg = cfg.providers[provider_name]
    available = [str(entry.id) for entry in list(provider_cfg.models or [])]
    if model:
        if available and model not in available:
            raise ConfigError(
                f"unknown model: {model} (provider {provider_name} offers {', '.join(available)})"
            )
        return model
    if provider_name == cfg.agent.provider and cfg.agent.model:
        return cfg.agent.model
    if available:
        return available[0]
    if provider_name == cfg.agent.provider:
        try:
            _, resolved = cfg.resolve()
        except KeyError as exc:
            raise ConfigError(f"unknown provider: {exc.args[0]}") from exc
        return resolved
    return ""
