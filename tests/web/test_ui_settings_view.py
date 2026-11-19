"""Pure-function tests for the NiceGUI settings surface.

The SettingsView class itself needs a NiceGUI client context, so these tests
cover the projectors, schema-to-control descriptors and save-state routing, plus
construction of the view against a fake bridge.
"""

from __future__ import annotations

from typing import Any

import pytest

from deeppyro.web.ui.settings_view import (
    DEFAULT_SECTION,
    FieldDescriptor,
    FieldKind,
    FieldOption,
    SaveState,
    SaveTone,
    SectionSpec,
    SettingsBridge,
    SettingsSection,
    SettingsView,
    coerce_field_value,
    collect_plugin_values,
    command_rows,
    current_model,
    current_permission,
    current_provider,
    error_text,
    group_for_command,
    grouped_commands,
    humanize_label,
    model_choices,
    permission_presets,
    plugin_save_detail,
    probe_state,
    provider_choices,
    provider_rows,
    resolve_section,
    save_bar_state,
    schema_fields,
    section_spec,
    section_specs,
    unsupported_schema_fields,
)


def sample_payload() -> dict[str, Any]:
    """One settings payload shaped exactly like settings_payload()."""
    return {
        "config": {
            "path": "C:/cfg/config.toml",
            "agent": {"provider": "deepseek", "model": "deepseek-flash"},
            "providers": [
                {"name": "deepseek", "api": "openai", "base_url": "https://api.deepseek.com"},
            ],
        },
        "provider": {
            "current": "deepseek",
            "model": "deepseek-flash",
            "providers": [
                {
                    "name": "deepseek",
                    "api": "openai",
                    "base_url": "https://api.deepseek.com",
                    "api_key_set": True,
                    "api_key_env": "DEEPSEEK_API_KEY",
                    "models": [
                        {"id": "deepseek-flash", "name": "DeepSeek Flash", "context_window": 128000},
                        {"id": "plain-model", "name": "plain-model", "context_window": 8000},
                    ],
                },
                {
                    "name": "local",
                    "api": "openai",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "api_key_set": False,
                    "models": [],
                },
            ],
        },
        "agent": {
            "provider": "deepseek",
            "model": "deepseek-flash",
            "temperature": 0.7,
            "max_tokens": 8000,
            "max_steps": 40,
        },
        "permissions": {
            "presets": [
                {
                    "name": "standard",
                    "sandbox": "workspace-write",
                    "approval": "ask",
                    "description": "Restrict writes",
                },
                {"name": "danger", "sandbox": "danger-full-access", "approval": "never", "description": ""},
            ],
            "current": "standard",
            "default": "standard",
            "custom": False,
        },
        "commands": [
            {
                "name": "new",
                "summary": "Start a session",
                "usage": "/new",
                "aliases": ["n"],
                "plugin": "core",
            },
            {"name": "settings", "summary": "Open settings", "usage": "/settings", "aliases": []},
            {"name": "plan", "summary": "Plan work", "usage": "/plan", "aliases": [], "group": "work"},
        ],
        "plugins": {"revision": "abc123", "items": []},
        "sessions": {
            "dir": "C:/sessions",
            "count": 3,
            "active_session_id": "session-1",
            "items": [
                {
                    "id": "session-1",
                    "title": "Active session",
                    "cwd": "D:/work",
                    "turns": 2,
                    "updated_at": 1_700_000_000_000,
                    "preset": "standard",
                    "preview": "hello",
                }
            ],
        },
        "web": {"host": "127.0.0.1", "port": 8765},
        "about": {"version": "1.2.3", "python": "3.13.1", "platform": "win32", "plugins": 12},
    }


PLUGIN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["name"],
    "properties": {
        "name": {"type": "string", "title": "Display name", "description": "Shown in the UI."},
        "enabled": {"type": "boolean", "title": "Enabled"},
        "mode": {"type": "string", "title": "Mode", "enum": ["fast", "safe"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "ratio": {"type": "number", "minimum": 0.0},
        "api_key": {"type": "string", "title": "API key"},
        "headers": {"type": "object"},
        "broken": 5,
    },
}


class FakeSettingsBridge:
    """Records the calls SettingsView is allowed to make."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def settings(self) -> dict[str, Any]:
        return sample_payload()

    async def update_agent(self, **changes: Any) -> dict[str, Any]:
        self.calls.append(("update_agent", changes))
        return {"agent": {"provider": "deepseek", "model": "deepseek-flash"}}

    async def update_provider(self, provider: str, **changes: Any) -> dict[str, Any]:
        self.calls.append(("update_provider", provider, changes))
        return {"provider": provider}

    async def test_provider(self, provider: str) -> dict[str, Any]:
        self.calls.append(("test_provider", provider))
        return {"ok": True, "detail": "ok", "latency_ms": 12.0}

    async def set_permission(self, preset: str) -> dict[str, Any]:
        self.calls.append(("set_permission", preset))
        return {"permissions": preset}

    def plugin_settings(self, plugin_id: str) -> dict[str, Any]:
        self.calls.append(("plugin_settings", plugin_id))
        return {"plugin_id": plugin_id, "revision": "rev-1", "schema": PLUGIN_SCHEMA, "values": {}}

    def update_plugin_settings(
        self,
        plugin_id: str,
        values: Any,
        expected_revision: str = "",
    ) -> dict[str, Any]:
        self.calls.append(("update_plugin_settings", plugin_id, dict(values), expected_revision))
        return {"plugin_id": plugin_id, "changed": ["name"], "restart_required": False}


# -- sections --------------------------------------------------------------


def test_section_slugs_are_exact() -> None:
    assert tuple(section.value for section in SettingsSection) == (
        "provider",
        "agent",
        "permissions",
        "commands",
        "plugins",
        "sessions",
        "web",
        "about",
    )
    assert DEFAULT_SECTION is SettingsSection.PROVIDER


def test_section_specs_cover_every_section_in_nav_order() -> None:
    specs = section_specs()
    assert len(specs) == 8
    assert tuple(spec.slug for spec in specs) == tuple(SettingsSection)
    for spec in specs:
        assert isinstance(spec, SectionSpec)
        assert spec.label and spec.title and spec.description and spec.icon


def test_resolve_section_falls_back_to_provider() -> None:
    assert resolve_section(None) is SettingsSection.PROVIDER
    assert resolve_section("") is SettingsSection.PROVIDER
    assert resolve_section("plugins") is SettingsSection.PLUGINS
    assert resolve_section("PROVIDER") is SettingsSection.PROVIDER
    assert resolve_section("behavior") is SettingsSection.AGENT
    assert resolve_section("models") is SettingsSection.PROVIDER
    assert resolve_section("does-not-exist") is SettingsSection.PROVIDER


def test_section_spec_reads_title_and_description() -> None:
    spec = section_spec("plugins")
    assert spec.slug is SettingsSection.PLUGINS
    assert spec.title == "Plugins"
    assert "plugin" in spec.description.lower()
    assert section_spec("nope").slug is SettingsSection.PROVIDER


# -- plugin schema to field descriptors -----------------------------------


def test_schema_fields_builds_native_controls() -> None:
    fields = schema_fields(PLUGIN_SCHEMA)
    by_name = {field.name: field for field in fields}
    assert tuple(by_name) == ("name", "enabled", "mode", "limit", "ratio", "api_key")
    assert by_name["name"].kind is FieldKind.STRING
    assert by_name["name"].label == "Display name"
    assert by_name["name"].required is True
    assert by_name["name"].description == "Shown in the UI."
    assert by_name["enabled"].kind is FieldKind.BOOLEAN
    assert by_name["enabled"].default is False
    assert by_name["mode"].kind is FieldKind.ENUM
    assert tuple(option.value for option in by_name["mode"].options) == ("fast", "safe")
    assert by_name["limit"].kind is FieldKind.INTEGER
    assert by_name["limit"].minimum == 1.0
    assert by_name["limit"].maximum == 100.0
    assert by_name["ratio"].kind is FieldKind.NUMBER
    assert by_name["api_key"].secret is True
    assert by_name["name"].secret is False


def test_schema_fields_skip_unsupported_properties() -> None:
    assert unsupported_schema_fields(PLUGIN_SCHEMA) == ("headers", "broken")
    assert schema_fields(None) == ()
    assert schema_fields({}) == ()
    assert schema_fields({"properties": []}) == ()


def test_humanize_label() -> None:
    assert humanize_label("api_key_env") == "Api key env"
    assert humanize_label("name") == "Name"
    assert humanize_label("") == ""


# -- value coercion -------------------------------------------------------


def test_collect_plugin_values_coerces_and_skips_blanks() -> None:
    fields = schema_fields(PLUGIN_SCHEMA)
    values = collect_plugin_values(
        fields,
        {
            "name": "hello",
            "enabled": "off",
            "mode": "fast",
            "limit": "5",
            "ratio": "0.5",
            "api_key": "***",
        },
    )
    assert values == {
        "name": "hello",
        "enabled": False,
        "mode": "fast",
        "limit": 5,
        "ratio": 0.5,
    }


def test_collect_plugin_values_requires_required_fields() -> None:
    with pytest.raises(ValueError, match="Display name: a value is required"):
        collect_plugin_values(schema_fields(PLUGIN_SCHEMA), {"name": "   "})


def test_collect_plugin_values_never_sends_masked_secrets() -> None:
    fields = schema_fields(PLUGIN_SCHEMA)
    values = collect_plugin_values(fields, {"name": "x", "api_key": "\u2022\u2022\u2022"})
    assert "api_key" not in values


def test_coerce_field_value_rejects_bad_input() -> None:
    integer = FieldDescriptor(name="n", label="N", kind=FieldKind.INTEGER)
    with pytest.raises(ValueError, match="N: expected an integer"):
        coerce_field_value(integer, "1.5")
    boolean = FieldDescriptor(name="b", label="B", kind=FieldKind.BOOLEAN)
    with pytest.raises(ValueError, match="B: expected a boolean"):
        coerce_field_value(boolean, "maybe")
    enum = FieldDescriptor(
        name="level",
        label="Level",
        kind=FieldKind.ENUM,
        options=(FieldOption(value=1, label="One"), FieldOption(value=2, label="Two")),
    )
    assert coerce_field_value(enum, "2") == 2
    with pytest.raises(ValueError, match="Level: not one of the allowed values"):
        coerce_field_value(enum, "9")


# -- payload projections --------------------------------------------------


def test_provider_and_model_projections() -> None:
    payload = sample_payload()
    assert tuple(row["name"] for row in provider_rows(payload)) == ("deepseek", "local")
    assert tuple(choice.value for choice in provider_choices(payload)) == ("deepseek", "local")
    assert current_provider(payload) == "deepseek"
    choices = model_choices(payload, "deepseek")
    assert tuple(choice.value for choice in choices) == ("deepseek-flash", "plain-model")
    assert choices[0].label == "DeepSeek Flash (deepseek-flash)"
    assert choices[1].label == "plain-model"
    assert current_model(payload) == "deepseek-flash"
    assert provider_rows(None) == ()
    assert provider_choices(payload)[0].value == "deepseek"


def test_current_provider_falls_back_to_first_row() -> None:
    payload = sample_payload()
    payload["provider"] = {"providers": payload["provider"]["providers"]}
    payload["agent"] = {}
    payload["config"] = {}
    assert current_provider(payload) == "deepseek"


def test_permission_projections() -> None:
    payload = sample_payload()
    presets = permission_presets(payload)
    assert tuple(choice.value for choice in presets) == ("standard", "danger")
    assert presets[0].label == "standard \u2014 Restrict writes"
    assert presets[1].label == "danger"
    assert current_permission(payload) == "standard"
    assert permission_presets({}) == ()
    assert current_permission({}) == ""


# -- command catalogue ----------------------------------------------------


def test_command_rows_derive_groups_and_usage() -> None:
    rows = command_rows(sample_payload()["commands"])
    by_name = {row.name: row for row in rows}
    assert by_name["new"].group == "session"
    assert by_name["new"].aliases == ("n",)
    assert by_name["new"].plugin == "core"
    assert by_name["settings"].group == "system"
    assert by_name["plan"].group == "work"
    assert by_name["settings"].usage == "/settings"
    assert group_for_command("unknown-command") == "system"


def test_command_rows_default_usage_and_skip_invalid() -> None:
    rows = command_rows(
        [
            {"name": "bare"},
            {"summary": "no name"},
            "not-a-mapping",
        ]
    )
    assert len(rows) == 1
    assert rows[0].usage == "/bare"
    assert rows[0].group == "system"


def test_grouped_commands_preserve_known_order_then_extras() -> None:
    rows = command_rows(
        [
            {"name": "settings"},
            {"name": "new"},
            {"name": "zeta", "group": "zeta"},
            {"name": "alpha", "group": "alpha"},
        ]
    )
    groups = grouped_commands(rows)
    assert tuple(group for group, _rows in groups) == ("session", "system", "alpha", "zeta")


# -- save bar and probe states --------------------------------------------


def test_save_bar_plain_write_means_saved() -> None:
    state = save_bar_state({"agent": {"temperature": 0.5}})
    assert state.text == "Saved"
    assert state.tone is SaveTone.OK
    assert state.klass == "dp-save-ok"


def test_save_bar_reports_restart_required() -> None:
    state = save_bar_state(
        {
            "changed": ["name"],
            "restart_required": True,
            "restart_fields": ["api_key"],
        }
    )
    assert state.text == "Restart required"
    assert state.tone is SaveTone.WARN
    assert state.klass == "dp-save-warn"
    assert "next boot: api_key" in state.detail


def test_save_bar_uses_volatile_changed_and_live_fields() -> None:
    state = save_bar_state({"volatile_changed": ["token"], "live_changed": ["enabled"]})
    assert state.text == "Restart required"
    assert "next boot: token" in state.detail
    live = save_bar_state({"changed": ["enabled"], "live_fields": ["enabled"]})
    assert live.text == "Saved"
    assert "live: enabled" in live.detail
    assert "changed: enabled" in live.detail


def test_save_state_error_is_never_success() -> None:
    state = SaveState.error("boom")
    assert state.text == "Not saved"
    assert state.tone is SaveTone.ERROR
    assert state.detail == "boom"
    assert state.klass == "dp-save-error"
    idle = SaveState.idle()
    assert idle.tone is SaveTone.IDLE
    assert idle.klass == "dp-save-idle"


def test_probe_state_reports_ok_latency_or_error() -> None:
    ok = probe_state({"ok": True, "detail": "ok", "latency_ms": 42.6})
    assert ok.text == "Reachable \u00b7 43 ms"
    assert ok.tone is SaveTone.OK
    assert ok.detail == ""
    failed = probe_state({"ok": False, "detail": "connection refused"})
    assert failed.text == "Test failed"
    assert failed.tone is SaveTone.ERROR
    assert failed.detail == "connection refused"
    assert probe_state(None).text == "Test failed"


def test_plugin_save_detail() -> None:
    detail = plugin_save_detail(
        {
            "changed": ["name", "enabled"],
            "live_fields": ["enabled"],
            "restart_fields": ["api_key"],
        }
    )
    assert detail == "changed: name, enabled \u00b7 live: enabled \u00b7 restart: api_key"
    assert plugin_save_detail(None) == ""


def test_error_text_is_one_line() -> None:
    assert error_text(ValueError("bad\n value")) == "bad value"
    assert error_text(RuntimeError("")) == "RuntimeError"


# -- class contract -------------------------------------------------------


def test_settings_view_constructs_with_fake_bridge() -> None:
    bridge = FakeSettingsBridge()
    assert isinstance(bridge, SettingsBridge)
    view = SettingsView(bridge)
    assert view.section is SettingsSection.PROVIDER
    assert view.payload is None
    view = SettingsView(bridge, section="behavior")
    assert view.section is SettingsSection.AGENT
    view = SettingsView(bridge, section="nope")
    assert view.section is SettingsSection.PROVIDER


async def test_settings_view_fake_bridge_implements_protocol_calls() -> None:
    bridge = FakeSettingsBridge()
    payload = await bridge.settings()
    assert payload["about"]["version"] == "1.2.3"
    assert "api_key_env" in payload["provider"]["providers"][0]
    response = await bridge.update_agent(provider="local", model="local")
    assert response["agent"]["provider"] == "deepseek"
    assert bridge.calls == [("update_agent", {"provider": "local", "model": "local"})]
