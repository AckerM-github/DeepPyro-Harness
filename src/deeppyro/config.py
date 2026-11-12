"""Configuration: providers, models, permissions, session storage."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from deeppyro import paths


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
    maxTokens: int | None = 4096
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
maxTokens = 4096
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
