"""Integration: the native headless loop resolves the observability service seams.

`AgentLoop` resolves `UsageSink` and `TraceExportSink` by their protocol service
names (`"usage"`, `"trace_export"`), not by plugin ids. This test boots the real
core bundle and asserts both services are present under those names, so
`HeadlessRuntime._plugin_services()` cannot silently drop them again (an empty
usage ledger / trace export would otherwise be invisible to `--headless` runs).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro import headless
from deeppyro.api.services import service_name
from deeppyro.builtin.usage import UsageService
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.kernel import boot
from deeppyro.observability.protocols import TraceExportSink


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


async def test_native_headless_services_are_the_observability_contracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    config = _config()
    app = await boot(config)
    try:
        runtime = headless.HeadlessRuntime(
            cfg=config,
            cwd=tmp_path,
            kernel_app=app,
            provider_name="local",
            model="m",
            context_window=32768,
            store=None,
            provider=None,
            registry=None,
            permission=None,
            agents=None,
        )
        services = runtime._plugin_services()
    finally:
        await app.stop()

    usage = services.get("usage")
    assert isinstance(usage, UsageService)
    assert callable(getattr(usage, "record_turn", None))
    trace_export = services.get("trace_export")
    assert trace_export is not None
    assert callable(getattr(trace_export, "export_trace_record", None))
    # The plugin id never was a service name: the loop looks up the protocol key.
    assert "telemetry" not in services

    # The two names must be the ones the loop binds its protocols to.
    assert service_name(UsageService) == "usage"
    assert service_name(TraceExportSink) == "trace_export"
