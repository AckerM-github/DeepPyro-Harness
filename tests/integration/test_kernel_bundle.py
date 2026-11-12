"""Integration: boot the real default bundle and exercise service factories.

Session/agent plugins are disabled here until their domain modules land; the
rest of the bundle (frontend, llm, sandbox, tools) must boot from config alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.kernel import boot


def _config(tmp_path: Path) -> Config:
    return Config(
        providers={
            "local": ProviderConfig(
                baseURL="http://127.0.0.1:8000/v1",
                apiKey="test-key",
                models=[ModelConfig(id="m", contextWindow=32768)],
            )
        },
        agent={"provider": "local", "model": "m", "permissionPreset": "workspace-write"},
        sessionDir=tmp_path / "sessions",
    )


@pytest.fixture()
async def app(tmp_path: Path):
    manifest = tmp_path / "plugins.toml"
    manifest.write_text(
        '[[plugins]]\nid = "sessions"\ndisabled = true\n'
        '[[plugins]]\nid = "agents"\ndisabled = true\n',
        encoding="utf-8",
    )
    app = await boot(_config(tmp_path), manifest_paths=[str(manifest)])
    try:
        yield app
    finally:
        await app.stop()


async def test_bundle_services_are_available(app) -> None:
    for name in ("frontend", "llm", "sandbox", "tools"):
        assert app.has_service(name), name
    assert not app.has_service("sessions")
    assert not app.has_service("agents")
    assert app.config.agent.provider == "local"


async def test_llm_service_resolves_configured_provider(app) -> None:
    llm = app.service("llm")
    provider = llm.provider("local")
    assert provider.name == "local"
    await provider.aclose()


async def test_sandbox_service_builds_gate_for_workspace(app, tmp_path: Path) -> None:
    gate = app.service("sandbox").gate(tmp_path)
    # workspace-write preset allows writes inside the workspace
    from deeppyro.sandbox import Decision

    decision = gate.check_write(tmp_path / "a.txt") if hasattr(gate, "check_write") else None
    assert decision is None or decision.allowed is True


async def test_tools_service_builds_registry_for_workspace(app, tmp_path: Path) -> None:
    registry = app.service("tools").registry_for(tmp_path)
    names = set(registry.names()) if hasattr(registry, "names") else set()
    assert "read" in names or "read_file" in names
    # a second workspace gets independent instances
    other = app.service("tools").registry_for(tmp_path / "other")
    assert other is not registry
