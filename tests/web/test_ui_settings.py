"""Tests for the comment-preserving settings backend.

The editor must never rewrite lines it does not own: comments, key order and
unknown plugin tables survive an update byte for byte. The HTTP payload and the
provider probe are tested against fakes so the module stays independent of the
kernel runtime.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from deeppyro.api.types import CommandSpec
from deeppyro.builtin.commands import CommandsPlugin
from deeppyro.builtin.plugin_manager import PluginManagerPlugin
from deeppyro.builtin.sessions import SessionsPlugin
from deeppyro.builtin.settings import SettingsPlugin
from deeppyro.plugins.model import PluginInfo
from deeppyro.sandbox.permission import PermissionPresetsService
from deeppyro.web.ui.settings import ConfigEditor, settings_payload
from deeppyro.web.ui.settings import test_provider as probe_provider

BASE_CONFIG = """# deeppyro configuration (hand written)
# keep this comment

[agent]
provider = "local"  # preferred provider
model = "deepseek-v4-pro-qwen3.5-9b"
max_tokens = 8192

[providers.local]
api = "openai-completions"
base_url = "http://127.0.0.1:8000/v1"
api_key_env = "DEEPPYRO_LOCAL_API_KEY"
models = [{ id = "deepseek-v4-pro-qwen3.5-9b", context_window = 32768 }]

[providers.deepseek]
api = "openai-completions"
base_url = "https://api.deepseek.com/v1"
api_key = "sk-test-secret"  # real key
models = [{ id = "deepseek-chat", name = "DeepSeek Chat", context_window = 65536 }]

[my_plugin]
enabled = true
threshold = 0.5

[my_plugin.nested]
label = "keep me"
"""

SECRET = "sk-test-secret"


def _write_config(tmp_path, text: str = BASE_CONFIG):
    path = tmp_path / "config.toml"
    path.write_bytes(text.encode("utf-8"))
    return path


def _backups(tmp_path) -> list[str]:
    return sorted(item.name for item in tmp_path.glob("config.toml.bak*"))


def _tail(text: str) -> str:
    return text[text.index("[my_plugin]") :]


# -- agent updates -------------------------------------------------------


def test_update_agent_preserves_comments_and_unknown_tables(tmp_path):
    path = _write_config(tmp_path)
    editor = ConfigEditor(path)

    editor.update_agent(temperature=0.3, max_steps=7)

    text = path.read_text(encoding="utf-8")
    assert text.startswith("# deeppyro configuration (hand written)\n# keep this comment\n")
    assert 'provider = "local"  # preferred provider' in text
    agent_block = text.split("[agent]", 1)[1].split("[providers.local]", 1)[0]
    assert 'model = "deepseek-v4-pro-qwen3.5-9b"' in agent_block
    assert "max_tokens = 8192" in agent_block
    assert "temperature = 0.3" in agent_block
    assert "max_steps = 7" in agent_block
    assert _tail(text) == _tail(BASE_CONFIG)
    assert _backups(tmp_path) == ["config.toml.bak"]


def test_update_agent_provider_and_model_keeps_trailing_comment(tmp_path):
    path = _write_config(tmp_path)
    editor = ConfigEditor(path)

    editor.update_agent(provider="deepseek", model="deepseek-chat")

    snapshot = editor.snapshot()
    assert snapshot["agent"]["provider"] == "deepseek"
    assert snapshot["agent"]["model"] == "deepseek-chat"
    assert snapshot["agent"]["temperature"] is None
    text = path.read_text(encoding="utf-8")
    assert 'provider = "deepseek"  # preferred provider' in text
    assert 'model = "deepseek-chat"' in text
    assert _backups(tmp_path) == ["config.toml.bak"]


# -- provider updates and masking ---------------------------------------


def test_snapshot_never_exposes_api_keys(tmp_path):
    editor = ConfigEditor(_write_config(tmp_path))

    snapshot = editor.snapshot()

    assert SECRET not in json.dumps(snapshot)
    deepseek = next(item for item in snapshot["providers"] if item["name"] == "deepseek")
    assert set(deepseek) == {"name", "api", "base_url", "api_key_env", "api_key_set", "models"}
    assert deepseek["api_key_set"] is True
    assert deepseek["models"][0]["id"] == "deepseek-chat"
    assert set(deepseek["models"][0]) == {"id", "name", "context_window"}


def test_update_provider_keeps_and_clears_api_key(tmp_path):
    path = _write_config(tmp_path)
    editor = ConfigEditor(path)

    editor.update_provider("deepseek", base_url="https://api.deepseek.com/beta")
    text = path.read_text(encoding="utf-8")
    assert 'base_url = "https://api.deepseek.com/beta"' in text
    assert _tail(text) == _tail(BASE_CONFIG)

    unchanged = path.read_bytes()
    editor.update_provider("deepseek", api_key=None)
    assert path.read_bytes() == unchanged
    assert editor.snapshot()["providers"][1]["api_key_set"] is True

    editor.update_provider("deepseek", api_key="sk-new-secret")
    text = path.read_text(encoding="utf-8")
    assert 'api_key = "sk-new-secret"  # real key' in text
    assert SECRET not in text
    snapshot = editor.snapshot()
    assert snapshot["providers"][1]["api_key_set"] is True
    assert "sk-new-secret" not in json.dumps(snapshot)

    cleared = editor.update_provider("deepseek", api_key="")
    assert cleared["providers"][1]["api_key_set"] is False
    block = path.read_text(encoding="utf-8").split("[providers.deepseek]", 1)[1].split("[my_plugin]", 1)[0]
    assert "api_key =" not in block


# -- invalid input leaves the file untouched -----------------------------


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"temperature": 5.0}, "agent.temperature: must be between 0.0 and 2.0"),
        ({"max_tokens": 0}, "agent.max_tokens: must be a positive integer"),
        ({"provider": "missing"}, "agent.provider: unknown provider"),
        ({"model": ""}, "agent.model: must be a non-empty model id"),
        ({"max_steps": "3"}, "agent.max_steps: must be an integer"),
    ],
)
def test_invalid_agent_update_leaves_file_unchanged(tmp_path, kwargs, message):
    path = _write_config(tmp_path)
    editor = ConfigEditor(path)

    with pytest.raises(ValueError, match=re.escape(message)):
        editor.update_agent(**kwargs)

    assert path.read_bytes() == BASE_CONFIG.encode("utf-8")
    assert _backups(tmp_path) == []


@pytest.mark.parametrize(
    ("name", "kwargs", "message"),
    [
        (
            "deepseek",
            {"base_url": "ftp://example.test/v1"},
            "providers.deepseek.base_url: must be an http(s) URL",
        ),
        ("deepseek", {"base_url": ""}, "providers.deepseek.base_url: must be a non-empty http(s) URL"),
        ("ghost", {"api_key": "x"}, "providers.ghost: unknown provider"),
        (
            "deepseek",
            {"api_key_env": "not a name"},
            "providers.deepseek.api_key_env: must be an environment variable name",
        ),
    ],
)
def test_invalid_provider_update_leaves_file_unchanged(tmp_path, name, kwargs, message):
    path = _write_config(tmp_path)
    editor = ConfigEditor(path)

    with pytest.raises(ValueError, match=re.escape(message)):
        editor.update_provider(name, **kwargs)

    assert path.read_bytes() == BASE_CONFIG.encode("utf-8")
    assert _backups(tmp_path) == []


# -- backups and idempotency --------------------------------------------


def test_numbered_backups_keep_previous_versions(tmp_path):
    path = _write_config(tmp_path)
    editor = ConfigEditor(path)

    editor.update_agent(max_tokens=4096)
    first = path.read_text(encoding="utf-8")
    assert (tmp_path / "config.toml.bak").read_text(encoding="utf-8") == BASE_CONFIG

    editor.update_agent(max_tokens=2048)
    second = path.read_text(encoding="utf-8")
    assert (tmp_path / "config.toml.bak.1").read_text(encoding="utf-8") == first
    assert second != first
    assert _backups(tmp_path) == ["config.toml.bak", "config.toml.bak.1"]


def test_identical_update_does_not_write_or_back_up(tmp_path):
    path = _write_config(tmp_path)
    editor = ConfigEditor(path)
    editor.update_agent(max_tokens=4096)
    text = path.read_text(encoding="utf-8")
    backups = _backups(tmp_path)

    editor.update_agent(model="deepseek-v4-pro-qwen3.5-9b", max_tokens=4096)

    assert path.read_text(encoding="utf-8") == text
    assert _backups(tmp_path) == backups


def test_crlf_config_file_keeps_crlf_endings(tmp_path):
    crlf = BASE_CONFIG.replace("\n", "\r\n")
    path = tmp_path / "config.toml"
    path.write_bytes(crlf.encode("utf-8"))
    editor = ConfigEditor(path)

    editor.update_agent(temperature=0.4)

    raw = path.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n")
    assert b"temperature = 0.4\r\n" in raw


# -- configuration path --------------------------------------------------


def test_editor_path_honors_config_env(monkeypatch, tmp_path):
    target = tmp_path / "custom.toml"
    target.write_bytes(BASE_CONFIG.encode("utf-8"))
    monkeypatch.setenv("DEEPPYRO_CONFIG", str(target))

    editor = ConfigEditor()

    assert editor.path == target
    assert editor.snapshot()["path"] == str(target)


def test_snapshot_without_file_uses_defaults_and_creates_nothing(tmp_path):
    path = tmp_path / "absent" / "config.toml"

    snapshot = ConfigEditor(path).snapshot()

    assert not path.exists()
    assert snapshot["path"] == str(path)
    assert snapshot["agent"]["provider"] == "local"
    names = {item["name"] for item in snapshot["providers"]}
    assert {"local", "deepseek"} <= names


def test_set_permission_default_appends_missing_table(tmp_path):
    path = _write_config(tmp_path)
    editor = ConfigEditor(path)

    editor.set_permission_default("read-only")

    text = path.read_text(encoding="utf-8")
    assert text.startswith(BASE_CONFIG)
    assert text[len(BASE_CONFIG) :] == '\n[permission]\ndefaultPreset = "read-only"\n'
    with pytest.raises(ValueError, match=re.escape("permission.defaultPreset: unknown preset 'bogus'")):
        editor.set_permission_default("bogus")


# -- provider probe ------------------------------------------------------


class _FakeLlm:
    def __init__(self, models=(), error=None) -> None:
        self.models = list(models)
        self.error = error
        self.calls: list[tuple[str, bool]] = []

    async def list_models(self, provider, *, refresh=False):
        self.calls.append((provider, refresh))
        if self.error is not None:
            raise self.error
        return list(self.models)


async def test_test_provider_success(tmp_path):
    editor = ConfigEditor(_write_config(tmp_path))
    llm = _FakeLlm(models=[SimpleNamespace(id="a"), SimpleNamespace(id="b"), SimpleNamespace(id="c")])

    result = await probe_provider(editor, "local", llm)

    assert result["ok"] is True
    assert result["detail"] == "3 model(s) available"
    assert llm.calls == [("local", True)]
    assert result["latency_ms"] >= 0.0


async def test_test_provider_failure_scrubs_api_key(tmp_path):
    editor = ConfigEditor(_write_config(tmp_path))
    llm = _FakeLlm(error=RuntimeError(f"401 for key {SECRET}"))

    result = await probe_provider(editor, "deepseek", llm)

    assert result["ok"] is False
    assert SECRET not in result["detail"]
    assert "***" in result["detail"]
    assert result["detail"].startswith("RuntimeError:")
    assert result["latency_ms"] >= 0.0


async def test_test_provider_unknown_provider(tmp_path):
    editor = ConfigEditor(_write_config(tmp_path))
    llm = _FakeLlm()

    result = await probe_provider(editor, "ghost", llm)

    assert result["ok"] is False
    assert result["detail"] == "unknown provider: ghost"
    assert llm.calls == []


# -- settings payload ----------------------------------------------------


class _FakeCommands:
    def all(self):
        return [
            CommandSpec(name="help", summary="Show help", usage="/help", aliases=("h",), plugin_id="demo")
        ]


class _FakeSettings:
    revision = "rev-1"


class _FakePluginManager:
    def list(self):
        return [
            PluginInfo(
                id="demo",
                name="Demo",
                version="1.0",
                origin="plugins/demo",
                description="Demo plugin",
                enabled=True,
                provides=("commands",),
            )
        ]


class _FakeSessions:
    def native_list(self, *, include_archived=False):
        assert include_archived is False
        return [
            SimpleNamespace(
                id="s1",
                title="First",
                cwd="C:/work",
                turns=2,
                updated_at=1700000000,
                preset="default",
                preview=None,
            )
        ]


class _FakePermissions:
    default = "read-only"

    def catalog(self):
        return tuple(
            SimpleNamespace(name=name, sandbox=name, approval="ask", description=f"{name} preset")
            for name in ("read-only", "workspace-write")
        )

    def current(self):
        return "read-only"

    def is_custom(self):
        return False


class _FakeApp:
    def __init__(self, services, started) -> None:
        self._services = services
        self.started = started
        self.mount_records = ()

    def get(self, contract):
        return self._services.get(contract)


def test_settings_payload_sections(tmp_path):
    editor = ConfigEditor(_write_config(tmp_path))
    app = _FakeApp(
        {
            CommandsPlugin: _FakeCommands(),
            PluginManagerPlugin: _FakePluginManager(),
            SettingsPlugin: _FakeSettings(),
            SessionsPlugin: _FakeSessions(),
            PermissionPresetsService: _FakePermissions(),
        },
        {"demo": "active"},
    )
    service = SimpleNamespace(port=8777, bound_host="127.0.0.1", active_session_id="s1")

    payload = settings_payload(app, editor, service, "9.9.9")

    assert set(payload) == {
        "config",
        "provider",
        "agent",
        "permissions",
        "commands",
        "plugins",
        "sessions",
        "web",
        "about",
    }
    assert payload["config"] == editor.snapshot()
    assert SECRET not in json.dumps(payload)
    assert payload["provider"]["current"] == "local"
    assert payload["provider"]["model"] == "deepseek-v4-pro-qwen3.5-9b"
    assert len(payload["provider"]["providers"]) == 2
    assert payload["agent"] == payload["config"]["agent"]

    permissions = payload["permissions"]
    assert permissions["current"] == "read-only"
    assert permissions["default"] == "read-only"
    assert permissions["custom"] is False
    assert [item["name"] for item in permissions["presets"]] == ["read-only", "workspace-write"]

    assert payload["commands"] == [
        {"name": "help", "summary": "Show help", "usage": "/help", "aliases": ["h"], "plugin": "demo"}
    ]
    assert payload["plugins"]["revision"] == "rev-1"
    item = payload["plugins"]["items"][0]
    assert item["id"] == "demo"
    assert item["version"] == "1.0"
    assert item["enabled"] is True
    assert item["state"] == "active"
    assert item["provides"] == ["commands"]

    sessions = payload["sessions"]
    assert sessions["count"] == 1
    assert sessions["active_session_id"] == "s1"
    assert sessions["items"][0]["id"] == "s1"
    assert isinstance(sessions["dir"], str) and sessions["dir"]

    assert payload["web"] == {"host": "127.0.0.1", "port": 8777}
    assert payload["about"]["version"] == "9.9.9"
    assert payload["about"]["plugins"] == 1
    assert payload["about"]["python"].startswith("3.")


def test_settings_payload_without_services(tmp_path):
    editor = ConfigEditor(_write_config(tmp_path))

    payload = settings_payload(object(), editor, None, "0.1.0")

    assert payload["provider"]["providers"]
    assert payload["web"] == {"host": "127.0.0.1", "port": 0}
    assert [item["name"] for item in payload["permissions"]["presets"]] == [
        "read-only",
        "workspace-write",
        "danger-full-access",
    ]
    assert payload["permissions"]["default"] == "workspace-write"
    assert payload["plugins"] == {"revision": "", "items": []}
    assert payload["commands"] == []
    assert payload["sessions"]["count"] == 0
    assert payload["sessions"]["active_session_id"] is None
    assert payload["about"]["plugins"] == 0
