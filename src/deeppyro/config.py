"""Configuration: providers, models, permissions, session storage.

The schema is strict (KI-013): keys are snake_case only, and every key inside a
schema table must be declared there.  Configuration files from 0.1 with
camelCase spellings fail with :class:`~deeppyro.core.errors.ConfigError` naming
the offending key and its snake_case replacement; see
docs/guides/config-migration.md for the mapping table.

Two boundaries stay open because their shape belongs to somebody else:

* the top level of :class:`Config` keeps undeclared tables as plugin
  configuration ([sandbox], [retry], [[mcp.servers]], ...); the plugin that owns
  the table reads and validates it itself;
* :class:`ProviderConfig` and :class:`ModelConfig` accept only the documented
  adapter options in _allowed_extra_keys; the provider/catalog layer reads them
  from model_extra (docs/spec/llm.md).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deeppyro import paths
from deeppyro.core.errors import ConfigError

if TYPE_CHECKING:
    from deeppyro.api.capabilities import TokenCost


#: camelCase spellings removed in 0.2.0, mapped to the snake_case key that replaced
#: them. A config file still using one of these fails loudly with the replacement
#: instead of silently configuring nothing.
_REMOVED_ALIASES: dict[str, str] = {
    "contextWindow": "context_window",
    "baseURL": "base_url",
    "apiKeyEnv": "api_key_env",
    "apiKey": "api_key",
    "maxTokens": "max_tokens",
    "maxSteps": "max_steps",
    "sessionDir": "session_dir",
}

#: camelCase spellings of the adapter options that 0.1 read from model_extra.
#: The snake_case option names are the only documented spelling now.
_ADAPTER_ALIASES: dict[str, str] = {
    "reasoningEffort": "reasoning_effort",
    "inputModalities": "input_modalities",
    "maxOutputTokens": "max_tokens",
    "maxCompletionTokens": "max_tokens",
}

#: Top-level keys retired in 0.2.0: validation is always strict, so there is
#: nothing left for these switches to control.
_REMOVED_SWITCHES: frozenset[str] = frozenset({"strict_config", "strictConfig"})


def _dotted(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


class _SchemaModel(BaseModel):
    """Base for config tables: snake_case keys and no undeclared fields.

    The only open boundaries are documented by the class variables below; every
    other undeclared key is a typo the schema refuses to swallow.
    """

    model_config = ConfigDict(extra="forbid")

    #: Adapter options the provider layer reads from model_extra.
    _allowed_extra_keys: ClassVar[frozenset[str]] = frozenset()
    #: Whether undeclared keys are plugin configuration (top-level Config only).
    _allow_unknown_keys: ClassVar[bool] = False
    #: Whether the removed top-level switch keys (strict_config/strictConfig) are
    #: reported for this model; only the top-level Config table declares them.
    _report_removed_switches: ClassVar[bool] = False

    @classmethod
    def _pre_validate(cls, data: Any) -> Any:
        """Hook for the top-level table: reject every issue with its dotted path."""
        if isinstance(data, Mapping):
            for key in data:
                if isinstance(key, str):
                    issue = _key_issue(cls, key, "")
                    if issue is not None:
                        raise ConfigError(issue)
        return data

    @model_validator(mode="before")
    @classmethod
    def _reject_undeclared_keys(cls, data: Any) -> Any:
        return cls._pre_validate(data)


def _declared_keys(model: type[_SchemaModel]) -> frozenset[str]:
    return frozenset(model.model_fields) | model._allowed_extra_keys


def _key_issue(model: type[_SchemaModel], key: str, path: str) -> str | None:
    """Return the error message for one key of a schema table, or None if valid."""
    field_path = _dotted(path, key)
    replacement = _REMOVED_ALIASES.get(key) or _ADAPTER_ALIASES.get(key)
    if replacement is not None:
        return f"configuration key {field_path!r} is not supported; use {_dotted(path, replacement)!r}"
    if key in _REMOVED_SWITCHES and model._report_removed_switches:
        return (
            f"configuration key {field_path!r} is no longer supported: "
            "configuration validation is always strict as of 0.2.0; remove it"
        )
    if key in model.model_fields or key in model._allowed_extra_keys:
        return None
    if model._allow_unknown_keys:
        return None
    return f"unknown configuration key {field_path!r} for {model.__name__}"


class ModelCapabilities(_SchemaModel):
    """Capability flags advertised by one model."""

    tools: bool = True
    vision: bool = False
    reasoning: bool = False
    input_modalities: tuple[str, ...] = ()
    max_output_tokens: int | None = None


class ModelCost(_SchemaModel):
    """Price list for one model, in USD per million tokens."""

    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0
    cached_input_per_mtok: float | None = None
    currency: str = "USD"


class ModelConfig(_SchemaModel):
    """One model declared inside a provider table.

    Keys are snake_case only; 0.1's camelCase spellings (contextWindow,
    maxTokens, inputModalities, ...) are rejected as of 0.2.0 with the
    snake_case replacement.  The provider/catalog layer reads three
    documented adapter options from model_extra (docs/spec/llm.md): max_tokens,
    reasoning_effort and input_modalities.
    """

    model_config = ConfigDict(extra="allow")

    _allowed_extra_keys: ClassVar[frozenset[str]] = frozenset(
        {"max_tokens", "reasoning_effort", "input_modalities"}
    )

    id: str
    name: str | None = None
    context_window: int | None = None
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    cost: ModelCost = Field(default_factory=ModelCost)


class ProviderConfig(_SchemaModel):
    """One OpenAI-compatible (or Anthropic) provider endpoint.

    Keys are snake_case only; baseURL/apiKeyEnv/apiKey are rejected as of 0.2.0.
    The documented adapter options max_tokens and reasoning_effort are accepted
    as model_extra so a provider can set defaults for all of its models.
    """

    model_config = ConfigDict(extra="allow")

    _allowed_extra_keys: ClassVar[frozenset[str]] = frozenset({"max_tokens", "reasoning_effort"})

    api: str = "openai-completions"
    base_url: str = "https://api.deepseek.com/v1"
    api_key_env: str | None = None
    api_key: str | None = None
    models: list[ModelConfig] = Field(default_factory=list)

    def resolve_api_key(self) -> str | None:
        """Resolve the API key from the inline value or its environment variable."""
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env) or None
        return None


class AgentConfig(_SchemaModel):
    """The [agent] table: provider/model selection and per-call limits."""

    provider: str = "local"
    model: str = ""
    temperature: float | None = None
    # Output cap per request. Reasoning consumes this budget too, so a low value
    # truncates answers that think first (the dshinfer default is just 512).
    # Tool-heavy steps can emit several long tool calls in one assistant message;
    # 8192 leaves room for that on DeepSeek's 8k-output models.
    max_tokens: int | None = 8192
    # Unbounded like DSH; set [agent].max_steps to cap a runaway loop.
    max_steps: int | None = None


class Config(_SchemaModel):
    """The top-level configuration document.

    Undeclared top-level tables are plugin configuration ([sandbox], [retry],
    [[mcp.servers]], [permission], ...) and are kept as model_extra for the
    owning plugin to read.  Everything inside the schema tables is strict.
    """

    model_config = ConfigDict(extra="allow")

    _allow_unknown_keys: ClassVar[bool] = True
    _report_removed_switches: ClassVar[bool] = True

    @classmethod
    def _pre_validate(cls, data: Any) -> Any:
        """Collect every issue in the document so errors carry dotted paths."""
        if isinstance(data, Mapping):
            issues = _collect_issues(data)
            if issues:
                raise ConfigError("invalid configuration: " + "; ".join(issues))
        return data

    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    session_dir: Path | None = None

    def resolve(self) -> tuple[ProviderConfig, str]:
        provider = self.providers.get(self.agent.provider)
        if provider is None:
            raise KeyError(f"unknown provider: {self.agent.provider!r}")
        model = self.agent.model or (provider.models[0].id if provider.models else "")
        if not model:
            raise KeyError(f"no model configured for provider {self.agent.provider!r}")
        return provider, model


class ModelProfile(_SchemaModel):
    """Capability, context and cost profile of one configured model.

    Profiles are the snake_case projection of a ModelConfig that governance and
    accounting consume.  The token/cost accounting service
    (deeppyro.builtin.usage) prices records through token_cost().
    """

    id: str
    provider: str = ""
    name: str | None = None
    context_window: int | None = None
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    cost: ModelCost = Field(default_factory=ModelCost)

    @classmethod
    def from_model_config(cls, model: ModelConfig, *, provider: str = "") -> ModelProfile:
        """Build a profile from one provider's model entry."""
        return cls(
            id=model.id,
            provider=provider,
            name=model.name,
            context_window=model.context_window,
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
    """A configuration problem reported as a warning instead of raised.

    Config loading is always strict as of 0.2.0, so this category is only used by
    preset model validation (deeppyro.builtin.presets).
    """


_STRICT_CONFIG_ENV = "DEEPPYRO_STRICT_CONFIG"
_STRICT_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _strict_requested(strict: bool | None = None) -> bool:
    """Resolve the preset-model strict switch: explicit argument, then the environment.

    Preset model validation warns when this is false and raises ConfigError when
    it is true.  Config loading itself is always strict as of 0.2.0 and never
    consults this switch (see :func:`load_config`).  The removed
    strict_config/strictConfig keys are rejected before this point, so only the
    environment remains as a file-independent switch.
    """
    if strict is not None:
        return bool(strict)
    env = os.environ.get(_STRICT_CONFIG_ENV)
    return env is not None and env.strip().lower() in _STRICT_TRUTHY


def _model_type(annotation: Any) -> type[_SchemaModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, _SchemaModel):
        return annotation
    return None


def _lint_value(annotation: Any, value: Any, path: str, issues: list[str]) -> None:
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


def _lint_model(model: type[_SchemaModel], data: Mapping[str, Any], path: str, issues: list[str]) -> None:
    for key, value in data.items():
        if not isinstance(key, str):
            continue
        issue = _key_issue(model, key, path)
        if issue is not None:
            issues.append(issue)
            continue
        info = model.model_fields.get(key)
        if info is not None:
            _lint_value(info.annotation, value, _dotted(path, key), issues)


def _collect_issues(document: Mapping[str, Any]) -> list[str]:
    """Return every schema problem in a parsed config document, with dotted paths."""
    issues: list[str] = []
    _lint_model(Config, document, "", issues)
    return issues


def lint_config(document: Mapping[str, Any]) -> list[str]:
    """Report the schema problems a parsed config document would raise.

    Removed camelCase spellings, unknown keys inside the schema tables and keys
    outside each table's allowed adapter options are listed with their dotted
    path and (for camelCase) the snake_case replacement.  Top-level tables the
    schema does not know (plugin configuration such as [sandbox], [retry] or
    [[mcp.servers]]) are legitimate plugin configuration and are never reported.

    This helper only reports; callers that want the error use :func:`load_config`
    or Config.model_validate, which always validate strictly.
    """
    return _collect_issues(document)


# User-scoped locations live under one home directory (DSH-style, ~/.dpro).
# DEEPPYRO_HOME overrides the root; DEEPPYRO_CONFIG and DEEPPYRO_SESSION_DIR
# override individual paths at call time.
DEFAULT_CONFIG_PATH = paths.default_config_path()
DEFAULT_SESSION_DIR = paths.default_sessions_dir()

DEFAULT_TOML = """
# deeppyro configuration
# Keys in schema tables ([agent], [providers.*], model entries) are snake_case.
# 0.1 camelCase spellings (baseURL, maxTokens, contextWindow, ...) are rejected
# as of 0.2.0: loading fails and names the snake_case replacement. Unknown keys in
# those tables fail too. Plugin sections below keep their own option names.
# Migration guide: docs/guides/config-migration.md

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


def load_config(path: Path | None = None, create: bool = True) -> Config:
    """Load the configuration file, which is always validated strictly.

    Removed camelCase aliases, unknown keys inside the schema tables and keys
    outside each table's allowed adapter options raise ConfigError naming the
    dotted path and (for camelCase) the snake_case replacement.  Top-level plugin
    tables are kept as plugin configuration, not validated here.
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
    issues = _collect_issues(data)
    if issues:
        raise ConfigError("invalid configuration: " + "; ".join(issues))
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
