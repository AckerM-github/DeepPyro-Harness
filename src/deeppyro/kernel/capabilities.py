"""Kernel capability registry: derived manifests, dependency checks, reports.

Every composition row and every host-style plugin mounted through
``ctx.mount`` contributes a :class:`~deeppyro.api.capabilities.CapabilityManifest`
to the registry.  The kernel uses it at boot to reject duplicate service
providers (naming both plugin ids) and version-mismatched requirements, and to
report unresolved requirements as diagnostics (a missing dependency leaves the
dependent fiber PENDING rather than aborting the whole boot, see
`deeppyro.kernel.fiber`).  The plugin CLI renders the same data.

The registry also snapshots two kinds of host facts that are not capability
declarations but affect what a boot can promise:

* bundle digests: the sha256 of every bundle.toml the default composition
  loaded, plus the optional `[bundle].digest` pin.  An unpinned bundle is
  recorded as ``unverified``; a wrong or malformed pin is a fatal issue (see
  docs/guides/bundle-digest.md).
* the host sandbox boundary: which enforcement level the local sandbox backend
  can actually deliver (see docs/guides/windows-sandbox-boundaries.md).  The
  live per-command value still comes from
  `deeppyro.sandbox.provider.Confinement`; this record only makes the host-wide
  limit visible to capability report consumers.
"""

from __future__ import annotations

import hashlib
import os
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from deeppyro.api.capabilities import CapabilityManifest, build_manifest
from deeppyro.kernel.bundle_schema import DIGEST_PATTERN, DIGEST_PREFIX
from deeppyro.kernel.composition import (
    Composition,
    PluginRow,
    resolve_row,
    version_satisfies,
)


class CapabilityIssueKind(StrEnum):
    """What is wrong with the declared capability graph."""

    DUPLICATE_PROVIDE = "duplicate-provide"
    MISSING_REQUIRE = "missing-require"
    PENDING_PROVIDER = "pending-provider"
    VERSION_MISMATCH = "version-mismatch"
    INVALID_MANIFEST = "invalid-manifest"
    BUNDLE_DIGEST_MISMATCH = "bundle-digest-mismatch"
    INVALID_BUNDLE_DIGEST = "invalid-bundle-digest"


#: Issue kinds that refuse boot: two live providers of one service, an
#: unsatisfiable version requirement, or a bundle whose bytes do not match the
#: digest it declares.
FATAL_ISSUES: frozenset[CapabilityIssueKind] = frozenset(
    {
        CapabilityIssueKind.DUPLICATE_PROVIDE,
        CapabilityIssueKind.VERSION_MISMATCH,
        CapabilityIssueKind.BUNDLE_DIGEST_MISMATCH,
        CapabilityIssueKind.INVALID_BUNDLE_DIGEST,
    }
)

#: Services the kernel provides before any composition row is mounted.
IMPLICIT_SERVICES: frozenset[str] = frozenset({"config"})

#: Substitute written before hashing so a bundle.toml can pin its own digest.
#: Same length as a real digest ("sha256:" + 64 hex chars), so the substitution
#: never shifts the rest of the file.
DIGEST_PLACEHOLDER = DIGEST_PREFIX + "0" * 64

ENTRY_ENABLED = "enabled"
ENTRY_DISABLED = "disabled"
ENTRY_GROUP = "group"
ENTRY_RUNTIME = "runtime"


@dataclass(frozen=True, slots=True)
class CapabilityIssue:
    """One diagnostic about the declared capability graph."""

    kind: CapabilityIssueKind
    plugin_id: str
    message: str
    related: tuple[str, ...] = ()

    @property
    def fatal(self) -> bool:
        """Whether this issue refuses boot."""
        return self.kind in FATAL_ISSUES

    def to_dict(self) -> dict[str, Any]:
        """Render as plain JSON-compatible data."""
        return {
            "kind": str(self.kind),
            "pluginId": self.plugin_id,
            "message": self.message,
            "related": list(self.related),
            "fatal": self.fatal,
        }


@dataclass(frozen=True, slots=True)
class CapabilityEntry:
    """One manifest registered with the kernel plus its mount bookkeeping."""

    plugin_id: str
    manifest: CapabilityManifest
    origin: str = ""
    state: str = ENTRY_ENABLED
    isolate: tuple[str, ...] = ()
    active: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Render as plain JSON-compatible data."""
        return {
            "pluginId": self.plugin_id,
            "origin": self.origin,
            "state": self.state,
            "isolate": list(self.isolate),
            "active": self.active,
            "manifest": self.manifest.to_dict(),
        }


class BundleDigestState(StrEnum):
    """How a loaded bundle's bytes compare with its optional digest pin."""

    #: No ``[bundle].digest`` was declared; the hash is recorded but never blocks.
    UNVERIFIED = "unverified"
    #: The declared digest matches the normalised bytes.
    MATCH = "match"
    #: The declared digest is valid but describes different bytes.
    MISMATCH = "mismatch"
    #: The declared digest is not ``sha256:`` plus 64 lowercase hex characters.
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class BundleDigest:
    """The sha256 of one loaded bundle.toml plus its optional declared pin."""

    path: str
    bundle_id: str = ""
    sha256: str = ""
    declared: str | None = None
    state: BundleDigestState = BundleDigestState.UNVERIFIED

    def to_dict(self) -> dict[str, Any]:
        """Render as plain JSON-compatible data."""
        return {
            "path": self.path,
            "bundleId": self.bundle_id,
            "sha256": self.sha256,
            "declared": self.declared,
            "state": str(self.state),
        }


@dataclass(frozen=True, slots=True)
class SandboxBoundary:
    """Host-wide sandbox enforcement facts that capability consumers can trust.

    ``enforcement`` mirrors ``Confinement.enforcement`` from
    `deeppyro.sandbox.provider`: ``full`` (OS-enforced), ``partial`` (Windows
    ACL restricted token: file writes only) or ``unavailable`` (no backend, so
    ``read-only``/``workspace-write`` fail closed).  ``reason`` is a short
    plain-text justification for the value.
    """

    platform: str
    enforcement: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Render as plain JSON-compatible data."""
        return {
            "platform": self.platform,
            "enforcement": self.enforcement,
            "reason": self.reason,
        }


def host_sandbox_boundary() -> SandboxBoundary:
    """Describe this host's local sandbox backend without probing it.

    Values mirror `deeppyro.sandbox.provider.LocalSandboxProvider`: Windows
    reports ``partial`` and every other platform reports ``unavailable``.
    This is a report fact, not a policy switch: per-command enforcement still
    comes from the sandbox plugin's live ``Confinement``.
    """
    if os.name == "nt":
        return SandboxBoundary(
            platform="windows",
            enforcement="partial",
            reason=(
                "the Windows ACL restricted-token backend confines file writes only: "
                "workspace and private-temp writes are limited by ACLs granted to a synthetic "
                "SID, while network access, reads, and process/registry operations are not "
                "constrained; danger-full-access runs unsandboxed"
            ),
        )
    return SandboxBoundary(
        platform=os.name,
        enforcement="unavailable",
        reason=(
            "no local sandbox backend is implemented on this platform, so read-only and "
            "workspace-write fail closed; only danger-full-access runs, unsandboxed"
        ),
    )


def _bundle_toml_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate / "bundle.toml" if candidate.is_dir() else candidate


def bundle_digest_bytes(data: bytes, *, declared: str | None = None) -> str:
    """sha256 of bundle.toml bytes, normalising a valid digest pin to a placeholder.

    The hash always covers the bytes as parsed from disk (BOM included) and TOML
    is never re-serialised, so comments, key order and whitespace are part of the
    hash.  When ``declared`` is syntactically valid its first verbatim
    occurrence is replaced by :data:`DIGEST_PLACEHOLDER` before hashing: without
    that substitution a pinned file could never match, because the digest would
    have to contain itself.  See docs/guides/bundle-digest.md.
    """
    payload = data
    if declared is not None and DIGEST_PATTERN.fullmatch(declared):
        needle = declared.encode("ascii")
        if needle in payload:
            payload = payload.replace(needle, DIGEST_PLACEHOLDER.encode("ascii"), 1)
    return hashlib.sha256(payload).hexdigest()


def _read_digest_declaration(data: bytes) -> tuple[str | None, str]:
    """Return (declared digest or None, bundle id) from raw bundle.toml bytes."""
    payload = data[3:] if data.startswith(b"\xef\xbb\xbf") else data
    try:
        document = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None, ""
    table = document.get("bundle")
    if not isinstance(table, dict):
        return None, ""
    raw_id = table.get("id")
    bundle_id = raw_id.strip() if isinstance(raw_id, str) else ""
    raw_digest = table.get("digest")
    if not isinstance(raw_digest, str):
        return None, bundle_id
    return raw_digest.strip(), bundle_id


def inspect_bundle_digest(path: str | Path) -> BundleDigest:
    """Read one bundle.toml and record its hash plus declared-digest state.

    Accepts a bundle.toml file or a directory containing it.  An unreadable or
    unparseable file is reported as ``unverified`` with an empty ``sha256``
    instead of raising: composition load already reports the real error (a
    warning for optional bundles, a boot error for the core bundle).
    """
    bundle_path = _bundle_toml_path(path)
    try:
        data = bundle_path.read_bytes()
    except OSError:
        return BundleDigest(path=str(bundle_path))
    declared, bundle_id = _read_digest_declaration(data)
    sha256 = bundle_digest_bytes(data, declared=declared)
    if declared is None:
        state = BundleDigestState.UNVERIFIED
    elif not DIGEST_PATTERN.fullmatch(declared):
        state = BundleDigestState.INVALID
    elif f"{DIGEST_PREFIX}{sha256}" == declared:
        state = BundleDigestState.MATCH
    else:
        state = BundleDigestState.MISMATCH
    return BundleDigest(
        path=str(bundle_path),
        bundle_id=bundle_id,
        sha256=sha256,
        declared=declared,
        state=state,
    )


def inspect_bundle_digests(paths: Iterable[str | Path]) -> tuple[BundleDigest, ...]:
    """Inspect every path that resolves to an existing bundle.toml."""
    digests: list[BundleDigest] = []
    for path in paths:
        bundle_path = _bundle_toml_path(path)
        if not bundle_path.is_file():
            continue
        digests.append(inspect_bundle_digest(bundle_path))
    return tuple(digests)


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    """Immutable snapshot of the registry: entries, issues and dependency trees."""

    entries: tuple[CapabilityEntry, ...] = ()
    issues: tuple[CapabilityIssue, ...] = ()
    available_services: frozenset[str] = frozenset()
    bundles: tuple[BundleDigest, ...] = ()
    sandbox: SandboxBoundary = field(default_factory=host_sandbox_boundary)

    @property
    def ok(self) -> bool:
        """Whether the graph has no fatal issue."""
        return not any(issue.fatal for issue in self.issues)

    @property
    def fatal_issues(self) -> tuple[CapabilityIssue, ...]:
        """Issues that make the composition invalid."""
        return tuple(issue for issue in self.issues if issue.fatal)

    def entry(self, plugin_id: str) -> CapabilityEntry | None:
        """The first enabled/runtime entry for `plugin_id`, if registered."""
        for wanted in (ENTRY_ENABLED, ENTRY_RUNTIME):
            for entry in self.entries:
                if entry.plugin_id == plugin_id and entry.state == wanted:
                    return entry
        for entry in self.entries:
            if entry.plugin_id == plugin_id:
                return entry
        return None

    def entries_for(self, plugin_id: str) -> tuple[CapabilityEntry, ...]:
        """Every entry registered for `plugin_id`."""
        return tuple(entry for entry in self.entries if entry.plugin_id == plugin_id)

    def issues_for(self, plugin_id: str) -> tuple[CapabilityIssue, ...]:
        """Issues attached to `plugin_id` (as the plugin or a related provider)."""
        return tuple(
            issue for issue in self.issues if issue.plugin_id == plugin_id or plugin_id in issue.related
        )

    def provider(self, service: str) -> CapabilityEntry | None:
        """The enabled/runtime entry providing `service`, if any."""
        for entry in self.entries:
            if entry.state in (ENTRY_ENABLED, ENTRY_RUNTIME) and service in entry.manifest.provides:
                return entry
        return None

    def bundle(self, identifier: str) -> BundleDigest | None:
        """Recorded digest for a bundle path or bundle id, if inspected."""
        for digest in self.bundles:
            if identifier in (digest.path, digest.bundle_id):
                return digest
        return None

    def missing_dependencies(self, plugin_id: str) -> tuple[str, ...]:
        """Required service names of `plugin_id` that no live or declared entry provides."""
        entry = self.entry(plugin_id)
        if entry is None:
            return ()
        provided = set(entry.manifest.provides) | set(self.available_services)
        declared = {service for item in self.entries for service in item.manifest.provides}
        missing = [
            service
            for service in entry.manifest.requires
            if service not in provided and service not in declared
        ]
        return tuple(missing)

    def dependency_tree(self, plugin_id: str) -> dict[str, Any]:
        """Nested dependency tree rooted at `plugin_id` (cycles are cut)."""
        return self._tree(plugin_id, frozenset())

    def _tree(self, plugin_id: str, seen: frozenset[str]) -> dict[str, Any]:
        entry = self.entry(plugin_id)
        if entry is None:
            return {
                "pluginId": plugin_id,
                "version": None,
                "provides": [],
                "requires": [],
                "missing": True,
            }
        manifest = entry.manifest
        requires: list[dict[str, Any]] = []
        for service in manifest.requires:
            constraint = manifest.version_requirement(service)
            provider = self.provider(service)
            node: dict[str, Any] = {
                "service": service,
                "versionConstraint": constraint or None,
                "provider": None,
                "providerVersion": None,
                "satisfied": service in manifest.provides or service in self.available_services,
                "requires": [],
            }
            if provider is not None:
                version_ok = (
                    True if not constraint else version_satisfies(provider.manifest.version, constraint)
                )
                node["provider"] = provider.plugin_id
                node["providerVersion"] = provider.manifest.version
                node["satisfied"] = bool(node["satisfied"] or version_ok)
                if version_ok and provider.plugin_id not in seen:
                    nested = self._tree(provider.plugin_id, seen | {plugin_id})
                    node["requires"] = nested["requires"]
            elif node["satisfied"]:
                node["provider"] = "<host>"
            requires.append(node)
        return {
            "pluginId": entry.plugin_id,
            "version": manifest.version,
            "provides": list(manifest.provides),
            "requires": requires,
            "missing": False,
        }

    def to_dict(self) -> dict[str, Any]:
        """Render the whole report as plain JSON-compatible data."""
        return {
            "ok": self.ok,
            "issues": [issue.to_dict() for issue in self.issues],
            "manifests": [
                {
                    "pluginId": entry.plugin_id,
                    "origin": entry.origin,
                    "state": entry.state,
                    "isolate": list(entry.isolate),
                    "active": entry.active,
                    "manifest": entry.manifest.to_dict(),
                }
                for entry in self.entries
            ],
            "bundles": [digest.to_dict() for digest in self.bundles],
            "sandbox": self.sandbox.to_dict(),
        }


class CapabilityRegistry:
    """Mutable registry the kernel fills while booting and mounting plugins."""

    def __init__(self) -> None:
        self._entries: list[CapabilityEntry] = []
        self._issues: list[CapabilityIssue] = []
        self._bundles: list[BundleDigest] = []

    @property
    def entries(self) -> tuple[CapabilityEntry, ...]:
        """Registered entries in registration order."""
        return tuple(self._entries)

    @property
    def issues(self) -> tuple[CapabilityIssue, ...]:
        """Issues discovered while indexing the composition."""
        return tuple(self._issues)

    @property
    def bundles(self) -> tuple[BundleDigest, ...]:
        """Recorded bundle.toml digests in recording order."""
        return tuple(self._bundles)

    def register(
        self,
        manifest: CapabilityManifest,
        *,
        plugin_id: str = "",
        origin: str = "",
        state: str = ENTRY_ENABLED,
        isolate: Iterable[str] = (),
        active: bool = False,
    ) -> CapabilityEntry:
        """Add one manifest to the registry."""
        entry = CapabilityEntry(
            plugin_id=plugin_id or manifest.plugin_id,
            manifest=manifest,
            origin=origin,
            state=state,
            isolate=tuple(isolate),
            active=active,
        )
        self._entries.append(entry)
        return entry

    def index_composition(self, composition: Composition) -> list[CapabilityIssue]:
        """Register every resolvable composition row (isolate path preserved).

        Rows that cannot be resolved are skipped: an enabled broken row still
        fails later through the normal mount path (with the original error),
        and a disabled/broken manifest row must not fail boot.  Enabled rows
        without a readable manifest produce an INVALID_MANIFEST diagnostic.
        """
        issues: list[CapabilityIssue] = []

        def walk(row: PluginRow, parent_isolate: tuple[str, ...], parent_disabled: bool) -> None:
            isolate = parent_isolate + tuple(row.isolate)
            disabled = parent_disabled or row.disabled is True
            if row.plugin:
                try:
                    manifest = build_manifest(resolve_row(row))
                except Exception as exc:  # noqa: BLE001 - reported as a diagnostic
                    if not disabled and not row.group:
                        issues.append(
                            CapabilityIssue(
                                kind=CapabilityIssueKind.INVALID_MANIFEST,
                                plugin_id=row.id,
                                message=(
                                    f"plugin {row.id!r} declares no readable capability manifest: "
                                    f"{type(exc).__name__}: {exc}"
                                ),
                                related=(row.plugin,),
                            )
                        )
                else:
                    state = ENTRY_DISABLED if disabled else (ENTRY_GROUP if row.group else ENTRY_ENABLED)
                    self.register(
                        manifest,
                        plugin_id=row.id,
                        origin=row.origin,
                        state=state,
                        isolate=isolate,
                        active=False,
                    )
            for child in row.children:
                walk(child, isolate, disabled)

        for row in composition.rows():
            walk(row, (), False)
        self._issues.extend(issues)
        return issues

    def mark_active(self, plugin_id: str, *, active: bool = True) -> None:
        """Mark the entries of `plugin_id` as live-mounted (or not)."""
        self._entries = [
            replace(entry, active=active)
            if entry.plugin_id == plugin_id and entry.active != active
            else entry
            for entry in self._entries
        ]

    def record_bundle_digests(self, digests: Iterable[BundleDigest]) -> list[CapabilityIssue]:
        """Record loaded bundle.toml digests and flag bad digest pins.

        A bundle without a declared digest is recorded as ``unverified`` and
        never blocks boot (backward compatible).  A valid digest that does not
        match the file bytes, or a malformed ``[bundle].digest`` value, adds a
        fatal issue so the caller's normal ``report().ok`` check refuses boot.
        """
        issues: list[CapabilityIssue] = []
        for digest in digests:
            self._bundles.append(digest)
            label = digest.bundle_id or digest.path
            if digest.state is BundleDigestState.MISMATCH:
                issue = CapabilityIssue(
                    kind=CapabilityIssueKind.BUNDLE_DIGEST_MISMATCH,
                    plugin_id=label,
                    message=(
                        f"bundle {label!r} at {digest.path} declares digest {digest.declared}, "
                        f"but its bundle.toml bytes hash to sha256:{digest.sha256}; refusing to "
                        "boot because the bundle changed after it was pinned"
                    ),
                    related=(digest.path,),
                )
            elif digest.state is BundleDigestState.INVALID:
                issue = CapabilityIssue(
                    kind=CapabilityIssueKind.INVALID_BUNDLE_DIGEST,
                    plugin_id=label,
                    message=(
                        f"bundle {label!r} at {digest.path} declares digest "
                        f"{digest.declared!r}; expected 'sha256:' followed by 64 lowercase hex "
                        "characters"
                    ),
                    related=(digest.path,),
                )
            else:
                continue
            self._issues.append(issue)
            issues.append(issue)
        return issues

    def report(self, *, available_services: Iterable[str] = ()) -> CapabilityReport:
        """Validate the declared graph and snapshot it."""
        entries = tuple(self._entries)
        available = frozenset(str(name) for name in available_services)
        issues: list[CapabilityIssue] = list(self._issues)
        issues.extend(_provider_issues(entries, available))
        return CapabilityReport(
            entries=entries,
            issues=tuple(issues),
            available_services=available,
            bundles=tuple(self._bundles),
        )


def _provider_issues(
    entries: tuple[CapabilityEntry, ...],
    available: frozenset[str],
) -> list[CapabilityIssue]:
    issues: list[CapabilityIssue] = []
    enabled = [entry for entry in entries if entry.state == ENTRY_ENABLED]
    runtime = [entry for entry in entries if entry.state == ENTRY_RUNTIME]

    # Providers visible to dependents: enabled rows and host-mounted plugins.
    live = enabled + runtime
    declared: dict[str, list[CapabilityEntry]] = {}
    for entry in entries:
        for service in entry.manifest.provides:
            declared.setdefault(service, []).append(entry)
    providers: dict[str, list[CapabilityEntry]] = {}
    for entry in live:
        for service in entry.manifest.provides:
            providers.setdefault(service, []).append(entry)

    # Duplicate providers: same service within the same isolate scope.
    buckets: dict[tuple[str, tuple[str, ...]], list[CapabilityEntry]] = {}
    for entry in enabled:
        for service in entry.manifest.provides:
            buckets.setdefault((service, entry.isolate), []).append(entry)
    for (service, isolate), owners in buckets.items():
        if len(owners) < 2:
            continue
        ids = [owner.plugin_id for owner in owners]
        scope = f" in isolate scope {list(isolate)!r}" if isolate else ""
        issues.append(
            CapabilityIssue(
                kind=CapabilityIssueKind.DUPLICATE_PROVIDE,
                plugin_id=ids[0],
                message=(
                    f"service {service!r} is provided by more than one plugin{scope}: " + ", ".join(ids)
                ),
                related=tuple(ids[1:]),
            )
        )

    for entry in enabled:
        manifest = entry.manifest
        for service in manifest.requires:
            if service in manifest.provides or service in available:
                continue
            constraint = manifest.version_requirement(service)
            candidates = providers.get(service, [])
            if candidates:
                if not constraint:
                    continue
                matching = [
                    candidate
                    for candidate in candidates
                    if version_satisfies(candidate.manifest.version, constraint)
                ]
                if matching:
                    continue
                offered = ", ".join(
                    f"{candidate.plugin_id} {candidate.manifest.version}" for candidate in candidates
                )
                issues.append(
                    CapabilityIssue(
                        kind=CapabilityIssueKind.VERSION_MISMATCH,
                        plugin_id=entry.plugin_id,
                        message=(
                            f"plugin {entry.plugin_id!r} requires service {service!r} {constraint}, "
                            f"but the available providers do not satisfy it: {offered}"
                        ),
                        related=tuple(candidate.plugin_id for candidate in candidates),
                    )
                )
                continue
            pending = declared.get(service, [])
            if pending:
                ids = [candidate.plugin_id for candidate in pending]
                issues.append(
                    CapabilityIssue(
                        kind=CapabilityIssueKind.PENDING_PROVIDER,
                        plugin_id=entry.plugin_id,
                        message=(
                            f"plugin {entry.plugin_id!r} requires service {service!r}, whose provider(s) "
                            f"are not active: {', '.join(ids)}"
                        ),
                        related=tuple(ids),
                    )
                )
                continue
            issues.append(
                CapabilityIssue(
                    kind=CapabilityIssueKind.MISSING_REQUIRE,
                    plugin_id=entry.plugin_id,
                    message=(
                        f"plugin {entry.plugin_id!r} requires service {service!r}, but no plugin in this "
                        "composition provides it"
                    ),
                )
            )
    return issues


__all__ = [
    "DIGEST_PLACEHOLDER",
    "ENTRY_DISABLED",
    "ENTRY_ENABLED",
    "ENTRY_GROUP",
    "ENTRY_RUNTIME",
    "FATAL_ISSUES",
    "IMPLICIT_SERVICES",
    "BundleDigest",
    "BundleDigestState",
    "CapabilityEntry",
    "CapabilityIssue",
    "CapabilityIssueKind",
    "CapabilityRegistry",
    "CapabilityReport",
    "SandboxBoundary",
    "bundle_digest_bytes",
    "host_sandbox_boundary",
    "inspect_bundle_digest",
    "inspect_bundle_digests",
]
