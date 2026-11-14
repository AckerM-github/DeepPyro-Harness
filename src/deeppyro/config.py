"""Configuration: providers, models, permissions, session storage."""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from deeppyro import paths
from deeppyro.core.errors import ConfigError


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str | None = None
    contextWindow: int | None = None


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    api: str = "openai-completions"
    baseURL: str = "https://api.deepseek.com/v1"
    apiKeyEnv: str | None = None
    apiKey: str | None = None
    models: list[ModelConfig] = Field(default_factory=list)

    def resolve_api_key(self) -> str | None:
        if self.apiKey:
            return self.apiKey
        if self.apiKeyEnv:
            return os.environ.get(self.apiKeyEnv) or None
        return None


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    provider: str = "local"
    model: str = ""
    temperature: float | None = None
    # Output cap per request. Reasoning consumes this budget too, so a low value
    # truncates answers that think first (the dshinfer default is just 512).
    # Tool-heavy steps can emit several long tool calls in one assistant message;
    # 8192 leaves room for that on DeepSeek's 8k-output models.
    maxTokens: int | None = 8192
    maxSteps: int = 100
    permissionPreset: Literal["read-only", "workspace-write", "danger-full-access"] = "workspace-write"
    approvalPolicy: Literal["ask", "never", "always"] = "ask"


class Config(BaseModel):
    model_config = ConfigDict(extra="allow")

    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    sessionDir: Path | None = None

    def resolve(self) -> tuple[ProviderConfig, str]:
        provider = self.providers.get(self.agent.provider)
        if provider is None:
            raise KeyError(f"unknown provider: {self.agent.provider!r}")
        model = self.agent.model or (provider.models[0].id if provider.models else "")
        if not model:
            raise KeyError(f"no model configured for provider {self.agent.provider!r}")
        return provider, model


# User-scoped locations live under one home directory (DSH-style, ~/.dpro).
# DEEPPYRO_HOME overrides the root; DEEPPYRO_CONFIG and DEEPPYRO_SESSION_DIR
# override individual paths at call time.
DEFAULT_CONFIG_PATH = paths.default_config_path()
DEFAULT_SESSION_DIR = paths.default_sessions_dir()

DEFAULT_TOML = """
# deeppyro configuration
[agent]
provider = "local"
model = "deepseek-v4-pro-qwen3.5-9b"
maxTokens = 8192
permissionPreset = "workspace-write"
approvalPolicy = "ask"

[providers.local]
api = "openai-completions"
baseURL = "http://127.0.0.1:8000/v1"
apiKeyEnv = "DEEPPYRO_LOCAL_API_KEY"
models = [{ id = "deepseek-v4-pro-qwen3.5-9b", contextWindow = 32768 }]

[providers.deepseek]
api = "openai-completions"
baseURL = "https://api.deepseek.com/v1"
apiKeyEnv = "DEEPSEEK_API_KEY"
models = [{ id = "deepseek-chat", contextWindow = 65536 }]

# Programmatic tool calling. "native" exposes every tool directly; "ptc" exposes
# only run_code plus a generated Python SDK, and programs call the other tools
# through async bindings. See docs/spec/ptc.md.
[ptc]
mode = "native"
"""


def config_path() -> Path:
    return paths.config_path()


def session_dir(cfg: Config | None = None) -> Path:
    if cfg is not None and cfg.sessionDir is not None:
        return Path(cfg.sessionDir)
    return paths.sessions_dir()


def load_config(path: Path | None = None, create: bool = True) -> Config:
    p = path or config_path()
    if not p.exists():
        if not create:
            return Config()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(DEFAULT_TOML, encoding="utf-8")
    data: dict[str, Any] = tomllib.loads(p.read_text(encoding="utf-8"))
    return Config.model_validate(data)


_AGENT_HEADER = re.compile(r"^\s*\[\s*agent\s*\]\s*(?:#.*)?$")
_ANY_SECTION_HEADER = re.compile(r"^\s*\[")


def _toml_string(value: str) -> str:
    """Render a TOML basic string (JSON escaping is TOML-compatible here)."""
    return json.dumps(str(value), ensure_ascii=False)


def _trailing_comment(line: str) -> str:
    """Return the trailing comment of a TOML key/value line, if any."""
    in_basic = False
    in_literal = False
    escaped = False
    for index, char in enumerate(line):
        if in_basic:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_basic = False
        elif in_literal:
            if char == "'":
                in_literal = False
        elif char == '"':
            in_basic = True
        elif char == "'":
            in_literal = True
        elif char == "#":
            return line[index:].rstrip()
    return ""


def _set_agent_value(lines: list[str], key: str, value: str) -> list[str]:
    """Set one key inside the [agent] table, preserving every other line."""
    rendered = f"{key} = {_toml_string(value)}"
    header = next((index for index, line in enumerate(lines) if _AGENT_HEADER.match(line)), None)
    if header is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["[agent]", rendered])
        return lines
    end = len(lines)
    for index in range(header + 1, len(lines)):
        if _ANY_SECTION_HEADER.match(lines[index]):
            end = index
            break
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for index in range(header + 1, end):
        if key_pattern.match(lines[index]):
            indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
            comment = _trailing_comment(lines[index])
            lines[index] = indent + rendered + (f"  {comment}" if comment else "")
            return lines
    lines.insert(end, rendered)
    return lines


def save_agent_selection(provider: str, model: str, path: Path | None = None) -> Path:
    """Persist provider/model under [agent] without rewriting unrelated text.

    The update is line-oriented so comments and the rest of the file survive;
    the result is re-parsed with tomllib before an atomic replace. Returns the
    path that was written.
    """
    provider = (provider or "").strip()
    model = (model or "").strip()
    if not provider:
        raise ConfigError("provider name is required")
    if not model:
        raise ConfigError("model id is required")
    target = Path(path) if path is not None else config_path()
    text = target.read_text(encoding="utf-8") if target.exists() else DEFAULT_TOML
    lines = text.splitlines()
    lines = _set_agent_value(lines, "provider", provider)
    lines = _set_agent_value(lines, "model", model)
    rendered = "\n".join(lines).rstrip("\n") + "\n"
    try:
        data: dict[str, Any] = tomllib.loads(rendered)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid config file {target}: {exc}") from exc
    agent = data.get("agent")
    if not isinstance(agent, dict) or agent.get("provider") != provider or agent.get("model") != model:
        raise ConfigError(f"could not update [agent] provider/model in {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
            newline="\n",
        ) as handle:
            temporary = Path(handle.name)
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    return target


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_SESSION_DIR",
    "DEFAULT_TOML",
    "AgentConfig",
    "Config",
    "ModelConfig",
    "ProviderConfig",
    "config_path",
    "load_config",
    "save_agent_selection",
    "session_dir",
]
