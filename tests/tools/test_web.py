"""web_search / web_fetch: mocked HTTP responses, formatting, validation."""

from __future__ import annotations

import httpx
import pytest
import respx

from deeppyro.core.errors import ToolError
from deeppyro.tools.builtin.web import (
    DUCKDUCKGO_HTML_ENDPOINT,
    FETCH_NOTICE,
    TRUNCATION_NOTE,
    WebFetchTool,
    WebSearchTool,
    html_to_text,
    parse_search_results,
)
from tests.tools.helpers import make_context

SEARCH_PAGE = """
<html><body>
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fone&amp;rut=abc">Example One</a>
<a rel="nofollow" class="result__a" href="https://example.org/two">Example Two</a>
<a class="result__snippet" href="https://ignored.example/snippet">Not a result title</a>
</body></html>
"""


def test_html_to_text_strips_markup_and_scripts() -> None:
    markup = "<html><head><style>h1{}</style><script>bad()</script></head><body><h1>Title</h1><p>Hello <b>world</b> &amp; more</p></body></html>"
    text = html_to_text(markup)
    assert "Title" in text
    assert "Hello world & more" in text
    assert "bad()" not in text
    assert "h1{}" not in text
    assert "<p>" not in text


def test_parse_search_results_unwraps_redirects() -> None:
    results = parse_search_results(SEARCH_PAGE, 8)
    assert results == [
        {"title": "Example One", "url": "https://example.com/one"},
        {"title": "Example Two", "url": "https://example.org/two"},
    ]


async def test_fetch_html_returns_untrusted_text(workspace) -> None:  # type: ignore[no-untyped-def]
    with respx.mock() as mock:
        route = mock.get("https://example.com/page").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text="<html><body><h1>Title</h1><p>Hello world</p><script>x()</script></body></html>",
            )
        )
        result = await WebFetchTool().run({"url": "https://example.com/page"}, make_context(workspace))
    assert route.called
    assert result.is_error is False
    assert result.text.startswith("Fetched https://example.com/page (HTTP 200)\n\n")
    assert FETCH_NOTICE in result.text
    assert "Title" in result.text and "Hello world" in result.text
    assert "x()" not in result.text
    assert result.meta["status"] == 200


async def test_fetch_non_2xx_is_returned_not_raised(workspace) -> None:  # type: ignore[no-untyped-def]
    with respx.mock() as mock:
        mock.get("https://example.com/missing").mock(
            return_value=httpx.Response(
                404,
                headers={"content-type": "text/plain"},
                text="404: Not Found",
            )
        )
        result = await WebFetchTool().run({"url": "https://example.com/missing"}, make_context(workspace))
    assert result.is_error is False
    assert "HTTP 404" in result.text
    assert "404: Not Found" in result.text


async def test_fetch_truncates_long_content(workspace) -> None:  # type: ignore[no-untyped-def]
    with respx.mock() as mock:
        mock.get("https://example.com/long").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                text="abcdefghijklmnopqrstuvwxyz",
            )
        )
        result = await WebFetchTool(max_output_chars=10).run(
            {"url": "https://example.com/long"}, make_context(workspace)
        )
    assert TRUNCATION_NOTE in result.text
    assert result.meta["truncated"] is True


async def test_fetch_validates_url(workspace) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ToolError, match="url must be a non-empty string"):
        await WebFetchTool().run({}, make_context(workspace))
    with pytest.raises(ToolError, match="absolute http"):
        await WebFetchTool().run({"url": "ftp://example.com/file"}, make_context(workspace))


async def test_fetch_transport_error_is_tool_error(workspace) -> None:  # type: ignore[no-untyped-def]
    with respx.mock() as mock:
        mock.get("https://example.com/down").mock(side_effect=httpx.ConnectError("connection refused"))
        with pytest.raises(ToolError, match="connection refused"):
            await WebFetchTool().run({"url": "https://example.com/down"}, make_context(workspace))


async def test_search_formats_sources(workspace) -> None:  # type: ignore[no-untyped-def]
    with respx.mock() as mock:
        route = mock.post(DUCKDUCKGO_HTML_ENDPOINT).mock(return_value=httpx.Response(200, text=SEARCH_PAGE))
        result = await WebSearchTool().run({"queries": ["deep seek"]}, make_context(workspace))
    assert route.call_count == 1
    assert result.is_error is False
    assert result.text.startswith(FETCH_NOTICE + "\n\nSources:\n")
    assert "- [Example One](https://example.com/one)" in result.text
    assert "- [Example Two](https://example.org/two)" in result.text
    assert "Cite the relevant URLs above as markdown links in your answer." in result.text
    assert result.meta["results"] == 2


async def test_search_runs_queries_concurrently_and_merges(workspace) -> None:  # type: ignore[no-untyped-def]
    with respx.mock() as mock:
        route = mock.post(DUCKDUCKGO_HTML_ENDPOINT).mock(return_value=httpx.Response(200, text=SEARCH_PAGE))
        result = await WebSearchTool().run(
            {"queries": ["first query", "second query"]}, make_context(workspace)
        )
    assert route.call_count == 2
    assert result.text.count("- [") == 2  # duplicate URLs across queries are removed


async def test_search_limits_sources(workspace) -> None:  # type: ignore[no-untyped-def]
    page = """
    <html><body>
    <a class="result__a" href="https://a.example/1">One</a>
    <a class="result__a" href="https://a.example/2">Two</a>
    <a class="result__a" href="https://a.example/3">Three</a>
    </body></html>
    """
    with respx.mock() as mock:
        mock.post(DUCKDUCKGO_HTML_ENDPOINT).mock(return_value=httpx.Response(200, text=page))
        result = await WebSearchTool(max_results=2).run({"queries": ["q"]}, make_context(workspace))
    assert result.text.count("- [") == 2
    assert "(Showing the first 2 sources. Refine the query for more.)" in result.text


async def test_search_empty_and_invalid_queries(workspace) -> None:  # type: ignore[no-untyped-def]
    with respx.mock(assert_all_called=False) as mock:
        mock.post(DUCKDUCKGO_HTML_ENDPOINT).mock(return_value=httpx.Response(200, text="<html></html>"))
        empty = await WebSearchTool().run({"queries": ["nothing"]}, make_context(workspace))
    assert empty.text == "No results found"
    with pytest.raises(ToolError, match="at least one query"):
        await WebSearchTool().run({"queries": []}, make_context(workspace))
    with pytest.raises(ToolError, match="at least one query"):
        await WebSearchTool().run({}, make_context(workspace))
    with pytest.raises(ToolError, match="at most 4 queries"):
        await WebSearchTool().run({"queries": ["a", "b", "c", "d", "e"]}, make_context(workspace))
    with pytest.raises(ToolError, match="non-empty strings"):
        await WebSearchTool().run({"queries": [1]}, make_context(workspace))


async def test_search_http_failure_is_tool_error(workspace) -> None:  # type: ignore[no-untyped-def]
    with respx.mock() as mock:
        mock.post(DUCKDUCKGO_HTML_ENDPOINT).mock(return_value=httpx.Response(500, text="oops"))
        with pytest.raises(ToolError, match="HTTP 500"):
            await WebSearchTool().run({"queries": ["q"]}, make_context(workspace))
