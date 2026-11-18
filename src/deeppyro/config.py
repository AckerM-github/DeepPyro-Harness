"""Configuration: providers, models, permissions, session storage."""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field

from deeppyro import paths
from deeppyro.core.errors import ConfigError

if TYPE_CHECKING:
    from deeppyro.api.capabilities import TokenCost


class ModelCapabilities(BaseModel):
    """Capability flags advertised by one model."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    tools: bool = True
    vision: bool = False
    reasoning: bool = False
    input_modalities: tuple[str, ...] = ()
    max_output_tokens: int | None = None


class ModelCost(BaseModel):
    """Price list for one model, in USD per million tokens."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0
    cached_input_per_mtok: float | None = None
    currency: str = "USD"


class ModelConfig(BaseModel):
    """One model declared inside a provider table.

    Canonical spelling is snake_case.  camelCase keys written by config files
    from before 0.2 are still accepted as validation aliases, so an existing
    ~/.dpro/config.toml keeps loading unchanged.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    name: str | None = None
    context_window: int | None = Field(default=None, alias="contextWindow")
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    cost: ModelCost = Field(default_factory=ModelCost)


class ProviderConfig(BaseModel):
    """One OpenAI-compatible (or Anthropic) provider endpoint."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    api: str = "openai-completions"
    base_url: str = Field(default="https://api.deepseek.com/v1", alias="baseURL")
    api_key_env: str | None = Field(default=None, alias="apiKeyEnv")
    api_key: str | None = Field(default=None, alias="apiKey")
    models: list[ModelConfig] = Field(default_factory=list)

    def resolve_api_key(self) -> str | None:
        """Resolve the API key from the inline value or its environment variable."""
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env) or None
        return None


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    provider: str = "local"
    model: str = ""
    temperature: float | None = None
    # Output cap per request. Reasoning consumes this budget too, so a low value
    # truncates answers that think first (the dshinfer default is just 512).
    # Tool-heavy steps can emit several long tool calls in one assistant message;
    # 8192 leaves room for that on DeepSeek's 8k-output models.
    max_tokens: int | None = Field(default=8192, alias="maxTokens")
    # Unbounded like DSH; set [agent].max_steps to cap a runaway loop.
    max_steps: int | None = Field(default=None, alias="maxSteps")


class Config(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    session_dir: Path | None = Field(default=None, alias="sessionDir")

    def resolve(self) -> tuple[ProviderConfig, str]:
        provider = self.providers.get(self.agent.provider)
        if provider is None:
            raise KeyError(f"unknown provider: {self.agent.provider!r}")
        model = self.agent.model or (provider.models[0].id if provider.models else "")
        if not model:
            raise KeyError(f"no model configured for provider {self.agent.provider!r}")
        return provider, model


class ModelProfile(BaseModel):
    """Capability, context and cost profile of one configured model.

    Profiles are the snake_case projection of a ModelConfig that governance and
    accounting consume.  The token/cost accounting service
    (deeppyro.builtin.usage) prices records through token_cost().
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    provider: str = ""
    name: str | None = None
    context_window: int | None = Field(default=None, alias="contextWindow")
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    cost: ModelCost = Field(default_factory=ModelCost)

    @classmethod
    def from_model_config(cls, model: ModelConfig, *, provider: str = "") -> ModelProfile:
        """Build a profile from one provider's model entry."""
        return cls(
            id=model.id,
            provider=provider,
            name=model.name,
            contextWindow=model.context_window,  # pydantic alias; populate_by_name also accepts context_window
            capabilities=model.capabilities,
            cost=model.cost,
        )

    def token_cost(self) -> TokenCost:
        """Return this profile's price list as an api.capabilities TokenCost."""
        from deeppyro.api.capabilities import TokenCost

        return TokenCost(
            input_per_mtok=self.cost.input_per_mtok,
            output_per_mtok=self.cost.output_per_mtok,
            cached_input_per_mtok=self.cost.cached_input_per_mtok,
        )

    def estimate_cost(
        self, *, input_tokens: int = 0, output_tokens: int = 0, cached_tokens: int = 0
    ) -> float:
        """Price one response with this model's cost metadata."""
        return self.token_cost().estimate(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
        )


class ConfigWarning(UserWarning):
    """A configuration key that is accepted today but is probably unintended."""


_STRICT_CONFIG_ENV = "DEEPPYRO_STRICT_CONFIG"
_STRICT_TRUTHY = frozenset({"1", "true", "yes", "on"})
_STRICT_KEYS = ("strict_config", "strictConfig")


def _strict_requested(document: Mapping[str, Any], strict: bool | None) -> bool:
    """Resolve the strict switch: explicit argument, environment, then the file.

    Strict mode rejects deprecated camelCase aliases and unknown keys inside the
    schema tables instead of warning about them.
    """
    if strict is not None:
        return bool(strict)
    env = os.environ.get(_STRICT_CONFIG_ENV)
    if env is not None and env.strip().lower() in _STRICT_TRUTHY:
        return True
    for key in _STRICT_KEYS:
        declared = document.get(key)
        if declared is None:
            continue
        if isinstance(declared, str):
            return declared.strip().lower() in _STRICT_TRUTHY
        return bool(declared)
    return False


def _dotted(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _model_type(annotation: Any) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _lint_value(annotation: Any, value: Any, path: str, issues: list[tuple[str, str]]) -> None:
    """Walk one value according to the declared annotation."""
    model = _model_type(annotation)
    if model is not None:
        if isinstance(value, Mapping):
            _lint_model(model, value, path, issues)
        return
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (list, tuple) and args and isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _lint_value(args[0], item, f"{path}[{index}]", issues)
        return
    if origin is dict and len(args) == 2 and isinstance(value, Mapping):
        for key, item in value.items():
            _lint_value(args[1], item, _dotted(path, str(key)), issues)


def _lint_model(
    model: type[BaseModel],
    data: Mapping[str, Any],
    path: str,
    issues: list[tuple[str, str]],
    *,
    allow_unknown: bool = False,
) -> None:
    aliases = {info.alias: name for name, info in model.model_fields.items() if info.alias}
    for key, value in data.items():
        info = model.model_fields.get(key)
        field_path = _dotted(path, key)
        if info is None:
            canonical = aliases.get(key)
            if canonical is None:
                if allow_unknown:
                    continue
                issues.append(
                    (
                        "unknown",
                        f"unknown configuration key {field_path!r} for {model.__name__}: "
                        "it is accepted today but ignored by the schema",
                    )
                )
                continue
            issues.append(
                (
                    "alias",
                    f"configuration key {field_path!r} is a deprecated camelCase alias; "
                    f"rename it to {_dotted(path, canonical)!r}",
                )
            )
            info = model.model_fields[canonical]
        _lint_value(info.annotation, value, field_path, issues)


def lint_config(document: Mapping[str, Any], *, strict: bool | None = None) -> list[str]:
    """Check a parsed config document for deprecated aliases and unknown keys.

    camelCase aliases are reported with a :class:`DeprecationWarning` that names
    the full dotted path and the suggested snake_case spelling; they keep working
    until 0.3.  Unknown keys inside the schema tables (`[agent]`,
    `[providers.*]`, model capability/cost tables) are reported with
    :class:`ConfigWarning`, because pydantic would otherwise swallow the typo.

    Strict mode raises :class:`ConfigError` listing every issue instead of
    warning.  Strictness resolves from the explicit *strict* argument, then the
    `DEEPPYRO_STRICT_CONFIG` environment variable, then a top-level
    `strict_config` key in the document.

    Top-level tables the schema does not know (plugin configuration such as
    `[sandbox]`, `[retry]` or `[[mcp.servers]]`) are legitimate plugin
    configuration and are never reported.  Returns the issue messages.
    """
    issues: list[tuple[str, str]] = []
    _lint_model(Config, document, "", issues, allow_unknown=True)
    messages = [message for _kind, message in issues]
    if _strict_requested(document, strict):
        if messages:
            raise ConfigError("invalid configuration: " + "; ".join(messages))
        return []
    for kind, message in issues:
        category = DeprecationWarning if kind == "alias" else ConfigWarning
        warnings.warn(message, category, stacklevel=2)
    return messages


# User-scoped locations live under one home directory (DSH-style, ~/.dpro).
# DEEPPYRO_HOME overrides the root; DEEPPYRO_CONFIG and DEEPPYRO_SESSION_DIR
# override individual paths at call time.
DEFAULT_CONFIG_PATH = paths.default_config_path()
DEFAULT_SESSION_DIR = paths.default_sessions_dir()

DEFAULT_TOML = """
# deeppyro configuration
# Canonical keys are snake_case; camelCase spellings from older config files
# (baseURL, maxTokens, contextWindow, ...) are still accepted when reading.
[agent]
provider = "local"
model = "deepseek-v4-pro-qwen3.5-9b"
max_tokens = 8192

# Standing sandbox policy. The mode is one of read-only, workspace-write and
# danger-full-access. See docs/spec/sandbox.md.
[sandbox]
mode = "workspace-write"
additionalWritableRoots = []

[providers.local]
api = "openai-completions"
base_url = "http://127.0.0.1:8000/v1"
api_key_env = "DEEPPYRO_LOCAL_API_KEY"
models = [{ id = "deepseek-v4-pro-qwen3.5-9b", context_window = 32768 }]

[providers.deepseek]
api = "openai-completions"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"
models = [{ id = "deepseek-chat", context_window = 65536 }]

# Programmatic tool calling. "native" exposes every tool directly; "ptc" exposes
# only run_code plus a generated Python SDK, and programs call the other tools
# through async bindings. See docs/spec/ptc.md.
[ptc]
mode = "native"

# Retry policy for failed provider requests. See docs/spec/llm.md and
# src/deeppyro/builtin/llm_retry.py. "normal" retries only retryable failures
# within maxRetries; "always" retries every failure until the turn is cancelled.
[retry]
mode = "normal"
maxRetries = 5
initialDelayMs = 500
maxDelayMs = 10000
jitterRatio = 0.1

# Text appended to the assembled system prompt by the prompt-extension plugin
# (src/deeppyro/builtin/prompt_extension.py). Empty text keeps the prompt
# unchanged.
[prompt]
extension = ""

# Approval policy for one-shot approvals: "ask" asks an answerer and fails
# closed when none is available, "never" rejects every approval request.
# hookMode/hookTools are consumed by the approval-policy plugin
# (src/deeppyro/builtin/approval_policy.py): "off"/"ask" defer to policy,
# "allow"/"deny" answer approval/request directly for the tools in hookTools
# (an empty list means every tool).
[approval]
policy = "ask"
hookMode = "off"
hookTools = []

# Named sandbox+approval bundles offered by /permission. See
# docs/spec/sandbox.md.
# Session titles: a short fallback is derived locally, then upgraded once by a
# single small request after the first turn. Set enabled = false to skip the
# request (and keep the fallback only).
[title]
enabled = true
maxChars = 24
model = ""

# [[mcp.servers]]
# serverName = "github"                  # [A-Za-z0-9_-]{1,32}, unique
# transport = "stdio"                    # or "streamable-http"
# command = "npx"
# args = ["-y", "@modelcontextprotocol/server-github"]
# cwd = ""                               # stdio working directory
# env = { GITHUB_TOKEN = "..." }         # merged over a scrubbed environment
# url = "https://example.invalid/mcp"    # transport = "streamable-http"
# headers = { Authorization = "Bearer ..." }
# enabled = true
# toolCallTimeoutMs = 60000
# maxInstructionBytes = 32768
# failOnStartupError = false
# [mcp.servers.reconnect]
# enabled = true
# initialDelayMs = 500
# maxDelayMs = 30000
# maxAttempts = 10

[permission]
defaultPreset = "workspace-write"
"""


def config_path() -> Path:
    return paths.config_path()


def session_dir(cfg: Config | None = None) -> Path:
    if cfg is not None and cfg.session_dir is not None:
        return Path(cfg.session_dir)
    return paths.sessions_dir()


def load_config(
    path: Path | None = None,
    create: bool = True,
    *,
    strict: bool | None = None,
) -> Config:
    """Load the configuration file, linting deprecated aliases and unknown keys.

    *strict* overrides the `DEEPPYRO_STRICT_CONFIG` environment variable and the
    file's top-level `strict_config` key; see :func:`lint_config`.
    """
    p = path or config_path()
    if not p.exists():
        if not create:
            return Config()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(DEFAULT_TOML, encoding="utf-8")
    try:
        data: dict[str, Any] = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid config file {p}: {exc}") from exc
    lint_config(data, strict=strict)
    for key in _STRICT_KEYS:
        data.pop(key, None)
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
    "ConfigWarning",
    "ModelCapabilities",
    "ModelConfig",
    "ModelCost",
    "ModelProfile",
    "ProviderConfig",
    "config_path",
    "lint_config",
    "load_config",
    "save_agent_selection",
    "session_dir",
]
