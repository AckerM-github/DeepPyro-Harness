"""Approval modal with risk highlighting.

The app opens this screen from ask_approval() and waits for one of the three
decision constants: ALLOW_ONCE, ALLOW_ALWAYS or DENY. The screen shows the
request kind, the tool involved, a human summary, the command or path detail,
and a coloured risk badge. Command details that match the dangerous-command
patterns from the sandbox gate are shown with a red DANGER badge.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from deeppyro.sandbox.gate import DANGEROUS_PATTERNS

ALLOW_ONCE = "once"
ALLOW_ALWAYS = "always"
DENY = "deny"

RISK_DANGER = "danger"
RISK_WARN = "warn"

DETAIL_LIMIT = 500

_KIND_LABELS = {
    "command-approval": "shell command",
    "write-outside-workspace": "write outside workspace",
    "sandbox-escalation": "sandbox escalation",
}

_KIND_TOOLS = {
    "command-approval": "shell",
    "write-outside-workspace": "filesystem",
    "sandbox-escalation": "sandbox",
}


def _request_field(request: Any, name: str, default: str | None = "") -> str | None:
    if isinstance(request, Mapping):
        value = request.get(name, default)
    else:
        value = getattr(request, name, default)
    if isinstance(value, str):
        return value
    return default


def dangerous_reason(command: str) -> str | None:
    """Return the sandbox label of the first dangerous pattern in command."""
    for label, pattern in DANGEROUS_PATTERNS:
        if pattern.search(command):
            return label
    return None


def risk_level(request: Any) -> str:
    """Classify an approval request as RISK_DANGER or RISK_WARN."""
    kind = _request_field(request, "kind", "") or ""
    summary = _request_field(request, "summary", "") or ""
    detail = _request_field(request, "detail", "") or ""
    if kind == "command-approval" and dangerous_reason(detail):
        return RISK_DANGER
    if kind == "sandbox-escalation" and "danger-full-access" in summary.lower():
        return RISK_DANGER
    return RISK_WARN


class ApprovalScreen(ModalScreen[str]):
    """Decision dialog for one ApprovalRequest."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "deny", "Deny", show=False),
        Binding("n", "deny", "Deny", show=False),
        Binding("y", "allow_once", "Allow once", show=False),
        Binding("a", "allow_always", "Always allow", show=False),
    ]

    DEFAULT_CSS = """
    ApprovalScreen {
        align: center middle;
        background: $background 70%;
    }
    #approval-dialog {
        width: 78;
        height: auto;
        max-height: 24;
        border: round $warning;
        background: $panel;
        padding: 1 2;
    }
    #approval-title {
        color: $text-warning;
        text-style: bold;
    }
    #approval-tool {
        color: $text-muted;
        margin: 1 0 0 0;
    }
    #approval-risk {
        text-style: bold;
        margin: 1 0 0 0;
    }
    #approval-risk.risk-danger {
        color: $text-error;
    }
    #approval-risk.risk-warn {
        color: $text-warning;
    }
    #approval-summary {
        color: $text;
        margin: 1 0 0 0;
    }
    #approval-detail {
        color: $text-muted;
        margin: 1 0 0 0;
    }
    #approval-buttons {
        height: auto;
        margin: 1 0 0 0;
        align-horizontal: right;
    }
    #approval-buttons Button {
        margin-left: 2;
    }
    """

    def __init__(self, request: Any) -> None:
        super().__init__()
        self.request = request
        self.kind = _request_field(request, "kind", "") or ""
        self.summary = _request_field(request, "summary", "") or ""
        self.detail = _request_field(request, "detail", "") or ""
        self.tool = _request_field(request, "tool", None)
        self.risk = risk_level(request)
        self.danger_reason = self._classify_danger()

    def _classify_danger(self) -> str | None:
        if self.kind == "command-approval":
            return dangerous_reason(self.detail)
        if self.kind == "sandbox-escalation" and "danger-full-access" in self.summary.lower():
            return "danger-full-access"
        return None

    def _title_text(self) -> str:
        label = _KIND_LABELS.get(self.kind, self.kind or "request")
        return "Approval required · " + label

    def _tool_text(self) -> str:
        tool = self.tool or _KIND_TOOLS.get(self.kind, "request")
        return "Tool: " + tool

    def _badge_text(self) -> str:
        if self.risk == RISK_DANGER and self.danger_reason:
            return "DANGER · " + self.danger_reason
        if self.risk == RISK_DANGER:
            return "DANGER"
        return "WARNING"

    def _detail_text(self) -> str:
        if len(self.detail) <= DETAIL_LIMIT:
            return self.detail
        return self.detail[: DETAIL_LIMIT - 1] + "…"

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-dialog"):
            yield Static(self._title_text(), id="approval-title")
            yield Static(self._tool_text(), id="approval-tool")
            yield Static(
                self._badge_text(),
                id="approval-risk",
                classes="risk-" + self.risk,
            )
            yield Static(self.summary, id="approval-summary")
            if self.detail:
                yield Static(self._detail_text(), id="approval-detail")
            with Horizontal(id="approval-buttons"):
                yield Button("Allow once", id="allow-once", variant="success")
                yield Button("Always allow", id="allow-always", variant="warning")
                yield Button("Deny", id="deny", variant="error")

    def on_mount(self) -> None:
        self.query_one("#allow-once", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        decisions = {
            "allow-once": ALLOW_ONCE,
            "allow-always": ALLOW_ALWAYS,
            "deny": DENY,
        }
        decision = decisions.get(event.button.id or "")
        if decision is None:
            return
        event.stop()
        self.dismiss(decision)

    def action_allow_once(self) -> None:
        self.dismiss(ALLOW_ONCE)

    def action_allow_always(self) -> None:
        self.dismiss(ALLOW_ALWAYS)

    def action_deny(self) -> None:
        self.dismiss(DENY)


__all__ = [
    "ALLOW_ALWAYS",
    "ALLOW_ONCE",
    "DENY",
    "RISK_DANGER",
    "RISK_WARN",
    "ApprovalScreen",
    "dangerous_reason",
    "risk_level",
]
