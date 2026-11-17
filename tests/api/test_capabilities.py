"""Capability manifests: plugin governance metadata and builtin tool coverage."""

from __future__ import annotations

import json

import pytest

from deeppyro.api import (
    CapabilityManifest,
    Plugin,
    RiskLevel,
    Service,
    TokenCost,
    ToolCapability,
    build_manifest,
    inject_names,
    tool_capability,
)
from deeppyro.kernel.composition import core_bundle_path, load_bundle, resolve_row
from deeppyro.tools.base import BaseTool, make_parameters
from tests.tools.test_tool_schemas import all_tools


class _GovernedTool(BaseTool):
    name = "governed"
    description = "A tool with explicit governance metadata."
    risk = RiskLevel.HIGH
    idempotent = True
    parameters = make_parameters({"value": {"type": "string"}}, required=["value"])


class _CostedPlugin(Plugin):
    plugin_id = "costed"
    version = "1.2.3"
    inject = ("tools",)
    risk = RiskLevel.MEDIUM
    idempotent = False
    permissions = ("tools:write",)
    sandbox = "workspace-write"
    token_cost = TokenCost(input_per_mtok=0.5, output_per_mtok=1.5)
    context_window = 32768


class _Provider(Service):
    provide = "capability-demo"


def _core_plugin_classes() -> list[type[Plugin]]:
    classes: list[type[Plugin]] = []
    for row in load_bundle(core_bundle_path()).rows:
        if row.group:
            continue
        target = resolve_row(row)
        if isinstance(target, type) and issubclass(target, Plugin):
            classes.append(target)
    return classes


def test_builtin_tools_all_have_capability_rows() -> None:
    tools = all_tools()
    assert len(tools) == 43
    for tool in tools:
        row = tool_capability(tool)
        assert row.name == tool.name
        assert isinstance(row.risk, RiskLevel)
        assert isinstance(row.idempotent, bool)
        assert row.parallel_safe == bool(tool.parallel_safe)


def test_tool_governance_metadata_never_enters_the_model_schema() -> None:
    tool = _GovernedTool()
    assert tool.risk is RiskLevel.HIGH
    assert tool.idempotent is True
    rendered = json.dumps(tool.parameters)
    assert "risk" not in rendered
    assert "idempotent" not in rendered
    assert rendered == json.dumps(_GovernedTool.parameters)
    assert set(tool.parameters) == {"type", "properties", "required", "additionalProperties"}


def test_every_core_plugin_has_a_manifest() -> None:
    classes = _core_plugin_classes()
    assert len(classes) >= 30
    for cls in classes:
        manifest = build_manifest(cls)
        assert isinstance(manifest, CapabilityManifest)
        assert manifest.plugin_id
        assert manifest.version
        assert manifest.services_required == tuple(inject_names(cls))
        provided = getattr(cls, "provide", "")
        if isinstance(provided, str) and provided:
            assert manifest.services_provided == (provided,)
        payload = manifest.to_dict()
        json.dumps(payload)
        assert payload["plugin_id"] == manifest.plugin_id


def test_plugin_manifest_uses_declared_governance_fields() -> None:
    manifest = build_manifest(_CostedPlugin)
    assert manifest.plugin_id == "costed"
    assert manifest.version == "1.2.3"
    assert manifest.services_required == ("tools",)
    assert manifest.permissions == ("tools:write",)
    assert manifest.sandbox == "workspace-write"
    assert manifest.risk is RiskLevel.MEDIUM
    assert manifest.idempotent is False
    assert manifest.token_cost == TokenCost(input_per_mtok=0.5, output_per_mtok=1.5)
    assert manifest.context_window == 32768
    instance = _CostedPlugin(None, None)
    assert build_manifest(instance).plugin_id == "costed"
    assert instance.capability_manifest().plugin_id == "costed"


def test_service_plugin_manifest_lists_the_provided_service() -> None:
    manifest = build_manifest(_Provider)
    assert manifest.services_provided == ("capability-demo",)


def test_manifest_rejects_invalid_governance_data() -> None:
    with pytest.raises(ValueError, match="plugin_id"):
        CapabilityManifest(plugin_id="")
    with pytest.raises(ValueError, match="sandbox"):
        CapabilityManifest(plugin_id="demo", sandbox="root")


def test_tool_capability_and_token_cost() -> None:
    cost = TokenCost(input_per_mtok=1.0, output_per_mtok=2.0, cached_input_per_mtok=0.25)
    assert cost.estimate(input_tokens=1_000_000, output_tokens=500_000) == pytest.approx(2.0)
    assert cost.estimate(cached_tokens=1_000_000) == pytest.approx(0.25)
    assert ToolCapability(name="demo").risk is RiskLevel.LOW
    assert ToolCapability(name="demo").idempotent is False
