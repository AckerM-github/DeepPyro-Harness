"""Preset model overrides are validated against the host provider config.

A preset may pin a model (PresetSpec.model). Before this check existed the
override went straight into AgentOptions, so an unknown model id only failed on
the next provider request. The check runs on registration and on resolve, and
mirrors config lint: ConfigWarning by default, ConfigError under strict mode
(DEEPPYRO_STRICT_CONFIG). A provider without a models list accepts any model
(wildcard provider).
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from deeppyro.api import PresetSpec
from deeppyro.builtin.presets import PresetsPlugin
from deeppyro.config import Config, ConfigWarning, ModelConfig, ProviderConfig
from deeppyro.core.errors import ConfigError
from deeppyro.kernel import boot
from deeppyro.kernel.config_service import KernelConfig


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep user bundles out of the test run and start from non-strict config."""
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DEEPPYRO_STRICT_CONFIG", raising=False)


def _config(tmp_path: Path, *, models: list[ModelConfig] | None = None) -> Config:
    return Config(
        providers={
            "local": ProviderConfig(
                baseURL="http://127.0.0.1:8000/v1",
                models=[ModelConfig(id="m")] if models is None else models,
            )
        },
        agent={"provider": "local", "model": "m"},
        sessionDir=tmp_path / "sessions",
    )


def _config_warnings(records: list[warnings.WarningMessage]) -> list[str]:
    return [str(record.message) for record in records if issubclass(record.category, ConfigWarning)]


async def test_declared_model_is_accepted_without_warning(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        presets = app.require(PresetsPlugin)
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter("always")
            presets.add(PresetSpec(id="reviewer", name="Reviewer", model="m"))
        assert _config_warnings(records) == []
        assert presets.resolve("reviewer").model == "m"
    finally:
        await app.stop()


async def test_unknown_model_warns_once_and_keeps_the_preset(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        presets = app.require(PresetsPlugin)
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter("always")
            presets.add(PresetSpec(id="reviewer", name="Reviewer", model="missing"))
            presets.add(PresetSpec(id="reviewer", name="Reviewer v2", model="missing"))
            assert presets.resolve("reviewer").model == "missing"

        messages = _config_warnings(records)
        assert len(messages) == 1
        assert "preset 'reviewer' model 'missing' is not offered by provider 'local'" in messages[0]
        assert "(offers: m)" in messages[0]
        # Both revisions still registered: the warning does not block the preset.
        assert presets.resolve("reviewer").revision >= 1
    finally:
        await app.stop()


async def test_unknown_model_is_an_error_in_strict_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPPYRO_STRICT_CONFIG", "1")
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        presets = app.require(PresetsPlugin)
        with pytest.raises(ConfigError, match="invalid preset model"):
            presets.add(PresetSpec(id="reviewer", name="Reviewer", model="missing"))
        assert "reviewer" not in presets.ids()
    finally:
        await app.stop()


async def test_provider_without_models_is_a_wildcard(tmp_path: Path) -> None:
    config = Config(
        providers={"local": ProviderConfig(baseURL="http://127.0.0.1:8000/v1", models=[])},
        agent={"provider": "local", "model": "anything"},
        sessionDir=tmp_path / "sessions",
    )
    app = await boot(config, manifest_paths=[])
    try:
        presets = app.require(PresetsPlugin)
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter("always")
            presets.add(PresetSpec(id="reviewer", name="Reviewer", model="anything"))
        assert _config_warnings(records) == []
        assert presets.resolve("reviewer").model == "anything"
    finally:
        await app.stop()


async def test_unknown_provider_warns_and_fails_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        config_service = app.require(KernelConfig)
        assert isinstance(config_service, KernelConfig)
        config_service.value = Config(
            providers={},
            agent={"provider": "ghost", "model": "m"},
            sessionDir=tmp_path / "ghost-sessions",
        )
        presets = app.require(PresetsPlugin)
        with pytest.warns(ConfigWarning, match="provider 'ghost' is not configured"):
            presets.add(PresetSpec(id="ghosted", name="Ghosted", model="m"))

        monkeypatch.setenv("DEEPPYRO_STRICT_CONFIG", "1")
        with pytest.raises(ConfigError, match="provider 'ghost' is not configured"):
            presets.add(PresetSpec(id="ghosted-2", name="Ghosted 2", model="m"))
        assert "ghosted-2" not in presets.ids()
    finally:
        await app.stop()


async def test_resolve_revalidates_against_the_current_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        presets = app.require(PresetsPlugin)
        presets.add(PresetSpec(id="reviewer", name="Reviewer", model="m"))

        # The config moves on after registration: the pinned model disappears.
        config_service = app.require(KernelConfig)
        config_service.value = Config(
            providers={
                "local": ProviderConfig(
                    baseURL="http://127.0.0.1:8000/v1",
                    models=[ModelConfig(id="other")],
                )
            },
            agent={"provider": "local", "model": "other"},
            sessionDir=tmp_path / "sessions-2",
        )
        monkeypatch.setenv("DEEPPYRO_STRICT_CONFIG", "1")
        with pytest.raises(ConfigError, match="invalid preset model"):
            presets.resolve("reviewer")
    finally:
        await app.stop()


async def test_registration_outside_a_host_config_is_left_alone(tmp_path: Path) -> None:
    """A host without the kernel config service skips validation (isolated hosts)."""
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        presets = app.require(PresetsPlugin)
        presets._host_config = lambda: None  # type: ignore[method-assign]
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter("always")
            presets.add(PresetSpec(id="headless", name="Headless", model="no-config"))
        assert _config_warnings(records) == []
    finally:
        await app.stop()
