"""Windows sandbox boundary facts in the capability report (KI-020).

``enforcement=partial`` is a host-wide fact: the Windows backend confines file
writes through ACLs but not network/read/process operations, while other hosts
have no local backend and fail closed.  Per-command enforcement still comes from
the sandbox plugin's live Confinement; these tests pin the reported fact and its
platform-specific reason so the documentation cannot silently drift.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from deeppyro.config import Config
from deeppyro.kernel.app import create_app
from deeppyro.kernel.capabilities import (
    CapabilityRegistry,
    CapabilityReport,
    host_sandbox_boundary,
)


def test_host_sandbox_boundary_matches_the_platform() -> None:
    boundary = host_sandbox_boundary()
    assert boundary.reason

    if os.name == "nt":
        assert boundary.platform == "windows"
        assert boundary.enforcement == "partial"
        assert "file writes only" in boundary.reason
        assert "ACL" in boundary.reason
        assert "network" in boundary.reason
        assert "not constrained" in boundary.reason
    else:
        assert boundary.enforcement == "unavailable"
        assert "fail closed" in boundary.reason


def test_capability_report_defaults_to_the_host_boundary() -> None:
    report = CapabilityReport()
    assert report.sandbox == host_sandbox_boundary()
    assert report.to_dict()["sandbox"] == host_sandbox_boundary().to_dict()


def test_registry_report_carries_the_sandbox_boundary() -> None:
    assert CapabilityRegistry().report().sandbox == host_sandbox_boundary()


async def test_booted_capability_dict_exposes_the_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    config = Config.model_validate({"agent": {"provider": "local", "model": "m"}})
    app = await create_app(config)
    try:
        data = app.capability_dict()
    finally:
        await app.stop()

    assert data["sandbox"] == host_sandbox_boundary().to_dict()
    if os.name == "nt":
        assert data["sandbox"]["enforcement"] == "partial"
        assert data["sandbox"]["reason"]
