"""Static build guards for the React/Vite web UI.

These tests are static: they inspect the committed SPA build, the Vite
configuration that produces it, the hatchling wheel config that ships it, and
the CSP that governs it. They need no socket, no browser and no network; the
ASGI/browser contract is covered by tests/web/test_ui_server.py.

Run "cd web && pnpm install && pnpm build" after changing web/src, and commit
the regenerated src/deeppyro/web/ui/static tree. The wheel embeds that tree, so
a missing build is caught here before release.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from deeppyro.web.ui import server as ui_server
from deeppyro.web.ui.auth import UI_CSP

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "src" / "deeppyro"
STATIC_DIR = PACKAGE_ROOT / "web" / "ui" / "static"
BRAND_DIR = PACKAGE_ROOT / "web" / "ui" / "assets"
WEB_DIR = REPO_ROOT / "web"
SHELL = STATIC_DIR / "index.html"

REFERENCE = re.compile(r'(?:src|href)="([^"]+)"')
EXTERNAL_URL = re.compile(r"https?://", re.IGNORECASE)
LEGACY_MARKERS = ("nicegui", "_nicegui", "socket.io", "socketio", "reconnect_timeout")


def _shell() -> str:
    return SHELL.read_text(encoding="utf-8")


def _assets() -> list[Path]:
    return sorted(path for path in (STATIC_DIR / "assets").iterdir() if path.is_file())


def test_ui_server_serves_the_committed_build() -> None:
    assert Path(ui_server.STATIC_DIR).resolve() == STATIC_DIR.resolve()
    assert Path(ui_server.ASSET_DIR).resolve() == BRAND_DIR.resolve()
    assert SHELL.is_file(), "missing committed SPA shell; run pnpm build in web/"
    assert "pnpm build" in ui_server.MISSING_BUILD_MESSAGE


def test_shell_references_resolve_to_committed_files() -> None:
    page = _shell()
    assert page.lstrip().lower().startswith("<!doctype html>")
    assert '<div id="root"></div>' in page
    assert 'name="viewport"' in page
    assert 'rel="icon"' in page
    references = REFERENCE.findall(page)
    asset_refs = [ref for ref in references if ref.startswith("/assets/")]
    brand_refs = [ref for ref in references if ref.startswith("/brand/")]
    assert any(ref.endswith(".js") for ref in asset_refs), "shell must load a module bundle"
    assert any(ref.endswith(".css") for ref in asset_refs), "shell must load the stylesheet"
    assert any(ref.endswith(".svg") for ref in brand_refs), "shell must reference the brand icon"
    for ref in asset_refs:
        resolved = STATIC_DIR / ref.removeprefix("/")
        assert resolved.is_file(), f"index.html references missing build asset {ref}"
    for ref in brand_refs:
        resolved = BRAND_DIR / ref.removeprefix("/brand/")
        assert resolved.is_file(), f"index.html references missing brand asset {ref}"


def test_shell_is_a_csp_friendly_document() -> None:
    page = _shell()
    assert "<script>" not in page
    assert "<style" not in page
    assert " style=" not in page
    assert " onclick=" not in page
    assert " onload=" not in page
    assert "javascript:" not in page
    assert not EXTERNAL_URL.search(page), "the shell must not load external URLs"
    for marker in LEGACY_MARKERS:
        assert marker not in page.lower(), f"the shell still mentions {marker}"


def test_hashed_bundles_are_committed_and_nonempty() -> None:
    assets = _assets()
    bundles = [path for path in assets if path.suffix == ".js"]
    styles = [path for path in assets if path.suffix == ".css"]
    assert bundles, "vite build must emit at least one hashed JS bundle"
    assert styles, "vite build must emit at least one hashed stylesheet"
    for path in bundles + styles:
        assert path.stat().st_size > 1000, f"{path.name} looks empty"
        assert "-" in path.stem, f"{path.name} is not content-hashed"
    assert not list(STATIC_DIR.rglob("*.map")), "source maps must not ship in the static tree"


def test_bundle_keeps_the_json_api_and_sse_contract() -> None:
    bundle = "\n".join(path.read_text(encoding="utf-8") for path in _assets() if path.suffix == ".js")
    assert "/api/ui/" in bundle
    assert "/events" in bundle
    assert "EventSource" in bundle
    assert "deeppyro.web.token" in bundle
    for marker in LEGACY_MARKERS:
        assert marker not in bundle.lower(), f"the bundle still mentions {marker}"


def test_stylesheet_uses_bundled_theme_tokens() -> None:
    css = "\n".join(path.read_text(encoding="utf-8") for path in _assets() if path.suffix == ".css")
    assert "--bg" in css
    assert "grid-template" in css
    for forbidden in ("googleapis", "jsdelivr", "unpkg", "cdn."):
        assert forbidden not in css.lower(), f"stylesheet depends on external resource {forbidden}"


def test_vite_build_targets_the_committed_static_dir() -> None:
    package = json.loads((WEB_DIR / "package.json").read_text(encoding="utf-8"))
    assert package["private"] is True
    assert package["scripts"]["build"] == "tsc --noEmit && vite build"
    assert package["scripts"]["typecheck"] == "tsc --noEmit"
    config = (WEB_DIR / "vite.config.ts").read_text(encoding="utf-8")
    assert "'../src/deeppyro/web/ui/static'" in config
    assert "assetsDir: 'assets'" in config
    assert "emptyOutDir: true" in config
    assert "base: '/'" in config
    assert "'/api':" in config
    assert "127.0.0.1:8765" in config
    assert not (WEB_DIR / "dist").exists(), "vite must not emit a second, unshipped dist tree"


def test_hatch_wheel_packages_the_web_ui() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["build-system"]["build-backend"] == "hatchling.build"
    wheel = project["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/deeppyro"]
    exclude = [str(pattern) for pattern in wheel.get("exclude", [])]
    assert not any("static" in pattern or "assets" in pattern for pattern in exclude), (
        "wheel config must not exclude the committed web build"
    )
    dependencies = [dependency.lower() for dependency in project["project"]["dependencies"]]
    assert not any(dependency.startswith("nicegui") for dependency in dependencies)
    required = [SHELL, BRAND_DIR / "cyborg-mark.svg", *_assets()]
    for path in required:
        assert path.is_file(), f"{path.relative_to(REPO_ROOT)} is not part of the packaged tree"
        assert path.stat().st_size > 0, f"{path.relative_to(REPO_ROOT)} is empty"


def test_legacy_nicegui_assets_are_gone() -> None:
    for name in ("style.css", "settings.css", "chat.css", "InterVariable.woff2"):
        assert not (BRAND_DIR / name).exists(), f"stale NiceGUI asset {name} is still present"
    assert (BRAND_DIR / "cyborg-mark.svg").is_file()
    leftovers = {path.name for path in BRAND_DIR.iterdir() if path.is_file()} - {"cyborg-mark.svg"}
    assert not leftovers, f"unexpected files in ui/assets: {sorted(leftovers)}"


def test_csp_is_compatible_with_the_spa_build() -> None:
    csp = UI_CSP
    for directive in (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "connect-src 'self' ws: wss:",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "worker-src 'self' blob:",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ):
        assert directive in csp, f"CSP is missing {directive!r}"
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert not EXTERNAL_URL.search(csp)
