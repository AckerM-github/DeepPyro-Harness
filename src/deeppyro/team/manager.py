"""Native-only agent team service.

The v4 session-log recorder path was deleted together with the v4 session
stack, so the lead-owned team always runs on the native session control plane.
``TeamService`` keeps its historical name because the plugin, web and TUI
bindings resolve the service by class and by the kernel's ``team`` name; it is
a thin subclass of :class:`~deeppyro.team.native_manager.NativeTeamManager`.
"""

from __future__ import annotations

from deeppyro.team.native_manager import NativeTeamManager

__all__ = ["TeamService"]


class TeamService(NativeTeamManager):
    """Lead-owned team: up to eight child agents plus a shared task board."""

    @property
    def _native(self) -> TeamService:
        """The native backend itself.

        Historical call sites (and the TUI suite) reached for the composed
        native manager through this attribute. Team operations now live on
        this instance, so the alias is read-only and returns ``self``; drop it
        once those call sites use the service directly.
        """
        return self
