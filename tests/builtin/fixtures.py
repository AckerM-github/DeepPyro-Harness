"""Shared helpers for the builtin Layer D tests."""

from __future__ import annotations

from deeppyro.config import Config, ModelConfig, ProviderConfig


def make_config() -> Config:
    """A valid config that never performs network calls during boot."""
    return Config(
        providers={
            "local": ProviderConfig(
                baseURL="http://127.0.0.1:8000/v1",
                models=[ModelConfig(id="m")],
            )
        },
        agent={"provider": "local", "model": "m"},
    )
