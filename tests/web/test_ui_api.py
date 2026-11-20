"""HTTP and host-level tests for the paginated session catalogue.

GET /api/ui/sessions used to be a boolean-shaped call (include_archived only)
that the host capped at SESSION_LIST_LIMIT rows. These tests pin the new
limit/offset/query/include_archived contract, the stable response shape with
total/has_more, the legacy /api/sessions compatibility path, and the host's
filter/slice/clamp behaviour over a single native snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from deeppyro.session.native_index import NativeSessionInfo
from deeppyro.web.service import MAX_SESSION_LIST_LIMIT, SESSION_LIST_LIMIT, WebSessionHost
from deeppyro.web.ui import server as ui_server
from deeppyro.web.ui.api import UiContext
from deeppyro.web.ui.auth import cookie_name

TOKEN = "test-token-0123456789"
PORT = 8765
COOKIE = {"cookie": f"{cookie_name(PORT)}={TOKEN}"}


def _port() -> ui_server._PortRef:
    port_ref = ui_server._PortRef()
    port_ref.value = PORT
    return port_ref


def _client(service: Any) -> httpx.AsyncClient:
    app = ui_server.create_app(
        service_provider=lambda: service,
        context=UiContext(token=TOKEN, version="test"),
        port=_port(),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
        headers=COOKIE,
    )


@dataclass
class FakeSessionService:
    """The minimum of WebSessionHost the catalogue routes read."""

    active_session_id: str | None = "session-1"
    calls: list[dict[str, Any]] = field(default_factory=list)
    services: Any = field(default_factory=SimpleNamespace)

    async def health(self) -> dict[str, Any]:
        return {"status": "ok", "version": "test", "active_session_id": self.active_session_id}

    async def list_sessions(
        self,
        *,
        include_archived: bool = False,
        limit: int | None = SESSION_LIST_LIMIT,
        offset: int = 0,
        query: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "include_archived": include_archived,
                "limit": limit,
                "offset": offset,
                "query": query,
            }
        )
        return {
            "sessions": [],
            "active_session_id": self.active_session_id,
            "total": 0,
            "limit": limit,
            "offset": offset,
            "has_more": False,
            "query": query,
        }


DEFAULT_CALL = {
    "include_archived": False,
    "limit": SESSION_LIST_LIMIT,
    "offset": 0,
    "query": None,
}


async def test_sessions_route_defaults_to_the_first_page() -> None:
    service = FakeSessionService()
    async with _client(service) as client:
        response = await client.get("/api/ui/sessions")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert service.calls == [DEFAULT_CALL]
    body = response.json()
    assert body["active_session_id"] == "session-1"
    assert body["total"] == 0
    assert body["limit"] == SESSION_LIST_LIMIT
    assert body["offset"] == 0
    assert body["has_more"] is False
    assert body["query"] is None
    assert body["sessions"] == []


async def test_sessions_route_forwards_paging_and_search() -> None:
    service = FakeSessionService()
    async with _client(service) as client:
        response = await client.get(
            "/api/ui/sessions",
            params={
                "limit": "2",
                "offset": "1",
                "query": "  old  ",
                "include_archived": "true",
            },
        )

    assert response.status_code == 200
    assert service.calls == [{"include_archived": True, "limit": 2, "offset": 1, "query": "old"}]
    body = response.json()
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert body["query"] == "old"


async def test_sessions_route_accepts_explicit_false_boolean() -> None:
    service = FakeSessionService()
    async with _client(service) as client:
        response = await client.get("/api/ui/sessions", params={"include_archived": "off"})

    assert response.status_code == 200
    assert service.calls == [DEFAULT_CALL]


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "abc"},
        {"limit": "-1"},
        {"limit": str(MAX_SESSION_LIST_LIMIT + 1)},
        {"offset": "-1"},
        {"offset": "later"},
        {"include_archived": "maybe"},
    ],
)
async def test_sessions_route_rejects_invalid_query_parameters(
    params: dict[str, str],
) -> None:
    service = FakeSessionService()
    async with _client(service) as client:
        response = await client.get("/api/ui/sessions", params=params)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert service.calls == []


async def test_legacy_sessions_route_keeps_the_old_call_shape() -> None:
    service = FakeSessionService()
    async with _client(service) as client:
        response = await client.get("/api/sessions")

    assert response.status_code == 200
    # Legacy clients pass no arguments; they still get the first page plus the
    # additive metadata keys instead of a hard error.
    assert service.calls == [DEFAULT_CALL]
    body = response.json()
    assert body["sessions"] == []
    assert body["active_session_id"] == "session-1"


async def test_bootstrap_still_returns_the_first_page() -> None:
    service = FakeSessionService()
    async with _client(service) as client:
        response = await client.get("/api/ui/bootstrap")

    assert response.status_code == 200
    assert service.calls == [DEFAULT_CALL]
    body = response.json()
    assert body["version"] == "test"
    assert body["health"]["status"] == "ok"
    assert body["sessions"] == []
    assert body["active_session_id"] == "session-1"
    assert any(command["name"] == "new" for command in body["commands"])
    assert body["presets"] == []
    assert body["permissions"] == []
    assert body["plugins"] == []
    assert body["providers"] == []
    assert body["defaults"] == {}


# -- host-level catalogue behaviour ---------------------------------------


def _info(
    index: int,
    *,
    title: str | None = None,
    cwd: str = "D:/work",
    preset: str = "standard",
    archived: bool = False,
    turns: int = 1,
) -> NativeSessionInfo:
    ident = f"session-{index:03d}"
    created_at = 1_000_000 - index
    return NativeSessionInfo(
        id=ident,
        path=Path(f"{ident}.jsonl"),
        cwd=cwd,
        created_at=created_at,
        updated_at=created_at,
        title=title or f"session {index}",
        turns=turns,
        event_count=turns * 2,
        preset=preset,
        parent_id=None,
        origin="user",
        preview=None,
        archived=archived,
    )


class FakeSessionsPlugin:
    """One native session plugin with the real native_list signature."""

    def __init__(self, infos: list[NativeSessionInfo]) -> None:
        self.infos = list(infos)
        self.calls: list[dict[str, Any]] = []

    def native_list(
        self,
        *,
        include_archived: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[NativeSessionInfo]:
        self.calls.append({"include_archived": include_archived, "limit": limit, "offset": offset})
        rows = [info for info in self.infos if include_archived or not info.archived]
        if limit is None:
            return rows[offset:]
        return rows[offset : offset + limit]


def _host(tmp_path: Path, infos: list[NativeSessionInfo]) -> tuple[WebSessionHost, FakeSessionsPlugin]:
    plugin = FakeSessionsPlugin(infos)
    host = WebSessionHost(SimpleNamespace(sessions=plugin), default_cwd=tmp_path)
    return host, plugin


async def test_host_defaults_to_one_page_of_the_native_index(tmp_path: Path) -> None:
    host, plugin = _host(tmp_path, [_info(index) for index in range(1, 61)])

    page = await host.list_sessions()

    assert len(page["sessions"]) == SESSION_LIST_LIMIT == 50
    assert page["total"] == 60
    assert page["limit"] == SESSION_LIST_LIMIT
    assert page["offset"] == 0
    assert page["has_more"] is True
    assert page["sessions"][0]["session_id"] == "session-001"
    assert page["sessions"][-1]["session_id"] == "session-050"
    # One full native snapshot, not a pre-sliced native page.
    assert plugin.calls == [{"include_archived": False, "limit": None, "offset": 0}]


async def test_host_offset_returns_the_tail_page(tmp_path: Path) -> None:
    host, _ = _host(tmp_path, [_info(index) for index in range(1, 61)])

    page = await host.list_sessions(limit=5, offset=55)

    assert [row["session_id"] for row in page["sessions"]] == [
        f"session-{index:03d}" for index in range(56, 61)
    ]
    assert page["total"] == 60
    assert page["offset"] == 55
    assert page["has_more"] is False


async def test_host_limit_none_returns_the_whole_catalogue(tmp_path: Path) -> None:
    host, _ = _host(tmp_path, [_info(index) for index in range(1, 61)])

    page = await host.list_sessions(limit=None)

    assert len(page["sessions"]) == 60
    assert page["total"] == 60
    assert page["limit"] == 60
    assert page["has_more"] is False


async def test_host_clamps_oversized_limits(tmp_path: Path) -> None:
    host, _ = _host(tmp_path, [_info(index) for index in range(1, 61)])

    page = await host.list_sessions(limit=MAX_SESSION_LIST_LIMIT + 50)

    assert page["limit"] == MAX_SESSION_LIST_LIMIT
    assert len(page["sessions"]) == 60
    assert page["has_more"] is False


async def test_host_query_matches_title_id_cwd_and_preset(tmp_path: Path) -> None:
    infos = [
        _info(1, title="Refactor the PARSER"),
        _info(2, title="Draft release notes"),
        _info(3, title="Team standup", preset="team"),
        _info(4, title="Other", cwd="D:/projects/Parser"),
    ]
    host, _ = _host(tmp_path, infos)

    by_title = await host.list_sessions(query="parser")
    assert [row["session_id"] for row in by_title["sessions"]] == ["session-001", "session-004"]
    assert by_title["total"] == 2

    by_preset = await host.list_sessions(query="TEAM")
    assert [row["session_id"] for row in by_preset["sessions"]] == ["session-003"]

    by_id = await host.list_sessions(query="session-002")
    assert [row["session_id"] for row in by_id["sessions"]] == ["session-002"]

    no_match = await host.list_sessions(query="nothing-here")
    assert no_match["sessions"] == []
    assert no_match["total"] == 0
    assert no_match["has_more"] is False


async def test_host_include_archived_off_by_default(tmp_path: Path) -> None:
    infos = [_info(1), _info(2), _info(3, archived=True)]
    host, plugin = _host(tmp_path, infos)

    visible = await host.list_sessions()
    assert visible["total"] == 2
    assert visible["sessions"][-1]["archived"] is False

    both = await host.list_sessions(include_archived=True)
    assert both["total"] == 3
    assert both["sessions"][-1]["archived"] is True
    assert plugin.calls[-1]["include_archived"] is True


async def test_host_rejects_invalid_catalogue_arguments(tmp_path: Path) -> None:
    host, _ = _host(tmp_path, [_info(1)])

    with pytest.raises(ValueError, match="limit must be an int or None"):
        await host.list_sessions(limit="5")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="offset must be an int"):
        await host.list_sessions(offset="1")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-negative"):
        await host.list_sessions(limit=-1)
    with pytest.raises(ValueError, match="non-negative"):
        await host.list_sessions(offset=-1)
    with pytest.raises(ValueError, match="query must be a string or None"):
        await host.list_sessions(query=123)  # type: ignore[arg-type]

    # A blank query is not a search: it returns the page unfiltered.
    blank = await host.list_sessions(query="  ")
    assert blank["total"] == 1
    assert blank["query"] is None
