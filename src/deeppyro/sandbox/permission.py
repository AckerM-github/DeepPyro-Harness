"""Permission presets: named bundles of a sandbox mode and approval policy.

Mirrors dsh-permission-presets. The service owns three DSH base presets
(read-only, workspace-write, danger-full-access); "custom" is a derived,
read-only state shown when the two knobs do not match any preset. Switching a
preset writes only the knobs that actually change, and a switch back to the
current state writes nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from deeppyro.core.errors import ConfigError
from deeppyro.sandbox.approval import ApprovalService
from deeppyro.sandbox.policy import SandboxPolicyService
from deeppyro.sandbox.types import (
    APPROVALS,
    CUSTOM_PERMISSION_PRESET,
    DEFAULT_PERMISSION_PRESET,
    DEFAULT_PERMISSION_PRESETS,
    PRESETS,
    PermissionPresetSpec,
)

PERMISSION_PRESET_EVENT = "permission/preset"


class PermissionPresetsService:
    """Catalog, derive and apply named permission presets."""

    def __init__(
        self,
        presets: Iterable[PermissionPresetSpec] | None = None,
        default: str = DEFAULT_PERMISSION_PRESET,
        *,
        policy_service: SandboxPolicyService | None = None,
        approval_service: ApprovalService | None = None,
    ) -> None:
        specs = tuple(presets) if presets is not None else DEFAULT_PERMISSION_PRESETS
        table: dict[str, PermissionPresetSpec] = {}
        for spec in specs:
            _validate(spec)
            table[spec.name] = spec
        if default not in table:
            raise ConfigError(f"unknown default permission preset: {default!r}")
        self._presets = table
        self._default = default
        self._policy_service = policy_service
        self._approval_service = approval_service

    # -- catalog ---------------------------------------------------------
    @property
    def default(self) -> str:
        return self._default

    def names(self) -> tuple[str, ...]:
        """The switchable preset names, in registration order."""
        return tuple(self._presets)

    def catalog(self) -> tuple[PermissionPresetSpec, ...]:
        return tuple(self._presets.values())

    def get(self, name: str) -> PermissionPresetSpec:
        try:
            return self._presets[name]
        except KeyError:
            raise ConfigError(f'unknown preset "{name}" (available: {", ".join(self.names())})') from None

    # -- current state ---------------------------------------------------
    def last_preset(self, session: Any | None = None) -> str | None:
        for record in reversed(getattr(session, "records", ()) or ()):
            if getattr(record, "type", None) != PERMISSION_PRESET_EVENT:
                continue
            data = getattr(record, "data", None) or {}
            preset = data.get("preset") if isinstance(data, dict) else None
            if isinstance(preset, str) and preset:
                return preset
            raise ConfigError(f"invalid permission/preset record: {preset!r}")
        return None

    def derive(self, session: Any | None = None) -> str:
        """Return the preset matching the current knobs, or "custom"."""
        sandbox = self._sandbox_mode(session)
        approval = self._approval_policy(session)
        last = self.last_preset(session)
        if last is not None:
            spec = self._presets.get(last)
            if spec is not None and spec.sandbox == sandbox and spec.approval == approval:
                return last
        for spec in self._presets.values():
            if spec.sandbox == sandbox and spec.approval == approval:
                return spec.name
        return CUSTOM_PERMISSION_PRESET

    def current(self, session: Any | None = None) -> str:
        return self.derive(session)

    def is_custom(self, session: Any | None = None) -> bool:
        return self.derive(session) == CUSTOM_PERMISSION_PRESET

    # -- switching -------------------------------------------------------
    def apply(self, session: Any, name: str) -> str:
        """Switch to one preset, writing only the knobs that change."""
        if name == CUSTOM_PERMISSION_PRESET:
            raise ConfigError("custom permission is derived and cannot be selected")
        spec = self.get(name)
        if self.current(session) != name:
            session.append(PERMISSION_PRESET_EVENT, {"preset": name})
        self._set_sandbox(session, spec.sandbox)
        self._set_approval(session, spec.approval)
        return name

    def pin_initial(self, session: Any) -> str:
        """Seed a fresh session with the default preset and its knobs.

        DSH pins the initial permission at agent-created time. Sessions that
        already carry equivalent records keep them; only missing events are
        appended.
        """
        if self.last_preset(session) is not None:
            return self.derive(session)
        spec = self._presets[self._default]
        if self._sandbox_mode(session) is None:
            self._set_sandbox(session, spec.sandbox)
        if self._approval_policy(session) is None:
            self._set_approval(session, spec.approval)
        session.append(PERMISSION_PRESET_EVENT, {"preset": spec.name})
        return spec.name

    # -- internals -------------------------------------------------------
    def _sandbox_mode(self, session: Any | None) -> str | None:
        if self._policy_service is None:
            return None
        return self._policy_service.last_mode(session)

    def _approval_policy(self, session: Any | None) -> str | None:
        if self._approval_service is None:
            return None
        return self._approval_service.last_policy(session)

    def _set_sandbox(self, session: Any, mode: str) -> None:
        if self._policy_service is not None:
            self._policy_service.set_mode(session, mode)

    def _set_approval(self, session: Any, policy: str) -> None:
        if self._approval_service is not None:
            self._approval_service.set_policy(session, policy)


def _validate(spec: PermissionPresetSpec) -> None:
    if not spec.name:
        raise ConfigError("permission preset name must not be empty")
    if spec.name == CUSTOM_PERMISSION_PRESET:
        raise ConfigError('"custom" is reserved for the derived permission state')
    if spec.sandbox not in PRESETS:
        raise ConfigError(f"invalid sandbox mode for permission preset {spec.name!r}: {spec.sandbox!r}")
    if spec.approval not in APPROVALS:
        raise ConfigError(f"invalid approval policy for permission preset {spec.name!r}: {spec.approval!r}")


__all__ = ["PERMISSION_PRESET_EVENT", "PermissionPresetsService"]
