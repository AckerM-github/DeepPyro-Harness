"""Modal screen for managing external plugins.

The screen is a thin view over deeppyro.plugins.PluginManager: it lists the
plugin rows in a table and delegates every mutation to the manager, so the same
code path is exercised by the TUI and by tests. Errors from the manager are
reported with App.notify and never escape the modal.

Keyboard: e enables, d disables, r refreshes, i shows validation details and
Esc closes the screen. A manager can be injected for tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Static

from deeppyro.plugins.model import SOURCE_LABELS, PluginInfo, ValidationResult

if TYPE_CHECKING:
    from deeppyro.plugins.manager import PluginManager
    from deeppyro.tui.health import HealthSnapshot, PluginHealth

_COLUMNS = ("名称", "版本", "来源", "状态", "说明")
_EMPTY_HINT = (
    "没有发现外部插件。可用来源：deeppyro.plugins 入口点、"
    "用户插件目录下的 <name>/plugin.toml、用户清单的 [[plugins]] 条目。"
)
_KEY_HINT = "e 启用   d 禁用   r 刷新   i 详情   Esc 关闭"


class PluginScreen(ModalScreen[str | None]):
    """External plugin manager shown as a modal over the conversation."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("e", "enable", "启用", show=False),
        Binding("d", "disable", "禁用", show=False),
        Binding("r", "refresh", "刷新", show=False),
        Binding("i", "details", "详情", show=False),
        Binding("escape", "close", "关闭", show=False),
    ]

    # Screen CSS is parsed as its own stylesheet, so it can only reference
    # variables that exist in the app/theme variable table. Standard Textual
    # theme names are used here (and pick up the app palette when theme.tcss
    # overrides those names); theme.tcss owns the conversation palette.
    CSS: ClassVar[str] = """
    PluginScreen {
        align: center middle;
    }

    #plugin-dialog {
        width: 90%;
        height: 80%;
        max-height: 34;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }

    #plugin-title {
        height: 1;
        color: $accent;
        text-style: bold;
    }

    #plugin-table {
        height: 1fr;
        margin-top: 1;
        background: $surface;
    }

    #plugin-health {
        height: auto;
        max-height: 2;
        color: $text-muted;
    }

    #plugin-detail {
        height: auto;
        max-height: 10;
        margin-top: 1;
        color: $text-muted;
    }

    #plugin-hint {
        height: 1;
        margin-top: 1;
        color: $text-muted;
        text-style: italic;
    }
    """

    def __init__(
        self,
        manager: PluginManager | None = None,
        *,
        health: HealthSnapshot | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        if manager is None:
            from deeppyro.plugins.manager import PluginManager as DefaultPluginManager

            manager = DefaultPluginManager()
        self._manager = manager
        self._health = health
        self._ids: list[str] = []

    @property
    def manager(self) -> PluginManager:
        """The manager this screen reads and mutates."""
        return self._manager

    # -- layout ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="plugin-dialog"):
            yield Static("外部插件", id="plugin-title", markup=False)
            yield Static("", id="plugin-health", markup=False)
            yield DataTable(id="plugin-table")
            yield Static("", id="plugin-detail", markup=False)
            yield Static(_KEY_HINT, id="plugin-hint", markup=False)

    def on_mount(self) -> None:
        self._render_health()
        self._load_rows()
        self.query_one("#plugin-table", DataTable).focus()

    def _render_health(self) -> None:
        """Show the shared diagnosis summary above the table."""
        widget = self.query_one("#plugin-health", Static)
        if self._health is None:
            widget.update("")
            widget.display = False
            return
        lines = [self._health.summary()]
        lines.extend(f"扫描错误: {error}" for error in self._health.errors[:2])
        widget.update("\n".join(lines))
        widget.display = True

    def _health_record(self, plugin_id: str) -> PluginHealth | None:
        if self._health is None:
            return None
        return self._health.get(plugin_id)

    def _health_records(self) -> tuple[PluginHealth, ...]:
        if self._health is None:
            return ()
        return self._health.records

    # -- table -----------------------------------------------------------

    def _load_rows(self, selected: str | None = None) -> None:
        table = self.query_one("#plugin-table", DataTable)
        table.clear(columns=True)
        self._ids.clear()
        table.add_columns(*_COLUMNS)
        infos = list(self._manager.list())
        for info in infos:
            record = self._health_record(info.id)
            table.add_row(
                info.name or info.id,
                info.version or "-",
                SOURCE_LABELS.get(info.source, info.source),
                self._status_text(info, record),
                self._summary_text(info, record),
                key=info.id,
            )
            self._ids.append(info.id)
        seen = set(self._ids)
        for record in self._health_records():
            if record.id in seen:
                continue
            table.add_row(
                record.id,
                record.version or "-",
                "插件包",
                record.state,
                record.summary,
                key=record.id,
            )
            self._ids.append(record.id)
        if not self._ids:
            self._update_detail(_EMPTY_HINT)
            return
        index = 0
        if selected is not None and selected in self._ids:
            index = self._ids.index(selected)
        table.move_cursor(row=index)

    @staticmethod
    def _status_text(info: PluginInfo, record: PluginHealth | None = None) -> str:
        if record is not None:
            return record.state
        if info.error:
            return "错误"
        return "启用" if info.enabled else "禁用"

    @staticmethod
    def _summary_text(info: PluginInfo, record: PluginHealth | None = None) -> str:
        if record is not None and not record.healthy:
            return record.summary
        if info.error:
            return info.error
        return info.description or "-"

    def _selected_id(self) -> str | None:
        table = self.query_one("#plugin-table", DataTable)
        row = table.cursor_row
        if 0 <= row < len(self._ids):
            return self._ids[row]
        return None

    # -- detail panel ----------------------------------------------------

    def _update_detail(self, text: str) -> None:
        self.query_one("#plugin-detail", Static).update(text)

    def _show_detail(self, info: PluginInfo, result: ValidationResult | None = None) -> None:
        source = SOURCE_LABELS.get(info.source, info.source)
        target = (result.target if result is not None else None) or info.target
        provides = (result.provides if result is not None else None) or info.provides
        lines = [
            f"标识: {info.id}",
            f"名称: {info.name}",
            f"版本: {info.version or '-'}",
            f"来源: {source}  ({info.origin or '-'})",
            f"状态: {self._status_text(info)}",
            f"说明: {info.description or '-'}",
            f"提供: {', '.join(provides) if provides else '-'}",
            f"目标: {target or '-'}",
        ]
        if info.error:
            lines.append(f"错误: {info.error}")
        if result is not None:
            if result.ok:
                lines.append("校验: 通过")
            else:
                lines.append(f"校验: 失败  {result.error or ''}".rstrip())
        record = self._health_record(info.id)
        if record is not None:
            lines.append(f"健康: {record.state}")
            for issue in record.issues:
                lines.append(f"  {issue.kind}: {issue.message}")
                if issue.remediation:
                    lines.append(f"  建议: {issue.remediation}")
        self._update_detail("\n".join(lines))

    def _show_health_detail(self, record: PluginHealth) -> None:
        lines = [
            f"标识: {record.id}",
            f"版本: {record.version or '-'}",
            f"目录: {record.directory or '-'}",
            f"状态: {record.state}",
        ]
        if record.ui_slots:
            lines.append(f"声明槽位: {', '.join(record.ui_slots)}")
        for issue in record.issues:
            lines.append(f"错误: {issue.kind}: {issue.message}")
            if issue.remediation:
                lines.append(f"建议: {issue.remediation}")
        self._update_detail("\n".join(lines))

    # -- actions ---------------------------------------------------------

    def action_enable(self) -> None:
        self._toggle(enable=True)

    def action_disable(self) -> None:
        self._toggle(enable=False)

    def _toggle(self, *, enable: bool) -> None:
        plugin_id = self._selected_id()
        if plugin_id is None:
            self.notify("没有可操作的插件", severity="warning")
            return
        try:
            info = self._manager.enable(plugin_id) if enable else self._manager.disable(plugin_id)
        except Exception as exc:  # noqa: BLE001 - every manager failure is reported
            self.notify(str(exc) or type(exc).__name__, severity="error")
            return
        self._load_rows(selected=plugin_id)
        self._show_detail(info)
        verb = "启用" if enable else "禁用"
        self.notify(f"已{verb}: {info.name or info.id}")

    def action_refresh(self) -> None:
        try:
            self._manager.refresh()
        except Exception as exc:  # noqa: BLE001 - refresh failures go to the notification bar
            self.notify(f"刷新失败: {exc}", severity="error")
            return
        self._load_rows()
        problems = len(getattr(self._manager, "errors", ()) or ())
        if problems:
            self.notify(f"刷新完成，{problems} 个来源读取失败", severity="warning")
        else:
            self.notify("插件列表已刷新")

    def action_details(self) -> None:
        plugin_id = self._selected_id()
        if plugin_id is None:
            self.notify("没有可查看的插件", severity="warning")
            return
        info = self._manager.get(plugin_id)
        if info is None:
            record = self._health_record(plugin_id)
            if record is not None:
                self._show_health_detail(record)
                return
            self.notify(f"插件 {plugin_id!r} 不存在", severity="error")
            return
        try:
            result = self._manager.validate(plugin_id)
        except Exception as exc:  # noqa: BLE001 - validation errors are display data
            self._show_detail(info)
            self.notify(f"校验失败: {exc}", severity="error")
            return
        self._show_detail(info, result)
        if not result.ok:
            self.notify(f"校验失败: {result.error or plugin_id}", severity="error")

    def action_close(self) -> None:
        self.dismiss(None)


__all__ = ["PluginScreen"]
