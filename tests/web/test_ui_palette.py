"""Pure-function tests for the command palette.

The dialog itself needs a NiceGUI client context, so these tests cover row
building, filtering, grouping and command-result routing, plus construction of
CommandPalette against a fake bridge.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from deeppyro.web.ui.commands import CommandResult
from deeppyro.web.ui.palette import (
    PALETTE_GROUPS,
    SETTINGS_SLUGS,
    CommandPalette,
    PaletteAction,
    PaletteBridge,
    PaletteGroup,
    PaletteKind,
    PaletteRow,
    filter_palette,
    grouped_rows,
    key_name,
    palette_rows,
    route_command_result,
)
from deeppyro.web.ui.settings_view import SettingsSection, section_spec


def sample_bootstrap() -> dict[str, Any]:
    """One bootstrap payload shaped like bridge.bootstrap()."""
    return {
        "commands": [
            {"name": "new", "summary": "Start a session", "group": "session", "shortcut": "Ctrl+N"},
            {"name": "settings", "summary": "Open settings"},
            {"name": "bare"},
        ],
        "sessions": [
            {
                "session_id": "session-1",
                "title": "Active session",
                "preset": "standard",
                "turns": 2,
                "updated_at": 1_700_000_000_000,
            },
            {"id": "session-2", "title": ""},
        ],
        "active_session_id": "session-1",
        "version": "1.2.3",
    }


class FakePaletteBridge:
    """Records the bridge calls the palette is allowed to make."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def bootstrap(self) -> dict[str, Any]:
        return sample_bootstrap()

    async def execute(
        self,
        name: str,
        args: list[str] | None = None,
        session_id: str | None = None,
    ) -> CommandResult:
        self.calls.append(("execute", name, args, session_id))
        return CommandResult(status="ok", message="ran " + name)


class ResultObject:
    """Anything with to_payload(), like the real CommandResult."""

    def to_payload(self) -> dict[str, Any]:
        return {"status": "ok", "data": {"session_id": "session-9"}}


# -- row building ---------------------------------------------------------


def test_palette_rows_build_all_three_groups() -> None:
    rows = palette_rows(sample_bootstrap())
    kinds = [row.kind for row in rows]
    assert kinds.count(PaletteKind.COMMAND) == 3
    assert kinds.count(PaletteKind.SESSION) == 2
    assert kinds.count(PaletteKind.SETTINGS) == len(SETTINGS_SLUGS)
    assert tuple(row.group for row in rows[:3]) == (PaletteGroup.COMMANDS,) * 3
    assert rows[0].title == "/new"
    assert rows[0].shortcut == "Ctrl+N"
    assert rows[0].command == "new"
    assert rows[0].icon == "sessions"
    assert rows[1].icon == "terminal"
    assert rows[2].title == "/bare"


def test_palette_session_rows_link_to_chat() -> None:
    rows = palette_rows(sample_bootstrap())
    sessions = [row for row in rows if row.kind is PaletteKind.SESSION]
    assert sessions[0].title == "Active session"
    assert sessions[0].path == "/c/session-1"
    assert sessions[0].session_id == "session-1"
    assert "active" in sessions[0].subtitle
    assert "2 turns" in sessions[0].subtitle
    assert sessions[1].title == "Untitled session"
    assert sessions[1].path == "/c/session-2"


def test_palette_settings_rows_use_section_metadata() -> None:
    rows = palette_rows(sample_bootstrap())
    settings = [row for row in rows if row.kind is PaletteKind.SETTINGS]
    assert tuple(row.path for row in settings) == tuple("/settings/" + slug for slug in SETTINGS_SLUGS)
    assert settings[0].title == section_spec("provider").title
    assert settings[0].subtitle == section_spec("provider").description
    for slug in SETTINGS_SLUGS:
        assert section_spec(slug).slug is SettingsSection(slug)
    assert "about" not in SETTINGS_SLUGS


def test_palette_rows_tolerate_missing_data() -> None:
    # Settings destinations are static, so they survive a missing bootstrap.
    assert palette_rows(None) == palette_rows({})
    assert tuple(row.kind for row in palette_rows(None)) == (PaletteKind.SETTINGS,) * len(SETTINGS_SLUGS)
    rows = palette_rows({"commands": "nope", "sessions": [None, 5, {"nope": 1}]})
    assert tuple(row.kind for row in rows) == (PaletteKind.SETTINGS,) * len(SETTINGS_SLUGS)


def test_palette_groups_are_fixed() -> None:
    assert PALETTE_GROUPS == (PaletteGroup.COMMANDS, PaletteGroup.SESSIONS, PaletteGroup.SETTINGS)


# -- filtering and grouping -----------------------------------------------


def test_filter_palette_is_case_insensitive() -> None:
    rows = palette_rows(sample_bootstrap())
    assert filter_palette(rows, "") == rows
    assert filter_palette(rows, None) == rows
    matched = filter_palette(rows, "NEW")
    assert matched == filter_palette(rows, "new")
    assert matched[0].title == "/new"
    assert "/new" in tuple(row.title for row in matched)
    assert len(filter_palette(rows, "SESSIONS")) == 2


def test_filter_palette_requires_every_token() -> None:
    rows = palette_rows(sample_bootstrap())
    matched = filter_palette(rows, "session active")
    assert tuple(row.title for row in matched) == ("Active session",)
    assert filter_palette(rows, "zzzz") == ()


def test_grouped_rows_keeps_group_order_and_drops_empty() -> None:
    rows = palette_rows(sample_bootstrap())
    groups = grouped_rows(rows)
    assert tuple(group for group, _rows in groups) == PALETTE_GROUPS
    assert tuple(len(group_rows) for _group, group_rows in groups) == (3, 2, len(SETTINGS_SLUGS))
    assert grouped_rows(()) == ()
    only_settings = tuple(row for row in rows if row.kind is PaletteKind.SETTINGS)
    assert tuple(group for group, _rows in grouped_rows(only_settings)) == (PaletteGroup.SETTINGS,)


def test_palette_row_haystack_includes_metadata() -> None:
    row = PaletteRow(
        group=PaletteGroup.COMMANDS,
        kind=PaletteKind.COMMAND,
        title="Cfg",
        subtitle="DeepSeek",
        command="config",
    )
    haystack = row.haystack
    assert "cfg" in haystack
    assert "deepseek" in haystack
    assert "commands" in haystack
    assert "config" in haystack


# -- command result routing -----------------------------------------------


def test_route_redirect_result_and_command_result_object() -> None:
    action = route_command_result({"status": "redirect", "redirect": "/settings/plugins", "data": {}})
    assert action.kind is PaletteAction.NAVIGATE
    assert action.path == "/settings/plugins"
    real = CommandResult(status="ok", data={"session_id": "session-9"})
    assert route_command_result(real) == route_command_result(ResultObject())


def test_route_ok_and_error_results() -> None:
    action = route_command_result(CommandResult(status="ok", message="done"))
    assert action.kind is PaletteAction.MESSAGE
    assert action.message == "done"
    fallback = route_command_result({"status": "ok"})
    assert fallback.kind is PaletteAction.MESSAGE
    assert fallback.message == "Command finished."
    error = route_command_result({"status": "error", "message": "/plan is not wired yet"})
    assert error.kind is PaletteAction.ERROR
    assert error.message == "/plan is not wired yet"
    bare_error = route_command_result({"status": "error"})
    assert bare_error.kind is PaletteAction.ERROR
    assert bare_error.message == "the command did not run"


def test_route_session_path_and_unexpected_results() -> None:
    action = route_command_result({"status": "ok", "data": {"path": "/c/session-1"}})
    assert (action.kind, action.path) == (PaletteAction.NAVIGATE, "/c/session-1")
    unexpected = route_command_result({"status": "weird"})
    assert unexpected.kind is PaletteAction.ERROR
    assert "unexpected command result" in unexpected.message
    assert route_command_result(None).kind is PaletteAction.ERROR


# -- keyboard -------------------------------------------------------------


def test_key_name_reads_generic_event_shapes() -> None:
    assert key_name(SimpleNamespace(args={"key": "Enter"})) == "Enter"
    assert key_name(SimpleNamespace(args=["Escape"])) == "Escape"
    assert key_name("ArrowDown") == "ArrowDown"
    assert key_name(SimpleNamespace(args=[])) == ""
    assert key_name(SimpleNamespace(args={"key": "ArrowUp"})) == "ArrowUp"


# -- class contract -------------------------------------------------------


def test_command_palette_constructs_with_fake_bridge() -> None:
    bridge = FakePaletteBridge()
    assert isinstance(bridge, PaletteBridge)
    palette = CommandPalette(bridge)
    assert palette.rows == ()
    assert palette.visible == ()
    assert palette.active_index == 0
    palette.close()

    async def navigate(path: str) -> None:
        del path

    assert CommandPalette(bridge, on_navigate=navigate) is not None


async def test_command_palette_open_without_mount_is_a_noop() -> None:
    palette = CommandPalette(FakePaletteBridge())
    await palette.open()
    assert palette.rows == ()
    await palette.open()
    palette.close()


async def test_fake_bridge_satisfies_palette_calls() -> None:
    bridge = FakePaletteBridge()
    payload = await bridge.bootstrap()
    rows = palette_rows(payload)
    assert rows
    result = await bridge.execute("new", [])
    assert result.status == "ok"
    assert bridge.calls == [("execute", "new", [], None)]
