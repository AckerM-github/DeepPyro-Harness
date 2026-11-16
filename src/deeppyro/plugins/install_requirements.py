"""Requirement parsing, id/version validation and install-source planning."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from deeppyro.plugins.install_errors import PreflightFailure
from deeppyro.plugins.metadata import satisfies


def normalise_name(name: str) -> str:
    """PEP 503-ish normalisation used for vendor package comparisons."""
    return name.strip().lower().replace("-", "_").replace(".", "_")


_REQUIREMENT_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)$", re.DOTALL)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.!+_-]*$")


def parse_requirement(requirement: str) -> tuple[str, str]:
    """Split ``"websockets>=15"`` into ``("websockets", ">=15")``.

    Extras and environment markers are tolerated; ``~=`` and other specs the
    metadata helper cannot evaluate raise :class:`ValueError`.
    """
    text = requirement.strip()
    if not text:
        raise ValueError("empty requirement")
    match = _REQUIREMENT_RE.fullmatch(text)
    if match is None:
        raise ValueError(f"invalid requirement {requirement!r}")
    name, rest = match.group(1), match.group(2).strip()
    if rest.startswith("["):
        end = rest.find("]")
        if end < 0:
            raise ValueError(f"invalid extras in {requirement!r}")
        rest = rest[end + 1 :].strip()
    if ";" in rest:
        rest = rest.split(";", 1)[0].strip()
    if rest.startswith(("@", "(")):
        # Direct URL references carry no evaluable version spec.
        return name, ""
    if rest:
        satisfies("0.0.0", rest)  # raises ValueError for unsupported specs
    return name, rest


def validate_plugin_id(plugin_id: str) -> str | None:
    """Return a problem message, or ``None`` when the id is a safe dir name."""
    if not plugin_id:
        return "plugin id must not be empty"
    if plugin_id in (".", ".."):
        return f"plugin id {plugin_id!r} is not a usable directory name"
    if not _ID_RE.fullmatch(plugin_id):
        return f"plugin id {plugin_id!r} must match [A-Za-z0-9][A-Za-z0-9._-]*"
    return None


def validate_version(version: str) -> str | None:
    """Return a problem message, or ``None`` for a plausible version string."""
    if not version:
        return "package version is empty"
    if not _VERSION_RE.fullmatch(version):
        return f"package version {version!r} is not a valid version"
    return None


@dataclass(frozen=True, slots=True)
class SourcePlan:
    """How a ``add`` source is materialised."""

    kind: str
    source: str
    path: Path | None = None
    spec: str = ""
    expected_id: str | None = None


def _looks_like_url(text: str) -> bool:
    lowered = text.lower()
    return lowered.startswith(("git+", "http://", "https://", "ssh://", "git://", "file://", "git@"))


def _looks_like_path(text: str) -> bool:
    if text.startswith((".", "~", "/", "\\")):
        return True
    if "/" in text or "\\" in text:
        return True
    if len(text) > 1 and text[1] == ":":
        return True
    return text.lower().endswith((".whl", ".zip", ".tar.gz", ".tgz", ".tar"))


def _archive_id(path: Path) -> str:
    name = path.name
    for suffix in (".tar.gz", ".tgz", ".whl", ".zip", ".tar"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    head = re.split(r"[-_](?=\d)", name)[0]
    return head.strip("._-") or path.stem


def _url_id(text: str) -> str:
    cleaned = text.strip().rstrip("/")
    cleaned = cleaned.removesuffix(".git")
    tail = cleaned.replace("\\", "/").rsplit("/", 1)[-1]
    tail = tail.rsplit(":", 1)[-1] if "://" not in cleaned else tail
    return re.sub(r"[^A-Za-z0-9._-]+", "-", tail).strip(".-") or "plugin"


def plan_source(source: str | Path) -> SourcePlan:
    """Classify ``add`` input and validate the cheap, static constraints."""
    text = str(source).strip()
    if not text:
        raise PreflightFailure(("source must not be empty",))
    candidate = Path(os.path.expanduser(text))
    if candidate.exists():
        if candidate.is_dir():
            if not (candidate / "plugin.toml").is_file():
                raise PreflightFailure((f"metadata-invalid: {candidate} does not contain a plugin.toml",))
            problem = validate_plugin_id(candidate.name)
            if problem is not None:
                raise PreflightFailure((f"metadata-invalid: {problem}",))
            return SourcePlan("directory", text, path=candidate.resolve(), expected_id=candidate.name)
        if candidate.is_file():
            if not _looks_like_path(candidate.name):
                raise PreflightFailure((f"unsupported package archive {candidate}",))
            return SourcePlan("archive", text, path=candidate.resolve(), expected_id=_archive_id(candidate))
        raise PreflightFailure((f"unsupported package source {candidate}",))
    if _looks_like_url(text):
        return SourcePlan("git", text, spec=text, expected_id=_url_id(text))
    if _looks_like_path(text):
        raise PreflightFailure((f"{text!r} does not exist",))
    try:
        name, _ = parse_requirement(text)
    except ValueError as exc:
        raise PreflightFailure((f"invalid requirement {text!r}: {exc}",)) from exc
    return SourcePlan("registry", text, spec=text, expected_id=name)
