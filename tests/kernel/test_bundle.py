"""Bundle-layer tests: default plugin order, aliases, patch manifests, boot."""

from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.kernel import (
    DEFAULT_PLUGINS,
    PluginSpec,
    boot,
    bundle_specs,
    create_app,
    load_manifest,
    merge_plugins,
)

EXPECTED_ORDER = ["frontend", "llm", "sessions", "sandbox", "tools", "agents"]


def _config() -> Config:
    return Config(
        providers={
            "local": ProviderConfig(
                baseURL="http://127.0.0.1:8000/v1",
                models=[ModelConfig(id="m")],
            )
        },
        agent={"provider": "local", "model": "m"},
    )


def test_default_plugin_order() -> None:
    assert [spec.id for spec in DEFAULT_PLUGINS] == EXPECTED_ORDER
    assert [spec.name for spec in DEFAULT_PLUGINS] == EXPECTED_ORDER  # aliases
    assert all(spec.enabled() for spec in DEFAULT_PLUGINS)


def test_every_bundle_alias_resolves_to_a_class() -> None:
    for spec in DEFAULT_PLUGINS:
        cls = spec.resolve()
        assert isinstance(cls, type), spec.id


def test_bundle_specs_applies_patch_file(tmp_path: Path) -> None:
    manifest = tmp_path / "plugins.toml"
    manifest.write_text(
        """
[[plugins]]
id = "llm"
config = { timeout_s = 42 }

[[plugins]]
id = "agents"
disabled = true

[[plugins]]
id = "echo"
name = "tests.kernel.plugins_dummy:CounterPlugin"
after = "llm"
config = { start = 5 }
""",
        encoding="utf-8",
    )
    specs = bundle_specs([str(manifest)])
    by_id = {spec.id: spec for spec in specs}
    assert by_id["llm"].config == {"timeout_s": 42}
    assert by_id["agents"].disabled is True
    assert [spec.id for spec in specs] == [
        "frontend",
        "llm",
        "echo",
        "sessions",
        "sandbox",
        "tools",
        "agents",
    ]


async def test_boot_skips_disabled_plugins(tmp_path: Path) -> None:
    manifest = tmp_path / "plugins.toml"
    manifest.write_text(
        "\n".join(
            [
                "[[plugins]]",
                'id = "sessions"',
                "disabled = true",
                "[[plugins]]",
                'id = "sandbox"',
                "disabled = true",
                "[[plugins]]",
                'id = "tools"',
                "disabled = true",
                "[[plugins]]",
                'id = "agents"',
                "disabled = true",
            ]
        ),
        encoding="utf-8",
    )
    app = await boot(_config(), manifest_paths=[str(manifest)])
    try:
        assert app.has_service("llm")
        assert app.has_service("frontend")
        assert not app.has_service("sessions")
        # started only lists active plugin fibers, in manifest order.
        assert list(app.started) == ["frontend", "llm"]
        assert all(state == "ACTIVE" for state in app.started.values())
    finally:
        await app.stop()
    assert not app.has_service("llm")


async def test_boot_fails_with_missing_domain_module() -> None:
    from deeppyro.kernel.app import KernelBootError

    specs = [PluginSpec(id="broken", name="tests.kernel.plugins_dummy:ExplodingPlugin")]
    with pytest.raises(KernelBootError) as excinfo:
        await create_app(_config(), plugins=specs)
    assert "broken" in str(excinfo.value)
