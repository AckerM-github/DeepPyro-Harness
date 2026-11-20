"""Markdown safety: model output must never render raw HTML or an approval UI.

The transcript renders untrusted model text through react-markdown. These are
static assertions on the renderer and its dependencies: raw HTML is skipped,
no rehype plugin is installed that could parse it back, the component never
uses dangerouslySetInnerHTML, and the element filter drops the active tags that
could fake a form, button, iframe, style or script in the page.
"""

from __future__ import annotations

import json
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
MARKDOWN_SOURCE = WEB_ROOT / "src" / "components" / "Markdown.tsx"
PACKAGE_JSON = WEB_ROOT / "package.json"

#: Elements a model reply must never be able to instantiate in the transcript.
FORBIDDEN_ELEMENTS = ("form", "button", "iframe", "style", "script")


def _markdown_source() -> str:
    assert MARKDOWN_SOURCE.is_file(), f"missing {MARKDOWN_SOURCE}"
    return MARKDOWN_SOURCE.read_text(encoding="utf-8")


def _markdown_element() -> str:
    """The JSX props of the ReactMarkdown element itself."""
    source = _markdown_source()
    start = source.index("<ReactMarkdown")
    end = source.index("</ReactMarkdown>", start)
    return source[start:end]


def _disallowed_elements_block() -> str:
    """The literal element filter declared next to the components map."""
    source = _markdown_source()
    start = source.index("const disallowedElements")
    end = source.index("];", start)
    return source[start:end]


def test_markdown_skips_raw_html() -> None:
    element = _markdown_element()
    assert "skipHtml" in element
    assert "disallowedElements" in element


def test_markdown_never_enables_dangerous_html() -> None:
    source = _markdown_source()
    assert "rehype-raw" not in source
    assert "rehypeRaw" not in source
    assert "allowDangerousHtml" not in source
    assert "dangerouslySetInnerHTML" not in source


def test_markdown_disallows_active_elements() -> None:
    block = _disallowed_elements_block()
    for element in FORBIDDEN_ELEMENTS:
        assert f"'{element}'" in block, element


def test_markdown_dependencies_do_not_parse_raw_html() -> None:
    package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    for section in ("dependencies", "devDependencies"):
        assert "rehype-raw" not in package.get(section, {}), section
    assert "react-markdown" in package["dependencies"]
