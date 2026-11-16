"""Archive extraction, payload materialisation and payload metadata loading."""

from __future__ import annotations

import os
import shutil
import sys
import tarfile
import tomllib
import zipfile
from collections.abc import Mapping
from pathlib import Path

from deeppyro.plugins.install_errors import InstallFailure, PreflightFailure
from deeppyro.plugins.install_requirements import SourcePlan, validate_plugin_id
from deeppyro.plugins.install_uv import Runner, UvCommand, _execute
from deeppyro.plugins.metadata import MetadataError, PluginMetadata, load_metadata, parse_toml


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    base = destination.resolve()
    try:
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                member = (destination / info.filename).resolve()
                if member != base and base not in member.parents:
                    raise PreflightFailure((f"archive member escapes the extraction root: {info.filename}",))
            bundle.extractall(destination)
    except zipfile.BadZipFile as exc:
        raise PreflightFailure((f"cannot read {archive}: {exc}",)) from exc
    except OSError as exc:
        raise PreflightFailure((f"cannot extract {archive}: {exc}",)) from exc


def _safe_extract_tar(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:*") as bundle:
            bundle.extractall(destination, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise PreflightFailure((f"cannot extract {archive}: {exc}",)) from exc


def _safe_extract(archive: Path, destination: Path) -> None:
    name = archive.name.lower()
    if name.endswith((".whl", ".zip")):
        _safe_extract_zip(archive, destination)
    elif name.endswith((".tar.gz", ".tgz", ".tar")):
        _safe_extract_tar(archive, destination)
    else:
        raise PreflightFailure((f"unsupported archive {archive}",))


def _payload_root(extracted: Path) -> Path:
    if (extracted / "plugin.toml").is_file():
        return extracted
    candidates = [
        child
        for child in sorted(extracted.iterdir())
        if child.is_dir() and not child.name.startswith((".", "__")) and (child / "plugin.toml").is_file()
    ]
    if len(candidates) == 1:
        return candidates[0]
    raise PreflightFailure(
        (
            f"metadata-invalid: {extracted} does not contain plugin.toml at its root",
            "hint: place plugin.toml at the archive root or inside one top-level directory",
        )
    )


def _materialise(
    plan: SourcePlan,
    staging: Path,
    *,
    runner: Runner | None,
    offline: bool,
    timeout: float | None,
) -> Path:
    payload = staging / "payload"
    if plan.kind == "directory":
        assert plan.path is not None
        shutil.copytree(
            plan.path,
            payload,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "plugin.toml.bak"),
        )
        return payload
    if plan.kind == "archive":
        assert plan.path is not None
        extracted = staging / "extract"
        _safe_extract(plan.path, extracted)
        os.replace(_payload_root(extracted), payload)
        return payload
    dist = staging / "dist"
    dist.mkdir()
    command = UvCommand(
        (
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(dist),
            "--no-deps",
            plan.spec or plan.source,
        )
        + (("--offline",) if offline else ())
    )
    result = _execute(runner, command, timeout)
    if not result.ok:
        raise InstallFailure(
            f"uv pip install failed for {plan.source!r} (exit {result.returncode})",
            stderr_tail=result.stderr,
        )
    os.replace(_payload_root(dist), payload)
    return payload


def _declared_id(payload: Path) -> str | None:
    try:
        document = parse_toml((payload / "plugin.toml").read_bytes())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    package = document.get("package")
    if isinstance(package, Mapping):
        value = package.get("id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = document.get("id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _payload_id(plan: SourcePlan, payload: Path) -> str:
    declared = _declared_id(payload)
    if declared is not None:
        return declared
    if plan.expected_id:
        return plan.expected_id
    return payload.name


def _load_payload_metadata(payload: Path, plan: SourcePlan) -> PluginMetadata:
    expected = _payload_id(plan, payload)
    problem = validate_plugin_id(expected)
    if problem is not None:
        raise PreflightFailure((f"metadata-invalid: {problem}",))
    try:
        return load_metadata(payload, plugin_id=expected)
    except MetadataError as exc:
        raise PreflightFailure((f"{exc.kind}: {exc.message}",)) from exc
