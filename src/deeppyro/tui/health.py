"""Plugin health snapshots shared by the TUI and the CLI diagnosis.

The TUI must not re-implement plugin preflight checks. Every record here is
produced by :func:`deeppyro.plugins.cli.diagnose`, the same function behind
``dpyro plugin doctor``, so the two frontends can never disagree about what is
broken or how to fix it. This module is Textual-free and safe to import in
headless sessions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from deeppyro.plugins.bootstrap import PluginPackage, scan_plugin_packages
from deeppyro.plugins.cli import diagnose
from deeppyro.plugins.metadata import KNOWN_SLOT_KINDS, MetadataError

STATUS_OK: Final = "ok"
STATUS_FAILED: Final = "failed"


@dataclass(frozen=True, slots=True)
class HealthIssue:
    """One preflight failure and its remediation hint."""

    kind: str
    message: str
    remediation: str = ""

    @property
    def line(self) -> str:
        """Single-line rendering used by the `/plugins` detail pane."""
        if self.remediation:
            return f"{self.kind}: {self.message} - {self.remediation}"
        return f"{self.kind}: {self.message}"


@dataclass(frozen=True, slots=True)
class PluginHealth:
    """Health of one installed plugin package."""

    id: str
    version: str = ""
    directory: str = ""
    issues: tuple[HealthIssue, ...] = ()
    ui_slots: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return not self.issues

    @property
    def state(self) -> str:
        """``ok`` or the first failure kind (e.g. ``metadata-invalid``)."""
        return STATUS_OK if self.healthy else self.issues[0].kind

    @property
    def summary(self) -> str:
        if self.healthy:
            return "ok"
        return self.issues[0].message

    def issue_lines(self) -> tuple[str, ...]:
        return tuple(issue.line for issue in self.issues)


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """Health of every scanned package, plus scan-level errors."""

    records: tuple[PluginHealth, ...] = ()
    errors: tuple[str, ...] = ()
    _by_id: dict[str, PluginHealth] = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_id", {record.id: record for record in self.records})

    @property
    def ok_count(self) -> int:
        return sum(1 for record in self.records if record.healthy)

    @property
    def failed_count(self) -> int:
        return sum(1 for record in self.records if not record.healthy)

    @property
    def healthy(self) -> bool:
        return not self.errors and self.failed_count == 0

    def get(self, plugin_id: str) -> PluginHealth | None:
        return self._by_id.get(plugin_id)

    def summary(self) -> str:
        text = f"健康检查: {self.ok_count} 正常 / {self.failed_count} 失败"
        if self.errors:
            text += f" · 扫描错误 {len(self.errors)}"
        return text


def _ui_slots(package: PluginPackage) -> tuple[str, ...]:
    metadata = package.metadata
    if metadata is None:
        return ()
    ui = getattr(metadata, "ui", None)
    slots = getattr(ui, "slots", ()) if ui is not None else ()
    return tuple(str(slot) for slot in (slots or ()))


def health_for(package: PluginPackage) -> PluginHealth:
    """Build one health record using the shared CLI diagnosis."""
    errors = diagnose(package)
    issues = tuple(
        HealthIssue(kind=error.kind, message=error.message, remediation=error.remediation) for error in errors
    )
    return PluginHealth(
        id=package.id,
        version=package.version or "",
        directory=str(package.directory),
        issues=issues,
        ui_slots=_ui_slots(package),
    )


def scan_health(root: str | Path | None = None) -> HealthSnapshot:
    """Scan installed packages with slot-kind validation enabled.

    ``scan_plugin_packages`` only validates ``[ui].slots`` when ``known_slots``
    is supplied, so the TUI passes the shared whitelist. A broken scan becomes
    a snapshot error rather than an exception at the screen boundary.
    """
    errors: list[str] = []
    try:
        packages = scan_plugin_packages(root, known_slots=KNOWN_SLOT_KINDS)
    except Exception as exc:  # noqa: BLE001 - a scan failure must not break /plugins
        return HealthSnapshot(errors=(f"{type(exc).__name__}: {exc}",))
    records: list[PluginHealth] = []
    for package in packages:
        try:
            records.append(health_for(package))
        except Exception as exc:  # noqa: BLE001 - one package cannot hide the rest
            errors.append(f"{getattr(package, 'id', '?')}: {type(exc).__name__}: {exc}")
    records.sort(key=lambda record: record.id)
    return HealthSnapshot(records=tuple(records), errors=tuple(errors))


def issue_from_error(error: MetadataError) -> HealthIssue:
    """Convert a metadata error (helper for callers that already have one)."""
    return HealthIssue(kind=error.kind, message=error.message, remediation=error.remediation)


def snapshot_lines(snapshot: HealthSnapshot | None) -> list[str]:
    """Plain-text lines for embedding health in a screen detail pane."""
    if snapshot is None:
        return ["健康检查: 未运行"]
    lines = [snapshot.summary()]
    lines.extend(f"扫描错误: {error}" for error in snapshot.errors)
    return lines


__all__: list[str] = [
    "STATUS_FAILED",
    "STATUS_OK",
    "HealthIssue",
    "HealthSnapshot",
    "PluginHealth",
    "health_for",
    "issue_from_error",
    "scan_health",
    "snapshot_lines",
]
