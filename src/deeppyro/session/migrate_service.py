"""Kernel service exposing session migration to the TUI and plugins."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from deeppyro import paths
from deeppyro.api import PluginConfig, PluginContext, Service
from deeppyro.config import session_dir
from deeppyro.kernel.config_service import KernelConfig
from deeppyro.migrate import MigrationReport, SessionFormat, run_migration

__all__ = ["SessionMigratePlugin"]


class SessionMigratePlugin(Service):
    """Convert session logs between the v4 and native schema 1 formats."""

    provide = "session_migrate"
    inject: ClassVar[list[str]] = ["config"]
    plugin_id = "session-migrate"
    version = "0.2.0"
    description = "session format migration between v4 and native schema 1"

    def __init__(self, ctx: PluginContext, config: PluginConfig | None = None) -> None:
        super().__init__(ctx, config)
        cfg = ctx.require(KernelConfig).value
        self.native_root: Path = paths.sessions_dir()
        self.v4_root: Path = session_dir(cfg)

    def migrate(
        self,
        *,
        to: SessionFormat | str,
        session: str | None = None,
        all: bool = False,
        dry_run: bool = False,
        backup: bool = True,
        yes: bool = False,
    ) -> MigrationReport:
        """Run a migration using this plugin's session roots."""
        return run_migration(
            to=to,
            session=session,
            all=all,
            dry_run=dry_run,
            backup=backup,
            yes=yes,
            native_root=self.native_root,
            v4_root=self.v4_root,
        )
