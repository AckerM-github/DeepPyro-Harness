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
        if getattr(entry, "id", None) == model_id and getattr(entry, "context_window", 0):
            return int(entry.context_window)
    if models and getattr(models[0], "context_window", 0):
        return int(models[0].context_window)
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


def _session_header_id(session: Any) -> str:
    """Session id from a native handle or a session-fold header."""
    session_id = str(getattr(session, "session_id", "") or "")
    if session_id:
        return session_id
    header = getattr(session, "header", None)
    return str(getattr(header, "id", "") or "")


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
