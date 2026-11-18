"""``dpyro plugin`` — inspect, diagnose and install plugin packages.

The command surface covers inspection (``list``/``info``/``inspect``/``doctor``/``path``)
and lifecycle verbs (``add``/``remove``/``update``/``enable``/``disable``/
``allow-version``). Installation is delegated to
:mod:`deeppyro.plugins.install`, which stages every change and keeps preflight,
install and manifest recording separate.

Exit codes:
  0  success
  1  usage error (unknown plugin id, bad arguments)
  2  preflight/diagnosis failure (nothing was installed or nothing is healthy)
  3  install failed and was rolled back
  4  restart required to apply the change
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from deeppyro.api.capabilities import CapabilityManifest
    from deeppyro.kernel.capabilities import CapabilityIssue, CapabilityReport

from deeppyro.plugins import install
from deeppyro.plugins.bootstrap import (
    PluginPackage,
    load_package,
    plugin_root,
    scan_plugin_packages,
)
from deeppyro.plugins.install import customized_core, requirement_errors, vendor_conflict_map
from deeppyro.plugins.metadata import (
    FAILURE_ABI,
    KNOWN_SLOT_KINDS,
    MetadataError,
    python_tag,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_PREFLIGHT = 2
EXIT_INSTALL = 3
EXIT_RESTART = 4


# -- helpers --------------------------------------------------------------


def _requirement_name(requirement: str) -> str:
    """``"websockets>=15"`` -> ``"websockets"`` (PEP 508-ish, best effort)."""
    text = requirement.strip()
    for index, char in enumerate(text):
        if char in "<>=!~[; ":
            return text[:index].strip()
    return text


def _normalise(name: str) -> str:
    return name.strip().lower().replace("-", "_").replace(".", "_")


def vendor_contents(directory: Path | None) -> set[str]:
    """Top-level importable names provided by a vendor directory."""
    if directory is None or not directory.is_dir():
        return set()
    names: set[str] = set()
    for child in directory.iterdir():
        name = child.name
        if name.endswith((".dist-info", ".egg-info")):
            names.add(_normalise(name.split("-")[0]))
        elif child.is_dir() and not name.startswith("__pycache__"):
            names.add(_normalise(name))
        elif child.suffix in (".pyd", ".so"):
            names.add(_normalise(child.stem.split(".")[0]))
    return names


def _abi_warnings(package: PluginPackage) -> list[MetadataError]:
    """Report untagged vendor dirs holding other interpreters' extensions."""
    if package.metadata is None:
        return []
    root = package.directory / package.metadata.deps.vendor
    if not root.is_dir():
        return []
    current = python_tag()
    warnings: list[MetadataError] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not child.name.startswith("cp"):
            continue
        if child.name == current:
            continue
        if any(item.suffix in (".pyd", ".so") for item in child.rglob("*")):
            warnings.append(
                MetadataError(
                    FAILURE_ABI,
                    f"{child} holds binary extensions for {child.name}, host is {current}",
                    plugin_id=package.id,
                )
            )
    return warnings


def dependency_errors(package: PluginPackage) -> list[MetadataError]:
    """Requirements that the vendor directory cannot satisfy."""
    metadata = package.metadata
    if metadata is None or not metadata.deps.requires:
        return []
    return requirement_errors(package.id, metadata.deps.requires, package.vendor)


def bundle_errors(package: PluginPackage) -> list[MetadataError]:
    """A package bundle layer that cannot be parsed is a boot hazard."""
    path = package.directory / "bundle.toml"
    if not path.is_file():
        return []
    from deeppyro.kernel.composition import CompositionError, load_bundle

    try:
        bundle = load_bundle(path)
    except CompositionError as exc:
        return [MetadataError("metadata-invalid", str(exc), plugin_id=package.id)]
    if not bundle.rows:
        return [
            MetadataError("metadata-invalid", f"{path} declares no [[plugins]] rows", plugin_id=package.id)
        ]
    return []


def diagnose(
    package: PluginPackage,
    *,
    conflicts: Mapping[str, Iterable[MetadataError]] | None = None,
) -> list[MetadataError]:
    """Every problem found for one package, in a stable order."""
    errors: list[MetadataError] = list(package.errors)
    errors.extend(dependency_errors(package))
    errors.extend(_abi_warnings(package))
    errors.extend(bundle_errors(package))
    if conflicts is not None:
        errors.extend(conflicts.get(package.id, ()))
    return errors


def plugin_manifest(package: PluginPackage) -> CapabilityManifest | None:
    """Derive one package's capability manifest, importing it like boot does.

    The package directory and vendor root are activated first, so the manifest
    reflects the class the kernel would mount.  An unimportable package degrades
    to the metadata declaration instead of failing the whole command.
    """
    from deeppyro.api.capabilities import CapabilityManifest, build_manifest
    from deeppyro.plugins.bootstrap import activate

    with contextlib.suppress(Exception):  # activation must never break inspection
        activate(scan_plugin_packages(known_slots=KNOWN_SLOT_KINDS))
    target = package.target
    candidate: Any = None
    if target and ":" in target:
        module_name, _, class_name = target.partition(":")
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - reported through package errors elsewhere
            module = None
        if module is not None:
            candidate = getattr(module, class_name, None)
    if isinstance(candidate, type):
        try:
            return build_manifest(candidate)
        except Exception:  # noqa: BLE001 - invalid metadata degrades to metadata only
            pass
    metadata = package.metadata
    if metadata is None:
        return None
    return CapabilityManifest(
        plugin_id=package.id,
        version=metadata.version or "0.0.0",
        services_provided=tuple(metadata.provides),
    )


def capability_index(
    packages: Sequence[PluginPackage],
) -> tuple[CapabilityReport, dict[str, CapabilityManifest]]:
    """Build the capability graph of installed packages plus the core composition."""
    from deeppyro.kernel.capabilities import IMPLICIT_SERVICES, CapabilityRegistry

    registry = CapabilityRegistry()
    manifests: dict[str, CapabilityManifest] = {}
    for package in packages:
        manifest = plugin_manifest(package)
        if manifest is None:
            continue
        manifests[package.id] = manifest
        registry.register(manifest, plugin_id=package.id, origin=str(package.directory))
    try:
        from deeppyro.kernel.composition import default_composition

        registry.index_composition(default_composition())
    except Exception:  # noqa: BLE001 - the core composition is advisory for the CLI
        pass
    return registry.report(available_services=IMPLICIT_SERVICES), manifests


def _package_payload(
    package: PluginPackage,
    errors: list[MetadataError],
    *,
    core_overrides: bool = False,
    warnings: Iterable[str] = (),
    manifest: CapabilityManifest | None = None,
    missing: Iterable[str] = (),
    issues: Iterable[CapabilityIssue] = (),
) -> dict[str, Any]:
    metadata = package.metadata
    return {
        "id": package.id,
        "version": package.version,
        "apiVersion": metadata.api_version if metadata is not None else None,
        "target": package.target,
        "provides": list(package.provides),
        "directory": str(package.directory),
        "vendor": str(package.vendor) if package.vendor is not None else None,
        "bundle": (package.directory / "bundle.toml").is_file(),
        "ui": (package.directory / "ui.toml").is_file(),
        "healthy": not errors,
        "customizedCore": core_overrides,
        "warnings": list(warnings),
        "manifest": manifest.to_dict() if manifest is not None else None,
        "missingDependencies": list(missing),
        "capabilityIssues": [issue.to_dict() for issue in issues],
        "errors": [
            {"kind": error.kind, "message": error.message, "remediation": error.remediation}
            for error in errors
        ],
    }


def _print_table(rows: list[list[str]], headers: list[str]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    line = "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    print(line)
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))


def _emit_json(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _failure(
    message: str,
    *,
    json_output: bool,
    kind: str,
    exit_code: int,
    problems: Iterable[str] = (),
    stderr_tail: str = "",
) -> int:
    problem_list = [str(problem) for problem in problems]
    if json_output:
        payload: dict[str, Any] = {"ok": False, "kind": kind, "error": message}
        if problem_list:
            payload["problems"] = problem_list
        if stderr_tail:
            payload["stderrTail"] = stderr_tail
        _emit_json(payload)
    else:
        print(message, file=sys.stderr)
        for problem in problem_list:
            print(f"  - {problem}", file=sys.stderr)
        if stderr_tail:
            print(stderr_tail, file=sys.stderr)
    return exit_code


def _installer_failure(exc: Exception, *, json_output: bool) -> int:
    if isinstance(exc, install.PreflightFailure):
        return _failure(
            "preflight failed; nothing was installed",
            json_output=json_output,
            kind="preflight",
            exit_code=EXIT_PREFLIGHT,
            problems=exc.problems,
        )
    if isinstance(exc, install.InstallFailure):
        message = str(exc)
        if exc.rolled_back:
            message += " (rolled back)"
        else:
            message += " (rollback incomplete; inspect the plugin directory)"
        return _failure(
            message,
            json_output=json_output,
            kind="install",
            exit_code=EXIT_INSTALL,
            stderr_tail=exc.stderr_tail,
        )
    if isinstance(exc, install.UnknownPlugin):
        return _failure(str(exc), json_output=json_output, kind="usage", exit_code=EXIT_USAGE)
    return _failure(str(exc), json_output=json_output, kind="preflight", exit_code=EXIT_PREFLIGHT)


# -- inspection verbs -----------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    packages = scan_plugin_packages(known_slots=KNOWN_SLOT_KINDS)
    conflicts = vendor_conflict_map(packages)
    diagnostics = {package.id: diagnose(package, conflicts=conflicts) for package in packages}
    report, manifests = capability_index(packages)
    if args.json:
        payload = []
        for package in packages:
            manifest = manifests.get(package.id)
            payload.append(
                _package_payload(
                    package,
                    diagnostics[package.id],
                    manifest=manifest,
                    missing=report.missing_dependencies(package.id),
                    issues=report.issues_for(package.id),
                )
            )
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return EXIT_OK
    if not packages:
        print(f"no plugin packages under {plugin_root()}")
        return EXIT_OK
    rows: list[list[str]] = []
    for package in packages:
        errors = diagnostics[package.id]
        status = "ok" if not errors else errors[0].kind
        metadata = package.metadata
        rows.append(
            [
                package.id,
                package.version or "-",
                str(metadata.api_version) if metadata is not None else "-",
                ",".join(package.provides) or "-",
                Path(package.vendor).name if package.vendor is not None else "-",
                status,
            ]
        )
    _print_table(rows, ["id", "version", "api", "provides", "vendor", "status"])
    unhealthy = [package.id for package in packages if diagnostics[package.id]]
    if unhealthy:
        print()
        print("unhealthy: " + ", ".join(unhealthy) + " (run 'dpyro plugin doctor')")
    return EXIT_OK


def cmd_info(args: argparse.Namespace) -> int:
    try:
        package = load_package(args.plugin_id)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    metadata = package.metadata
    if metadata is None:
        print(f"{package.id}: metadata is invalid")
        for error in package.errors:
            print(f"  {error.kind}: {error.message}")
        return EXIT_PREFLIGHT
    payload = {
        "id": metadata.id,
        "name": metadata.name,
        "version": metadata.version,
        "description": metadata.description,
        "module": metadata.module,
        "class": metadata.class_name,
        "apiVersion": metadata.api_version,
        "provides": list(metadata.provides),
        "compat": {
            "dpyro": metadata.compat.dpyro,
            "python": metadata.compat.python,
            "exemptVersions": list(metadata.compat.exempt_versions),
        },
        "deps": {
            "vendor": metadata.deps.vendor,
            "requires": list(metadata.deps.requires),
        },
        "ui": {"slots": list(metadata.ui.slots), "package": metadata.ui.package},
        "bundle": {"patch": metadata.bundle.patch},
        "directory": str(package.directory),
        "vendorDirectory": str(package.vendor) if package.vendor is not None else None,
        "raw": metadata.raw,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return EXIT_OK


def _print_manifest(manifest: CapabilityManifest) -> None:
    print(f"plugin {manifest.plugin_id} {manifest.version}")
    print(f"  provides:    {', '.join(manifest.provides) or '-'}")
    print(f"  requires:    {', '.join(manifest.requires) or '-'}")
    if manifest.requires_versions:
        constraints = ", ".join(f"{item.service} {item.version}" for item in manifest.requires_versions)
        print(f"  versions:    {constraints}")
    if manifest.tools:
        tools = ", ".join(
            f"{tool.name} [{tool.risk}{'' if tool.parallel_safe else ', serial'}]" for tool in manifest.tools
        )
        print(f"  tools:       {tools}")
    if manifest.events:
        print(f"  events:      {', '.join(manifest.events)}")
    if manifest.permissions:
        print(f"  permissions: {', '.join(manifest.permissions)}")
    print(f"  risk:        {manifest.risk}  idempotent: {manifest.idempotent}")
    if manifest.sandbox:
        print(f"  sandbox:     {manifest.sandbox}")
    if manifest.context_window:
        print(f"  context:     {manifest.context_window} tokens")
    if manifest.live_config:
        print(f"  live config: {', '.join(manifest.live_config)}")
    if manifest.config_schema:
        properties = manifest.config_schema.get("properties")
        keys = ", ".join(properties) if isinstance(properties, Mapping) else "-"
        print(f"  config keys: {keys or '-'}")


def _print_dependencies(node: Mapping[str, Any], indent: int = 0) -> None:
    pad = "  " * indent
    version = node.get("version") or "?"
    print(f"{pad}{node.get('pluginId')} {version}")
    provides = node.get("provides") or []
    if provides:
        print(f"{pad}  provides: {', '.join(str(item) for item in provides)}")
    for requirement in node.get("requires") or []:
        service = requirement.get("service")
        constraint = requirement.get("versionConstraint")
        suffix = f" ({constraint})" if constraint else ""
        provider = requirement.get("provider")
        if provider:
            state = "ok" if requirement.get("satisfied") else "VERSION-MISMATCH"
            provider_version = requirement.get("providerVersion") or ""
            print(f"{pad}  {service}{suffix} <- {provider} {provider_version} [{state}]".rstrip())
        else:
            print(f"{pad}  {service}{suffix} <- MISSING")
        nested = requirement.get("requires") or []
        if nested and provider:
            print(f"{pad}    provider {provider} requires:")
            for child in nested:
                child_service = child.get("service")
                child_constraint = child.get("versionConstraint")
                child_suffix = f" ({child_constraint})" if child_constraint else ""
                child_provider = child.get("provider")
                mark = "ok" if child.get("satisfied") else "MISSING"
                target = f"{child_provider}" if child_provider else "MISSING"
                print(f"{pad}      {child_service}{child_suffix} <- {target} [{mark}]")


def cmd_inspect(args: argparse.Namespace) -> int:
    try:
        package = load_package(args.plugin_id)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    packages = scan_plugin_packages(known_slots=KNOWN_SLOT_KINDS)
    report, manifests = capability_index(packages)
    manifest = manifests.get(package.id)
    missing = report.missing_dependencies(package.id) if manifest is not None else ()
    issues = [issue.to_dict() for issue in report.issues_for(package.id)]
    if args.json:
        _emit_json(
            {
                "id": package.id,
                "version": package.version,
                "target": package.target,
                "directory": str(package.directory),
                "manifest": manifest.to_dict() if manifest is not None else None,
                "missingDependencies": list(missing),
                "capabilityIssues": issues,
                "dependencyTree": report.dependency_tree(package.id),
            }
        )
        return EXIT_OK if manifest is not None else EXIT_PREFLIGHT
    if manifest is None:
        print(f"{package.id}: no capability manifest could be derived", file=sys.stderr)
        for error in package.errors:
            print(f"  {error.kind}: {error.message}", file=sys.stderr)
        return EXIT_PREFLIGHT
    _print_manifest(manifest)
    print(f"  target:      {package.target or '-'}")
    print(f"  directory:   {package.directory}")
    print("dependency tree:")
    _print_dependencies(report.dependency_tree(package.id), 1)
    if missing:
        print("missing dependencies: " + ", ".join(missing))
    if issues:
        for issue in report.issues_for(package.id):
            print(f"issue {issue.kind}: {issue.message}")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    packages = scan_plugin_packages(known_slots=KNOWN_SLOT_KINDS)
    issues = list(install.manifest_issues())
    if args.plugin_id:
        packages = [package for package in packages if package.id == args.plugin_id]
        issues = [issue for issue in issues if issue.plugin_id == args.plugin_id]
        if not packages and not issues:
            print(f"plugin {args.plugin_id!r} is not installed", file=sys.stderr)
            return EXIT_USAGE
    conflicts = vendor_conflict_map(packages)
    overrides = set(customized_core(packages))
    report, manifests = capability_index(packages)
    failures = 0
    for package in packages:
        errors = diagnose(package, conflicts=conflicts)
        manifest = manifests.get(package.id)
        advisory: list[str] = []
        if package.id in overrides:
            advisory.append("bundle.toml overrides composition rows owned by the core bundle")
        missing = report.missing_dependencies(package.id) if manifest is not None else ()
        for service in missing:
            advisory.append(f"capability: requires service {service!r}, which no installed plugin provides")
        if args.json:
            print(
                json.dumps(
                    _package_payload(
                        package,
                        errors,
                        core_overrides=package.id in overrides,
                        warnings=advisory,
                        manifest=manifest,
                        missing=missing,
                        issues=report.issues_for(package.id),
                    ),
                    indent=2,
                    ensure_ascii=False,
                )
            )
            failures += 1 if errors else 0
            continue
        header = f"{package.id} {package.version}".strip()
        if errors:
            failures += 1
            print(f"FAIL  {header}")
            for error in errors:
                print(f"      {error.kind}: {error.message}")
                if error.remediation:
                    print(f"      -> {error.remediation}")
        else:
            print(f"ok  {header}")
        for warning in advisory:
            print(f"warn  customized-core: {warning} [{package.id}]")
    for issue in issues:
        failures += 1
        if args.json:
            print(
                json.dumps(
                    {
                        "id": issue.plugin_id,
                        "healthy": False,
                        "manifestDangling": True,
                        "errors": [
                            {
                                "kind": issue.kind,
                                "message": issue.message,
                                "remediation": issue.remediation,
                            }
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            continue
        print(f"FAIL  {issue.plugin_id} (manifest)")
        print(f"      {issue.kind}: {issue.message}")
        if issue.remediation:
            print(f"      -> {issue.remediation}")
    if not packages and not issues:
        print(f"no plugin packages under {plugin_root()}")
    return EXIT_OK if failures == 0 else EXIT_PREFLIGHT


def cmd_path(args: argparse.Namespace) -> int:
    root = plugin_root()
    print(root)
    if args.json:
        print(json.dumps({"pluginsDir": str(root), "exists": root.is_dir()}, indent=2))
    return EXIT_OK


# -- lifecycle verbs ------------------------------------------------------


def cmd_add(args: argparse.Namespace) -> int:
    try:
        report = install.add(
            args.source,
            force=args.force,
            no_deps=args.no_deps,
            offline=args.offline,
        )
    except install.InstallerError as exc:
        return _installer_failure(exc, json_output=args.json)
    payload = {
        "ok": True,
        "id": report.plugin_id,
        "version": report.version,
        "directory": str(report.directory),
        "replaced": report.replaced,
        "manifestChanged": report.manifest_changed,
        "vendor": report.vendor,
        "warnings": list(report.warnings),
        "restartRequired": report.manifest_changed,
    }
    if args.json:
        _emit_json(payload)
    else:
        verb = "replaced" if report.replaced else "installed"
        print(f"{verb} {report.plugin_id} {report.version} -> {report.directory}")
        for warning in report.warnings:
            print(f"warn: {warning}")
        if report.manifest_changed:
            print("restart deeppyro to apply the change")
    return EXIT_RESTART if report.manifest_changed else EXIT_OK


def cmd_remove(args: argparse.Namespace) -> int:
    try:
        report = install.remove(args.plugin_id, force=args.force)
    except install.UnknownPlugin:
        return _failure(
            f"plugin {args.plugin_id!r} is not installed",
            json_output=args.json,
            kind="usage",
            exit_code=EXIT_USAGE,
        )
    except install.InstallerError as exc:
        return _installer_failure(exc, json_output=args.json)
    payload = {
        "ok": True,
        "id": report.plugin_id,
        "dependents": list(report.dependents),
        "forced": bool(report.dependents) and args.force,
        "manifestChanged": report.manifest_changed,
        "restartRequired": report.manifest_changed,
    }
    if args.json:
        _emit_json(payload)
    else:
        print(f"removed {report.plugin_id}")
        if report.dependents and args.force:
            print("removed despite dependents: " + ", ".join(report.dependents))
        if report.manifest_changed:
            print("restart deeppyro to apply the change")
    return EXIT_RESTART if report.manifest_changed else EXIT_OK


def cmd_update(args: argparse.Namespace) -> int:
    try:
        report = install.update(args.plugin_id, offline=args.offline)
    except install.UnknownPlugin:
        return _failure(
            f"plugin {args.plugin_id!r} is not installed",
            json_output=args.json,
            kind="usage",
            exit_code=EXIT_USAGE,
        )
    except install.InstallerError as exc:
        return _installer_failure(exc, json_output=args.json)
    payload = {
        "ok": True,
        "id": report.plugin_id,
        "rebuilt": report.rebuilt,
        "changes": [
            {"name": change.name, "before": change.before, "after": change.after} for change in report.changes
        ],
        "warnings": list(report.warnings),
        "restartRequired": report.rebuilt,
    }
    if args.json:
        _emit_json(payload)
    else:
        for change in report.changes:
            print(f"{change.name}: {change.before or '-'} -> {change.after or '-'}")
        if report.rebuilt:
            print(f"updated {report.plugin_id}")
        elif not report.changes:
            print(f"{report.plugin_id}: nothing to update")
        for warning in report.warnings:
            print(f"warn: {warning}")
    return EXIT_RESTART if report.rebuilt else EXIT_OK


def _state_command(args: argparse.Namespace, *, enabled: bool) -> int:
    try:
        report = install.set_enabled(args.plugin_id, enabled)
    except install.UnknownPlugin:
        return _failure(
            f"plugin {args.plugin_id!r} is not installed",
            json_output=args.json,
            kind="usage",
            exit_code=EXIT_USAGE,
        )
    except install.InstallerError as exc:
        return _installer_failure(exc, json_output=args.json)
    payload = {
        "ok": True,
        "id": report.plugin_id,
        "enabled": report.enabled,
        "changed": report.changed,
        "restartRequired": report.changed,
    }
    if args.json:
        _emit_json(payload)
    else:
        state = "enabled" if report.enabled else "disabled"
        print(f"{state} {report.plugin_id}")
        if report.changed:
            print("restart deeppyro to apply the change")
    return EXIT_RESTART if report.changed else EXIT_OK


def cmd_enable(args: argparse.Namespace) -> int:
    return _state_command(args, enabled=True)


def cmd_disable(args: argparse.Namespace) -> int:
    return _state_command(args, enabled=False)


def cmd_allow_version(args: argparse.Namespace) -> int:
    try:
        report = install.allow_version(args.plugin_id, args.version)
    except install.UnknownPlugin:
        return _failure(
            f"plugin {args.plugin_id!r} is not installed",
            json_output=args.json,
            kind="usage",
            exit_code=EXIT_USAGE,
        )
    except install.InstallerError as exc:
        return _installer_failure(exc, json_output=args.json)
    payload = {
        "ok": True,
        "id": report.plugin_id,
        "version": report.version,
        "changed": report.changed,
        "restartRequired": report.changed,
    }
    if args.json:
        _emit_json(payload)
    else:
        if report.changed:
            print(f"allowed {report.plugin_id} {report.version} (backup: plugin.toml.bak)")
        else:
            print(f"{report.plugin_id} {report.version} is already allowed")
    return EXIT_RESTART if report.changed else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dpyro plugin",
        description="inspect, diagnose and install deeppyro plugin packages",
    )
    parser.add_argument("--json", action="store_true", help="machine readable output")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="list installed plugin packages")
    info = sub.add_parser("info", help="show the full metadata of one package")
    info.add_argument("plugin_id")
    inspect = sub.add_parser("inspect", help="show a package's capability manifest and dependency tree")
    inspect.add_argument("plugin_id")
    doctor = sub.add_parser("doctor", help="diagnose packages and print remediation")
    doctor.add_argument("plugin_id", nargs="?", default=None)
    sub.add_parser("path", help="print the plugin package directory")

    add = sub.add_parser("add", help="install a plugin package")
    add.add_argument("source", help="directory, wheel/sdist archive, registry spec or git URL")
    add.add_argument("--force", action="store_true", help="replace an existing install")
    add.add_argument("--no-deps", dest="no_deps", action="store_true", help="do not install [deps].requires")
    add.add_argument("--offline", action="store_true", help="pass --offline to uv")

    remove = sub.add_parser("remove", help="remove an installed package")
    remove.add_argument("plugin_id")
    remove.add_argument("--force", action="store_true", help="remove even if other packages depend on it")

    update = sub.add_parser("update", help="rebuild a package's vendor tree")
    update.add_argument("plugin_id")
    update.add_argument("--offline", action="store_true", help="pass --offline to uv")

    for name in ("enable", "disable"):
        child = sub.add_parser(name, help=f"{name} an installed package")
        child.add_argument("plugin_id")

    allow = sub.add_parser("allow-version", help="allow one exact dpyro version for a package")
    allow.add_argument("plugin_id")
    allow.add_argument("version")

    # Accept --json after the verb as well; SUPPRESS keeps a global --json that
    # appeared before the verb instead of overwriting it with False.
    for child in sub.choices.values():
        child.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="machine readable output",
        )
    return parser


_COMMANDS = {
    None: cmd_list,
    "list": cmd_list,
    "info": cmd_info,
    "inspect": cmd_inspect,
    "doctor": cmd_doctor,
    "path": cmd_path,
    "add": cmd_add,
    "remove": cmd_remove,
    "update": cmd_update,
    "enable": cmd_enable,
    "disable": cmd_disable,
    "allow-version": cmd_allow_version,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not hasattr(args, "json"):  # pragma: no cover - only when a verb vanishes
        args.json = False
    handler = _COMMANDS.get(args.command)
    if handler is None:  # pragma: no cover - argparse rejects unknown verbs
        parser.print_help()
        return EXIT_USAGE
    return handler(args)


__all__ = [
    "EXIT_INSTALL",
    "EXIT_OK",
    "EXIT_PREFLIGHT",
    "EXIT_RESTART",
    "EXIT_USAGE",
    "build_parser",
    "bundle_errors",
    "capability_index",
    "cmd_inspect",
    "dependency_errors",
    "diagnose",
    "main",
    "plugin_manifest",
    "vendor_contents",
]

if __name__ == "__main__":  # pragma: no cover - manual invocation
    raise SystemExit(main())
