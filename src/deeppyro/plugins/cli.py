"""``dpyro plugin`` — inspect and diagnose installed plugin packages.

The command surface is deliberately small for now (``list``/``info``/``doctor``/
``path``); installation verbs (``add``/``remove``/``update``) arrive with the uv
installer and reuse the same :class:`~deeppyro.plugins.bootstrap.PluginPackage`
diagnosis, so what ``doctor`` reports is exactly what the installer would have
preflighted.

Exit codes:
  0  success
  1  usage error
  2  preflight/diagnosis failure (nothing was installed or nothing is healthy)
  3  install failed and was rolled back
  4  restart required to apply the change
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from deeppyro.plugins.bootstrap import (
    PluginPackage,
    load_package,
    plugin_root,
    scan_plugin_packages,
)
from deeppyro.plugins.metadata import (
    FAILURE_ABI,
    FAILURE_DEP_MISSING,
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
    available = vendor_contents(package.vendor)
    errors: list[MetadataError] = []
    for requirement in metadata.deps.requires:
        name = _requirement_name(requirement)
        if not name:
            continue
        if _normalise(name) not in available:
            errors.append(
                MetadataError(
                    FAILURE_DEP_MISSING,
                    f"requires {requirement!r} but {package.vendor or metadata.deps.vendor} does not provide it",
                    plugin_id=package.id,
                )
            )
    return errors


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


def diagnose(package: PluginPackage) -> list[MetadataError]:
    """Every problem found for one package, in a stable order."""
    errors: list[MetadataError] = list(package.errors)
    errors.extend(dependency_errors(package))
    errors.extend(_abi_warnings(package))
    errors.extend(bundle_errors(package))
    return errors


def _package_payload(package: PluginPackage, errors: list[MetadataError]) -> dict[str, Any]:
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


# -- verbs ----------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    packages = scan_plugin_packages()
    if args.json:
        payload = [_package_payload(package, diagnose(package)) for package in packages]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return EXIT_OK
    if not packages:
        print(f"no plugin packages under {plugin_root()}")
        return EXIT_OK
    rows: list[list[str]] = []
    for package in packages:
        errors = diagnose(package)
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
    unhealthy = [package.id for package in packages if diagnose(package)]
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


def cmd_doctor(args: argparse.Namespace) -> int:
    packages = scan_plugin_packages()
    if args.plugin_id:
        packages = [package for package in packages if package.id == args.plugin_id]
        if not packages:
            print(f"plugin {args.plugin_id!r} is not installed", file=sys.stderr)
            return EXIT_USAGE
    failures = 0
    for package in packages:
        errors = diagnose(package)
        if args.json:
            print(json.dumps(_package_payload(package, errors), indent=2, ensure_ascii=False))
            failures += 1 if errors else 0
            continue
        header = f"{package.id} {package.version}".strip()
        if not errors:
            print(f"ok  {header}")
            continue
        failures += 1
        print(f"FAIL  {header}")
        for error in errors:
            print(f"      {error.kind}: {error.message}")
            if error.remediation:
                print(f"      -> {error.remediation}")
    if not packages:
        print(f"no plugin packages under {plugin_root()}")
    return EXIT_OK if failures == 0 else EXIT_PREFLIGHT


def cmd_path(args: argparse.Namespace) -> int:
    root = plugin_root()
    print(root)
    if args.json:
        print(json.dumps({"pluginsDir": str(root), "exists": root.is_dir()}, indent=2))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dpyro plugin",
        description="inspect and diagnose deeppyro plugin packages",
    )
    parser.add_argument("--json", action="store_true", help="machine readable output")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="list installed plugin packages")
    info = sub.add_parser("info", help="show the full metadata of one package")
    info.add_argument("plugin_id")
    doctor = sub.add_parser("doctor", help="diagnose packages and print remediation")
    doctor.add_argument("plugin_id", nargs="?", default=None)
    sub.add_parser("path", help="print the plugin package directory")
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
    "doctor": cmd_doctor,
    "path": cmd_path,
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
    "dependency_errors",
    "diagnose",
    "main",
    "vendor_contents",
]

if __name__ == "__main__":  # pragma: no cover - manual invocation
    raise SystemExit(main())
