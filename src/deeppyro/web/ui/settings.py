"""Settings backend for the local web UI: safe editing of the user config.

The web UI edits the same ~/.dpro/config.toml (or $DEEPPYRO_CONFIG) file the
native kernel reads. A plain TOML round-trip would drop the user's comments,
key order and unrelated plugin tables, so ConfigEditor edits the document as
text: it locates the target table and key, replaces exactly that line (keeping
indentation and a trailing comment), validates the rendered document through
Config.model_validate and only then atomically writes it, keeping numbered
.bak backups. Unknown tables are never touched, so they survive byte for byte;
absent known values or tables are inserted or appended.

API keys never leave the process: snapshot() only reports whether a key is
set, update_provider(api_key=None) leaves the stored key untouched, and probe
failures are scrubbed of every configured secret before they are returned.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import math
import os
import platform
import re
import shutil
import tempfile
import time
import tomllib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from deeppyro.builtin.commands import CommandsPlugin
from deeppyro.builtin.plugin_manager import PluginManagerPlugin
from deeppyro.builtin.sessions import SessionsPlugin
from deeppyro.builtin.settings import SettingsPlugin
from deeppyro.config import DEFAULT_TOML, Config
from deeppyro.core.errors import ConfigError
from deeppyro.paths import config_path, sessions_dir
from deeppyro.sandbox.permission import PermissionPresetsService
from deeppyro.sandbox.types import (
    DEFAULT_PERMISSION_PRESET,
    DEFAULT_PERMISSION_PRESETS,
    PRESETS,
)
from deeppyro.web.security import LOCALHOST

__all__ = ["ConfigEditor", "settings_payload", "test_provider"]

#: Longest probe error returned to a browser; longer messages are truncated.
_MAX_PROBE_DETAIL = 240

#: Sessions listed in the settings payload.
_MAX_SESSION_ITEMS = 20

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


# -- helpers: configuration text -----------------------------------------


def _read_config_text(path: Path) -> str:
    """Read a config file without newline translation so CRLF survives."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _split_lines(text: str) -> tuple[str, ...]:
    """Split only on LF, keeping CRLF terminators inside each line."""
    lines: list[str] = []
    start = 0
    while True:
        index = text.find("\n", start)
        if index < 0:
            if start < len(text):
                lines.append(text[start:])
            break
        lines.append(text[start : index + 1])
        start = index + 1
    return tuple(lines)


def _ending(line: str) -> tuple[str, str]:
    """Return (body without terminator, terminator) for one raw line."""
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    if line.endswith("\r"):
        return line[:-1], "\r"
    return line, ""


def _dominant_newline(lines: Sequence[str]) -> str:
    """First line terminator found in the document, LF when it is empty."""
    for line in lines:
        ending = _ending(line)[1]
        if ending:
            return ending
    return "\n"


def _trailing_comment(line: str) -> str:
    """Return the trailing # comment of a TOML line, string aware."""
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if quote:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in {'"', "'"}:
            quote = char
        elif char == "#":
            return line[index:]
    return ""


def _code_part(line: str) -> str:
    """Return one raw line without its terminator and trailing comment."""
    body = _ending(line)[0]
    comment = _trailing_comment(body)
    if comment:
        body = body[: len(body) - len(comment)]
    return body.rstrip()


def _unquote_key(text: str) -> str:
    """Turn a possibly quoted TOML key segment into its plain value."""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        try:
            parsed = tomllib.loads(f"key = {text}")
        except tomllib.TOMLDecodeError:
            return text[1:-1]
        return str(parsed.get("key", text[1:-1]))
    return text


def _split_key_path(text: str) -> tuple[str, ...]:
    """Split a dotted TOML table path, honouring quoted segments."""
    parts: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    for char in text:
        if quote:
            current.append(char)
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in {'"', "'"}:
            quote = char
            current.append(char)
        elif char == ".":
            parts.append(_unquote_key("".join(current).strip()))
            current = []
        else:
            current.append(char)
    parts.append(_unquote_key("".join(current).strip()))
    return tuple(part for part in parts if part)


def _table_path(line: str) -> tuple[str, ...] | None:
    """Return the table path declared by a header line, else None."""
    body = _code_part(line).strip()
    if not body.startswith("["):
        return None
    if body.startswith("[["):
        if not body.endswith("]]"):
            return None
        inner = body[2:-2].strip()
    else:
        if not body.endswith("]"):
            return None
        inner = body[1:-1].strip()
    if not inner:
        return None
    return _split_key_path(inner)


def _format_table_header(path: tuple[str, ...]) -> str:
    """Render a table header for keys that may need quoting."""
    rendered = ".".join(key if _BARE_KEY.fullmatch(key) else json.dumps(key) for key in path)
    return f"[{rendered}]"


def _find_table(lines: Sequence[str], path: tuple[str, ...]) -> tuple[int, int] | None:
    """Return (header index, exclusive end index) for a table, else None."""
    for index, line in enumerate(lines):
        if _table_path(line) != path:
            continue
        end = len(lines)
        for probe in range(index + 1, len(lines)):
            if _table_path(lines[probe]) is not None:
                end = probe
                break
        return index, end
    return None


def _replace_key_line(line: str, key: str, rendered: str) -> str:
    """Rewrite one key line, preserving its indentation and trailing comment."""
    body, ending = _ending(line)
    comment = _trailing_comment(body)
    content = body[: len(body) - len(comment)] if comment else body
    code = content.rstrip()
    suffix = body[len(code) :]
    indent = code[: len(code) - len(code.lstrip())]
    return f"{indent}{key} = {rendered}{suffix}{ending}"


def _append_table_value(
    lines: list[str],
    path: tuple[str, ...],
    key: str,
    rendered: str,
    newline: str,
) -> None:
    """Append a new table with one key at the end of the document."""
    if lines:
        if _ending(lines[-1])[1] == "":
            lines[-1] = lines[-1] + newline
        if lines[-1].strip():
            lines.append(newline)
    lines.append(f"{_format_table_header(path)}{newline}")
    lines.append(f"{key} = {rendered}{newline}")


def _set_table_value(
    lines: list[str],
    path: tuple[str, ...],
    key: str,
    rendered: str | None,
) -> tuple[list[str], bool]:
    """Set (rendered str) or remove (rendered None) one key inside a table."""
    section = _find_table(lines, path)
    if section is None:
        if rendered is None:
            return lines, False
        _append_table_value(lines, path, key, rendered, _dominant_newline(lines))
        return lines, True
    header, end = section
    matcher = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for index in range(header + 1, end):
        if not matcher.match(lines[index]):
            continue
        if rendered is None:
            del lines[index]
        else:
            lines[index] = _replace_key_line(lines[index], key, rendered)
        return lines, True
    if rendered is None:
        return lines, False
    insert_at = end
    while insert_at > header + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    newline = _ending(lines[header])[1] or _dominant_newline(lines)
    if insert_at > 0 and _ending(lines[insert_at - 1])[1] == "":
        lines[insert_at - 1] = lines[insert_at - 1] + newline
    lines.insert(insert_at, f"{key} = {rendered}{newline}")
    return lines, True


def _toml_string(value: str) -> str:
    """Render a TOML basic string with escaping (JSON is TOML compatible)."""
    return json.dumps(value, ensure_ascii=False)


def _backup_path(target: Path) -> Path:
    """First free numbered backup path next to the target config file."""
    candidate = target.with_name(f"{target.name}.bak")
    if not candidate.exists():
        return candidate
    for index in range(1, 1000):
        candidate = target.with_name(f"{target.name}.bak.{index}")
        if not candidate.exists():
            return candidate
    raise ConfigError(f"too many configuration backups next to {target}")


def _write_atomic(target: Path, text: str) -> None:
    """Write text through a same-directory temporary file and os.replace."""
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _parse_config(text: str, path: Path) -> Config:
    """Parse and strictly validate a rendered config document."""
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid config file {path}: {exc}") from exc
    try:
        return Config.model_validate(document)
    except ConfigError:
        raise
    except ValueError as exc:
        raise ConfigError(f"invalid configuration in {path}: {exc}") from exc


# -- helpers: values ------------------------------------------------------


def _positive_int(field: str, value: Any) -> str:
    """Validate a positive integer and render it for TOML."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field}: must be an integer")
    if value <= 0:
        raise ValueError(f"{field}: must be a positive integer")
    return str(value)


def _temperature(field: str, value: Any) -> str:
    """Validate a sampling temperature and render it as a TOML float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field}: must be a number")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 2.0:
        raise ValueError(f"{field}: must be between 0.0 and 2.0")
    return f"{number:.1f}"


# -- helpers: payload plumbing -------------------------------------------


def _get(source: Any, name: str) -> Any:
    """Read one attribute or mapping entry from an optional object."""
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _text(value: Any, default: str = "") -> str:
    """Coerce an optional value to a stripped string with a fallback."""
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _safe_call(service: Any, name: str, default: Any) -> Any:
    """Call one optional service method; fall back on any failure."""
    method = getattr(service, name, None)
    if not callable(method):
        return default
    try:
        return method()
    except Exception:  # noqa: BLE001 - optional service data is best effort
        return default


def _walk_instances(app: Any) -> Iterator[Any]:
    """Yield mounted plugin instances from app.mount_records when present."""
    for record in getattr(app, "mount_records", ()) or ():
        walk = getattr(record, "walk", None)
        records: Iterable[Any] = cast(Iterable[Any], walk()) if callable(walk) else (record,)
        for item in records:
            instance = getattr(item, "instance", None)
            if instance is not None:
                yield instance


def _service(app: Any, contract: type[Any], provide: str) -> Any:
    """Resolve a service through App.get, then through mount records."""
    getter = getattr(app, "get", None)
    if callable(getter):
        try:
            found = getter(contract)
        except Exception:  # noqa: BLE001 - App.get is typed and optional
            found = None
        if found is not None:
            return found
    for instance in _walk_instances(app):
        if isinstance(instance, contract) or getattr(instance, "provide", None) == provide:
            return instance
    return None


def _state_name(value: Any) -> str:
    """Render a plugin state value (enum or string) as plain text."""
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def _plugin_item(info: Any, states: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize one PluginInfo for the settings payload."""
    plugin_id = _text(_get(info, "id"))
    item: dict[str, Any] = {
        "id": plugin_id,
        "name": _text(_get(info, "name"), plugin_id),
        "version": _text(_get(info, "version")),
        "source": _text(_get(info, "source")),
        "origin": _text(_get(info, "origin")),
        "description": _text(_get(info, "description")),
        "enabled": bool(_get(info, "enabled")),
        "error": _text(_get(info, "error")),
        "provides": [_text(entry) for entry in (_get(info, "provides") or ()) if _text(entry)],
    }
    item["state"] = _state_name(states.get(plugin_id))
    return item


def _child_rows(row: Any) -> Iterator[Any]:
    """Yield nested plugin rows of a composition row."""
    for child in getattr(row, "children", ()) or ():
        yield child
        yield from _child_rows(child)


def _plugin_items(app: Any) -> list[dict[str, Any]]:
    """List installed plugins, preferring the plugin manager's PluginInfo."""
    started = getattr(app, "started", None)
    states: Mapping[str, Any] = started if isinstance(started, Mapping) else {}
    manager = _service(app, PluginManagerPlugin, "plugin_manager")
    if manager is not None:
        try:
            infos = list(manager.list())
        except Exception:  # noqa: BLE001 - plugin listing must not break settings
            infos = []
        if infos:
            return [_plugin_item(info, states) for info in infos]
    items: list[dict[str, Any]] = []
    for row in getattr(app, "rows", ()) or ():
        for entry in (row, *_child_rows(row)):
            plugin_id = _text(getattr(entry, "id", None))
            if not plugin_id:
                continue
            items.append(
                {
                    "id": plugin_id,
                    "name": _text(getattr(entry, "name", None), plugin_id),
                    "version": "",
                    "source": "manifest",
                    "origin": _text(getattr(entry, "origin", None)),
                    "description": "",
                    "enabled": not bool(getattr(entry, "disabled", False)),
                    "error": "",
                    "provides": [],
                    "state": _state_name(states.get(plugin_id)),
                }
            )
    return items


def _preset_item(spec: Any) -> dict[str, str]:
    """Serialize one PermissionPresetSpec (or compatible object)."""
    return {
        "name": _text(_get(spec, "name")),
        "sandbox": _text(_get(spec, "sandbox")),
        "approval": _text(_get(spec, "approval")),
        "description": _text(_get(spec, "description")),
    }


def _permissions_section(app: Any) -> dict[str, Any]:
    """Current permission preset plus the preset catalog."""
    service = _service(app, PermissionPresetsService, "permissionPresets")
    specs: Sequence[Any] = DEFAULT_PERMISSION_PRESETS
    current = DEFAULT_PERMISSION_PRESET
    default = DEFAULT_PERMISSION_PRESET
    custom = False
    if service is not None:
        try:
            specs = list(service.catalog())
        except Exception:  # noqa: BLE001 - fall back to the built-in catalog
            specs = DEFAULT_PERMISSION_PRESETS
        current = _text(_safe_call(service, "current", DEFAULT_PERMISSION_PRESET), DEFAULT_PERMISSION_PRESET)
        default = _text(_get(service, "default"), DEFAULT_PERMISSION_PRESET)
        custom = bool(_safe_call(service, "is_custom", False))
    return {
        "presets": [_preset_item(spec) for spec in specs],
        "current": current,
        "default": default,
        "custom": custom,
    }


def _commands_section(app: Any) -> list[dict[str, Any]]:
    """Registered slash commands, in registration order."""
    service = _service(app, CommandsPlugin, "commands")
    if service is None:
        return []
    try:
        specs = list(service.all())
    except Exception:  # noqa: BLE001 - command listing must not break settings
        return []
    return [
        {
            "name": _text(_get(spec, "name")),
            "summary": _text(_get(spec, "summary")),
            "usage": _text(_get(spec, "usage")),
            "aliases": [_text(alias) for alias in (_get(spec, "aliases") or ()) if _text(alias)],
            "plugin": _text(_get(spec, "plugin_id")),
        }
        for spec in specs
    ]


def _session_item(record: Any) -> dict[str, Any]:
    """Serialize one NativeSessionInfo for the settings payload."""
    return {
        "id": _text(_get(record, "id")),
        "title": _text(_get(record, "title")),
        "cwd": _text(_get(record, "cwd")),
        "turns": int(_get(record, "turns") or 0),
        "updated_at": int(_get(record, "updated_at") or 0),
        "preset": _text(_get(record, "preset")),
        "preview": _text(_get(record, "preview"))[:160],
    }


def _sessions_section(app: Any, service: Any) -> dict[str, Any]:
    """Recent sessions, the sessions directory and the active session."""
    records: list[Any] = []
    sessions = _service(app, SessionsPlugin, "sessions")
    if sessions is not None:
        try:
            records = list(sessions.native_list(include_archived=False))
        except Exception:  # noqa: BLE001 - session indexing is best effort
            records = []
    active = _get(service, "active_session_id")
    return {
        "dir": str(sessions_dir()),
        "count": len(records),
        "active_session_id": _text(active) or None,
        "items": [_session_item(record) for record in records[:_MAX_SESSION_ITEMS]],
    }


def _running_web_host(app: Any) -> Any:
    """Return the web plugin instance or its running WebHost when available."""
    for instance in _walk_instances(app):
        if getattr(instance, "provide", None) == "web":
            return getattr(instance, "host", None) or instance
    return None


def _web_section(app: Any, service: Any) -> dict[str, Any]:
    """Loopback bind address the web host is actually serving on."""
    host = _text(_get(service, "bound_host")) or _text(_get(service, "host"))
    port = _get(service, "port")
    running = _running_web_host(app)
    if running is not None:
        host = host or _text(_get(running, "bound_host")) or _text(_get(running, "host"))
        if not isinstance(port, int) or port <= 0:
            port = _get(running, "port")
        settings = _get(running, "settings")
        host = host or _text(_get(settings, "host"))
        if not isinstance(port, int) or port <= 0:
            port = _get(settings, "port")
    return {
        "host": host or LOCALHOST,
        "port": port if isinstance(port, int) and port > 0 else 0,
    }


# -- settings editor -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Document:
    """One loaded config document: raw text, raw lines and typed config."""

    text: str
    lines: tuple[str, ...]
    config: Config


class ConfigEditor:
    """Comment-preserving editor for the user configuration file.

    Every update method validates its arguments, re-renders only the affected
    key lines, validates the whole document through Config.model_validate and
    writes atomically. A numbered .bak copy next to the file is kept before
    every call that changes bytes; an update that renders exactly the current
    text is a no-op and does not touch the file.
    """

    def __init__(self, path: Path | None = None) -> None:
        """Edit path, or the kernel config path ($DEEPPYRO_CONFIG wins)."""
        self._path = Path(path).expanduser() if path is not None else config_path()

    @property
    def path(self) -> Path:
        """Path of the configuration file this editor reads and writes."""
        return self._path

    def snapshot(self) -> dict[str, Any]:
        """Read-only config view: no API key value ever leaves this method."""
        config = self._load().config
        return {
            "path": str(self._path),
            "agent": {
                "provider": config.agent.provider,
                "model": config.agent.model,
                "temperature": config.agent.temperature,
                "max_tokens": config.agent.max_tokens,
                "max_steps": config.agent.max_steps,
            },
            "providers": [
                {
                    "name": name,
                    "api": provider.api,
                    "base_url": provider.base_url,
                    "api_key_env": provider.api_key_env,
                    "api_key_set": bool(provider.resolve_api_key()),
                    "models": [
                        {
                            "id": model.id,
                            "name": model.name,
                            "context_window": model.context_window,
                        }
                        for model in provider.models
                    ],
                }
                for name, provider in config.providers.items()
            ],
        }

    def load(self) -> Config:
        """The validated configuration as currently written on disk.

        Read-only twin of :meth:`snapshot` for callers that need the real
        Config object (live-apply, permission defaults) rather than the
        redacted JSON view.
        """
        return self._load().config

    def update_agent(
        self,
        provider: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_steps: int | None = None,
    ) -> dict[str, Any]:
        """Update [agent] fields; None leaves a field unchanged."""
        document = self._load()
        updates: list[tuple[str, str]] = []
        if provider is not None:
            if not isinstance(provider, str) or not provider.strip():
                raise ValueError("agent.provider: must be a non-empty provider name")
            name = provider.strip()
            if name not in document.config.providers:
                raise ValueError(f"agent.provider: unknown provider {name!r}")
            updates.append(("provider", _toml_string(name)))
        if model is not None:
            if not isinstance(model, str) or not model.strip():
                raise ValueError("agent.model: must be a non-empty model id")
            updates.append(("model", _toml_string(model.strip())))
        if temperature is not None:
            updates.append(("temperature", _temperature("agent.temperature", temperature)))
        if max_tokens is not None:
            updates.append(("max_tokens", _positive_int("agent.max_tokens", max_tokens)))
        if max_steps is not None:
            updates.append(("max_steps", _positive_int("agent.max_steps", max_steps)))
        if not updates:
            return self.snapshot()
        lines = list(document.lines)
        for key, rendered in updates:
            _set_table_value(lines, ("agent",), key, rendered)
        return self._commit(document, lines)

    def update_provider(
        self,
        name: str,
        base_url: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
    ) -> dict[str, Any]:
        """Update one provider; None keeps a value, an empty string clears it."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("providers: a provider name is required")
        provider_name = name.strip()
        document = self._load()
        if provider_name not in document.config.providers:
            raise ValueError(f"providers.{provider_name}: unknown provider")
        updates: list[tuple[str, str | None]] = []
        field = f"providers.{provider_name}.base_url"
        if base_url is not None:
            if not isinstance(base_url, str) or not base_url.strip():
                raise ValueError(f"{field}: must be a non-empty http(s) URL")
            candidate = base_url.strip()
            parts = urlsplit(candidate)
            if parts.scheme not in {"http", "https"} or not parts.netloc:
                raise ValueError(f"{field}: must be an http(s) URL")
            updates.append(("base_url", _toml_string(candidate)))
        field = f"providers.{provider_name}.api_key"
        if api_key is not None:
            if not isinstance(api_key, str):
                raise ValueError(f"{field}: must be a string")
            updates.append(("api_key", _toml_string(api_key) if api_key else None))
        field = f"providers.{provider_name}.api_key_env"
        if api_key_env is not None:
            if not isinstance(api_key_env, str):
                raise ValueError(f"{field}: must be a string")
            env_name = api_key_env.strip()
            if api_key_env and not env_name:
                raise ValueError(f"{field}: must be an environment variable name")
            if env_name and not _ENV_NAME.fullmatch(env_name):
                raise ValueError(f"{field}: must be an environment variable name")
            updates.append(("api_key_env", _toml_string(env_name) if env_name else None))
        if not updates:
            return self.snapshot()
        if _find_table(document.lines, ("providers", provider_name)) is None:
            raise ConfigError(
                f"cannot locate [providers.{provider_name}] in {self._path}; "
                "the table spelling in the file cannot be addressed"
            )
        lines = list(document.lines)
        for key, rendered in updates:
            _set_table_value(lines, ("providers", provider_name), key, rendered)
        return self._commit(document, lines)

    def set_permission_default(self, preset: str) -> dict[str, Any]:
        """Set [permission] defaultPreset to one known preset name."""
        if not isinstance(preset, str) or preset.strip() not in PRESETS:
            raise ValueError(f"permission.defaultPreset: unknown preset {preset!r}")
        document = self._load()
        lines, _changed = _set_table_value(
            list(document.lines),
            ("permission",),
            "defaultPreset",
            _toml_string(preset.strip()),
        )
        return self._commit(document, lines)

    # -- internals -------------------------------------------------------

    def _read_text(self) -> str:
        """Current file text, or DEFAULT_TOML when the file does not exist."""
        try:
            return _read_config_text(self._path)
        except FileNotFoundError:
            return DEFAULT_TOML

    def _load(self) -> _Document:
        """Load and validate the current document (never creates the file)."""
        text = self._read_text()
        return _Document(text=text, lines=_split_lines(text), config=_parse_config(text, self._path))

    def _secret_values(self) -> tuple[str, ...]:
        """Configured secret values, longest first, for error scrubbing."""
        try:
            config = self._load().config
        except Exception:  # noqa: BLE001 - masking must never break the probe
            return ()
        secrets: list[str] = []
        for provider in config.providers.values():
            for candidate in (provider.api_key, provider.resolve_api_key()):
                if candidate and candidate not in secrets:
                    secrets.append(candidate)
        return tuple(sorted(secrets, key=len, reverse=True))

    def _commit(self, document: _Document, lines: list[str]) -> dict[str, Any]:
        """Validate, back up and atomically write a rendered document."""
        text = "".join(lines)
        if text == document.text:
            return self.snapshot()
        _parse_config(text, self._path)
        self._store(text)
        return self.snapshot()

    def _store(self, text: str) -> None:
        """Keep a numbered backup of an existing file, then write atomically."""
        target = self._path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.copyfile(target, _backup_path(target))
        _write_atomic(target, text)


# -- connection probe ----------------------------------------------------


async def test_provider(editor: ConfigEditor, provider: str, llm: Any) -> dict[str, Any]:
    """Probe one configured provider by listing its models.

    Returns {ok, detail, latency_ms}; every failure is reported as a short
    scrubbed string instead of an exception, and the configured API key is
    never echoed to the caller (including the browser).
    """
    started = time.perf_counter()
    try:
        if not isinstance(provider, str) or not provider.strip():
            return _probe_result(False, "provider name is required", started)
        name = provider.strip()
        known = {item["name"] for item in editor.snapshot()["providers"]}
        if name not in known:
            return _probe_result(False, f"unknown provider: {name}", started)
        response = llm.list_models(name, refresh=True)
        if inspect.isawaitable(response):
            response = await response
        models = list(response or ())
        return _probe_result(True, f"{len(models)} model(s) available", started)
    except Exception as exc:  # noqa: BLE001 - probe failures are reported, never raised
        return _probe_result(False, _masked_error(editor, exc), started)


def _probe_result(ok: bool, detail: str, started: float) -> dict[str, Any]:
    """Build the probe result with the measured round-trip latency."""
    elapsed = (time.perf_counter() - started) * 1000.0
    return {"ok": ok, "detail": detail, "latency_ms": round(elapsed, 2)}


def _masked_error(editor: ConfigEditor, exc: BaseException) -> str:
    """Short one-line error text with every configured secret scrubbed."""
    message = " ".join(f"{type(exc).__name__}: {exc}".split())
    for secret in editor._secret_values():
        message = message.replace(secret, "***")
    if len(message) > _MAX_PROBE_DETAIL:
        message = message[: _MAX_PROBE_DETAIL - 3] + "..."
    return message or type(exc).__name__


# -- GET /api/ui/settings payload ----------------------------------------


def settings_payload(app: Any, editor: ConfigEditor, service: Any, version: str) -> dict[str, Any]:
    """Build the JSON payload for GET /api/ui/settings.

    All service lookups are defensive: a missing plugin or session backend only
    removes that part of the payload, it never fails the whole settings page.
    """
    snapshot = editor.snapshot()
    agent = dict(snapshot["agent"])
    plugins = _plugin_items(app)
    settings_service = _service(app, SettingsPlugin, "settings")
    revision = ""
    if settings_service is not None:
        try:
            revision = str(settings_service.revision)
        except Exception:  # noqa: BLE001 - settings revision is best effort
            revision = ""
    return {
        "config": snapshot,
        "provider": {
            "current": agent["provider"],
            "model": agent["model"],
            "providers": list(snapshot["providers"]),
        },
        "agent": agent,
        "permissions": _permissions_section(app),
        "commands": _commands_section(app),
        "plugins": {
            "revision": revision,
            "items": plugins,
        },
        "sessions": _sessions_section(app, service),
        "web": _web_section(app, service),
        "about": {
            "version": str(version),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "plugins": len(plugins),
        },
    }
