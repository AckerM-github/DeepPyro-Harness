"""The P1 JSON API shared by the browser shell and the legacy clients.

Every route is a thin projection over WebSessionHost: no agent logic lives here,
and no route can reach past the host's native session handle. The API is mounted
behind the loopback bearer-cookie middleware, so handlers never see anonymous
traffic.

Two route families are served from one router:

* `/api/ui/*` is what the P1 shell calls (bootstrap, timeline, session actions).
* `/api/*` keeps the original 0.3 clients working (health, sessions, messages,
  interrupt, SSE) while the shell is the only first-party UI.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from deeppyro.builtin.commands_builtin import core_command_specs
from deeppyro.builtin.settings import SettingsPlugin
from deeppyro.core.errors import ConfigError
from deeppyro.plugins.errors import PluginError, SettingsConflict
from deeppyro.web.events import HEARTBEAT_FRAME
from deeppyro.web.security import sse_headers
from deeppyro.web.service import MAX_SESSION_LIST_LIMIT, SESSION_LIST_LIMIT
from deeppyro.web.ui.commands import execute as execute_command
from deeppyro.web.ui.live_apply import apply_settings_live, permission_default
from deeppyro.web.ui.settings import ConfigEditor, settings_payload, test_provider
from deeppyro.web.wire import (
    BadRequest,
    Conflict,
    CreateSessionRequest,
    NotFound,
    PayloadTooLarge,
    SendMessageRequest,
    ServiceUnavailable,
    parse_session_id,
)

#: Bounded body accepted by the JSON API.
MAX_BODY_BYTES = 1024 * 1024

#: Decisions the approval prompt accepts from the browser.
ALLOWED_DECISIONS = frozenset({"allow", "deny", "expire", "cancel"})

#: Approval scopes the browser may ask for when allowing.
ALLOWED_SCOPES = frozenset({"once", "session"})

#: Commands grouped for the palette and the settings surface.
COMMAND_GROUPS: dict[str, str] = {
    "new": "session",
    "resume": "session",
    "sessions": "session",
    "rename": "session",
    "archive": "session",
    "unarchive": "session",
    "cwd": "session",
    "provider": "model",
    "model": "model",
    "preset": "model",
    "permission": "model",
    "thinking": "model",
    "plan": "work",
    "goal": "work",
    "jobs": "work",
    "team": "work",
    "status": "system",
    "compact": "system",
    "clear": "system",
    "hooks": "system",
    "plugins": "system",
    "panel": "system",
    "settings": "system",
    "help": "system",
    "exit": "system",
}


@dataclass(frozen=True, slots=True)
class UiContext:
    """Everything the P1 API may read outside the session host."""

    token: str
    version: str
    kernel_app: Any | None = None


def _service(provider: Callable[[], Any]) -> Any:
    service = provider()
    if service is None:
        raise ServiceUnavailable("the web session host is not running")
    return service


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise PayloadTooLarge(f"request body exceeds {MAX_BODY_BYTES} bytes")
    if not raw:
        return {}
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BadRequest(f"request body is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise BadRequest("request body must be a JSON object")
    return decoded


def _after(request: Request) -> int:
    raw = request.headers.get("last-event-id") or request.query_params.get("after")
    if raw is None or raw == "":
        return 0
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError) as exc:
        raise BadRequest("invalid SSE replay position") from exc


def _sid(request: Request) -> str:
    return parse_session_id(request.path_params["session_id"])


def _query_int(
    request: Request,
    name: str,
    *,
    default: int,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    """Parse one integer query parameter with explicit bounds."""
    raw = request.query_params.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise BadRequest(f'"{name}" must be an integer') from exc
    if value < minimum:
        raise BadRequest(f'"{name}" must be at least {minimum}')
    if maximum is not None and value > maximum:
        raise BadRequest(f'"{name}" must be at most {maximum}')
    return value


def _query_bool(request: Request, name: str, *, default: bool = False) -> bool:
    """Parse one boolean query parameter ("1/true/yes/on" or "0/false/no/off")."""
    raw = request.query_params.get(name)
    if raw is None or not raw.strip():
        return default
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise BadRequest(f'"{name}" must be a boolean')


def _query_text(request: Request, name: str) -> str | None:
    """Parse one optional text query parameter (blank means absent)."""
    raw = request.query_params.get(name)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def json_body(payload: Any, *, status: int = 200) -> JSONResponse:
    """One compact JSON response with the no-store UI cache policy."""
    return JSONResponse(
        payload,
        status_code=status,
        headers={"cache-control": "no-store"},
    )


def _commands() -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for spec in core_command_specs():
        takes_arg = "[" in spec.usage
        rendered.append(
            {
                "name": spec.name,
                "aliases": list(spec.aliases),
                "summary": spec.summary,
                "usage": spec.usage,
                "group": COMMAND_GROUPS.get(spec.name, "system"),
                "takes_arg": takes_arg,
                "addressable": True,
            }
        )
    return rendered


def _presets(service: Any) -> list[dict[str, Any]]:
    try:
        specs = service.services.presets.all()
    except Exception:  # noqa: BLE001 - the catalogue is advisory in the shell
        return []
    return [
        {
            "id": spec.id,
            "name": spec.name,
            "description": spec.description,
            "model": spec.model,
            "mode": str(spec.mode),
            "hidden": bool(spec.hidden),
            "order": spec.order,
        }
        for spec in specs
        # Team mode is a session-level switch (POST .../team), so the hidden
        # team preset is not offered as a starting preset.
        if not spec.hidden
    ]


def _permissions(service: Any) -> list[dict[str, Any]]:
    try:
        specs = service.services.permission_presets.catalog()
    except Exception:  # noqa: BLE001
        return []
    return [
        {
            "name": spec.name,
            "sandbox": str(spec.sandbox),
            "approval": str(spec.approval),
            "description": spec.description,
        }
        for spec in specs
    ]


def _plugins(context: UiContext) -> list[dict[str, Any]]:
    app = context.kernel_app
    if app is None or not hasattr(app, "row_states"):
        return []
    try:
        rows = app.row_states()
    except Exception:  # noqa: BLE001 - plugin introspection is advisory
        return []
    rendered: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            rendered.append(
                {
                    "id": str(row.get("id", "")),
                    "state": str(row.get("state", "")),
                    "version": str(row.get("version", "")),
                    "description": str(row.get("description", "")),
                    "plugin_id": str(row.get("plugin_id", row.get("id", ""))),
                }
            )
    return rendered


def build_router(
    *,
    service_provider: Callable[[], Any],
    context: UiContext,
) -> APIRouter:
    """Build every JSON route; the caller mounts it on the ASGI app."""
    router = APIRouter()

    # -- P1 shell bootstrap ------------------------------------------------

    @router.get("/api/ui/bootstrap")
    async def bootstrap() -> JSONResponse:
        service = _service(service_provider)
        health = await service.health()
        sessions = await service.list_sessions()
        return json_body(
            {
                "version": context.version,
                "health": health,
                "sessions": sessions.get("sessions", []),
                "active_session_id": service.active_session_id,
                "commands": _commands(),
                "presets": _presets(service),
                "permissions": _permissions(service),
                "plugins": _plugins(context),
                "providers": _providers(service),
                "defaults": _defaults(service),
            }
        )

    @router.get("/api/ui/sessions")
    async def ui_sessions(request: Request) -> JSONResponse:
        # Paging/searching is opt-in: the response always carries total, limit,
        # offset and has_more, so a client can page without a second count call.
        limit = _query_int(
            request,
            "limit",
            default=SESSION_LIST_LIMIT,
            minimum=0,
            maximum=MAX_SESSION_LIST_LIMIT,
        )
        offset = _query_int(request, "offset", default=0, minimum=0)
        include_archived = _query_bool(request, "include_archived", default=False)
        query = _query_text(request, "query")
        service = _service(service_provider)
        return json_body(
            await service.list_sessions(
                include_archived=include_archived,
                limit=limit,
                offset=offset,
                query=query,
            )
        )

    @router.get("/api/ui/sessions/{session_id}/timeline")
    async def timeline(request: Request) -> JSONResponse:
        service = _service(service_provider)
        payload = await service.timeline(_sid(request))
        journal = getattr(service, "journal", None)
        if journal is not None:
            payload["journal_seq"] = journal.last_seq
        return json_body(payload)

    @router.get("/api/ui/sessions/{session_id}/team")
    async def session_team(request: Request) -> JSONResponse:
        return json_body(_service(service_provider).team_state(_sid(request)))

    @router.post("/api/ui/sessions/{session_id}/team")
    async def set_session_team(request: Request) -> JSONResponse:
        payload = await _body(request)
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise BadRequest('"enabled" must be a boolean')
        return json_body(await _service(service_provider).set_session_team(_sid(request), enabled))

    @router.post("/api/ui/sessions/{session_id}/model")
    async def set_session_model(request: Request) -> JSONResponse:
        payload = await _body(request)
        provider = payload.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            raise BadRequest('"provider" must be a non-empty string')
        model = payload.get("model")
        if not isinstance(model, str) or not model.strip():
            raise BadRequest('"model" must be a non-empty string')
        return json_body(
            await _service(service_provider).set_session_model(
                _sid(request),
                provider.strip(),
                model.strip(),
            )
        )

    @router.post("/api/ui/sessions")
    async def create_session(request: Request) -> JSONResponse:
        payload = CreateSessionRequest.from_payload(await _body(request))
        try:
            result = await _service(service_provider).create_session(payload)
        except (ValueError, ConfigError) as exc:
            raise _config_error(exc) from exc
        return json_body(result, status=201)

    @router.post("/api/ui/sessions/{session_id}/resume")
    async def resume_session(request: Request) -> JSONResponse:
        return json_body(await _service(service_provider).resume_session(_sid(request)))

    @router.post("/api/ui/sessions/{session_id}/messages")
    async def send_message(request: Request) -> JSONResponse:
        payload = SendMessageRequest.from_payload(await _body(request))
        result = await _service(service_provider).send_message(_sid(request), payload)
        return json_body(result, status=202)

    @router.post("/api/ui/sessions/{session_id}/interrupt")
    async def interrupt(request: Request) -> JSONResponse:
        return json_body(await _service(service_provider).interrupt(_sid(request)))

    @router.get("/api/ui/sessions/{session_id}/queue")
    async def queue(request: Request) -> JSONResponse:
        return json_body(await _service(service_provider).queue(_sid(request)))

    @router.delete("/api/ui/sessions/{session_id}/queue/{queue_id}")
    async def drop_queued(request: Request) -> JSONResponse:
        queue_id = request.path_params["queue_id"]
        return json_body(await _service(service_provider).drop_queued(_sid(request), queue_id))

    @router.post("/api/ui/sessions/{session_id}/rename")
    async def rename(request: Request) -> JSONResponse:
        payload = await _body(request)
        title = payload.get("title")
        if not isinstance(title, str) or not title.strip():
            raise BadRequest('"title" must be a non-empty string')
        return json_body(await _service(service_provider).rename_session(_sid(request), title))

    @router.post("/api/ui/sessions/{session_id}/archive")
    async def archive(request: Request) -> JSONResponse:
        payload = await _body(request)
        archived = payload.get("archived", True)
        if not isinstance(archived, bool):
            raise BadRequest('"archived" must be a boolean')
        return json_body(await _service(service_provider).archive_session(_sid(request), archived=archived))

    @router.post("/api/ui/sessions/{session_id}/permission")
    async def permission(request: Request) -> JSONResponse:
        payload = await _body(request)
        preset = payload.get("preset")
        if not isinstance(preset, str) or not preset.strip():
            raise BadRequest('"preset" must be a non-empty string')
        return json_body(await _service(service_provider).apply_permission(_sid(request), preset))

    @router.get("/api/ui/approvals")
    async def pending_approvals(request: Request) -> JSONResponse:
        raw = request.query_params.get("session_id")
        session_id = parse_session_id(raw) if raw else None
        service = _service(service_provider)
        return json_body({"approvals": service.pending_approvals(session_id)})

    @router.post("/api/ui/approvals/{approval_id}")
    async def decide_approval(request: Request, approval_id: str) -> JSONResponse:
        payload = await _body(request)
        decision = payload.get("decision")
        if not isinstance(decision, str) or decision not in ALLOWED_DECISIONS:
            raise BadRequest('"decision" must be one of allow, deny, expire, cancel')
        scope = payload.get("scope", "once")
        if not isinstance(scope, str) or scope not in ALLOWED_SCOPES:
            raise BadRequest('"scope" must be one of once, session')
        settled = _service(service_provider).decide_approval(approval_id, decision, scope=scope)
        if not settled:
            raise NotFound(f"approval {approval_id} is not pending")
        return json_body(
            {
                "approval_id": approval_id,
                "decision": decision,
                "settled": True,
                "scope": scope,
            }
        )

    # -- commands ----------------------------------------------------------

    @router.post("/api/ui/commands")
    async def run_command(request: Request) -> JSONResponse:
        payload = await _body(request)
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise BadRequest('"name" must be a non-empty string')
        raw_args = payload.get("args", [])
        if raw_args is None:
            raw_args = []
        if not isinstance(raw_args, list) or not all(isinstance(arg, str) for arg in raw_args):
            raise BadRequest('"args" must be a list of strings')
        raw_session = payload.get("session_id")
        if raw_session in (None, ""):
            session_id = None
        else:
            if not isinstance(raw_session, str):
                raise BadRequest('"session_id" must be a string')
            session_id = parse_session_id(raw_session)
        service = _service(service_provider)
        result = await execute_command(service, context, name, list(raw_args), session_id)
        return json_body(result.to_payload())

    # -- settings ----------------------------------------------------------

    def editor() -> ConfigEditor:
        return ConfigEditor()

    def _config_error(exc: BaseException) -> BadRequest:
        if isinstance(exc, (ValueError, ConfigError)):
            return BadRequest(str(exc))
        raise exc

    async def _live_apply(
        changed: Iterable[str],
        *,
        providers: Iterable[str] = (),
        previous_permission_default: str | None = None,
    ) -> dict[str, Any]:
        """Apply a just-saved config to the running kernel.

        A live-apply failure is reported in the response (the config file was
        already written), never swallowed and never turned into a fake success.
        """
        service = _service(service_provider)
        try:
            config = editor().load()
            return await apply_settings_live(
                app=context.kernel_app,
                service=service,
                config=config,
                changed=changed,
                providers=providers,
                previous_permission_default=previous_permission_default,
            )
        except Exception as exc:  # noqa: BLE001 - report, do not fail the save
            return {
                "applied": [],
                "restart_required": [],
                "deferred": False,
                "detail": f"live apply failed: {type(exc).__name__}: {exc}",
            }

    @router.get("/api/ui/settings")
    async def settings() -> JSONResponse:
        service = _service(service_provider)
        try:
            payload = settings_payload(
                context.kernel_app,
                editor(),
                service,
                context.version,
            )
        except (ValueError, ConfigError) as exc:
            raise _config_error(exc) from exc
        return json_body(payload)

    @router.patch("/api/ui/settings/agent")
    async def update_agent(request: Request) -> JSONResponse:
        payload = await _body(request)
        allowed = ("provider", "model", "temperature", "max_tokens", "max_steps")
        changes = {key: payload[key] for key in allowed if key in payload}
        if not changes:
            raise BadRequest("at least one agent setting is required")
        try:
            config = editor().update_agent(**changes)
        except (ValueError, ConfigError) as exc:
            raise _config_error(exc) from exc
        live = await _live_apply({f"agent.{key}" for key in changes})
        return json_body({"ok": True, "config": config, "live": live})

    @router.patch("/api/ui/settings/provider/{name}")
    async def update_provider(request: Request, name: str) -> JSONResponse:
        payload = await _body(request)
        allowed = ("base_url", "api_key", "api_key_env")
        changes = {key: payload[key] for key in allowed if key in payload}
        if not changes:
            raise BadRequest("at least one provider setting is required")
        try:
            config = editor().update_provider(name, **changes)
        except (ValueError, ConfigError) as exc:
            raise _config_error(exc) from exc
        live = await _live_apply({"provider"}, providers=(name,))
        return json_body({"ok": True, "config": config, "live": live})

    @router.post("/api/ui/settings/providers/{name}/test")
    async def probe_provider(name: str) -> JSONResponse:
        service = _service(service_provider)
        llm = getattr(service.services, "llm", None)
        if llm is None:
            raise ServiceUnavailable("llm service is not available")
        return json_body(await test_provider(editor(), name, llm))

    def settings_store() -> Any:
        app = context.kernel_app
        if app is None:
            raise ServiceUnavailable("the plugin host is not available on this surface")
        try:
            return app.require(SettingsPlugin)
        except Exception as exc:
            raise ServiceUnavailable("the settings plugin is not running") from exc

    @router.get("/api/ui/plugins/{plugin_id}/settings")
    async def plugin_settings(plugin_id: str) -> JSONResponse:
        try:
            return json_body(settings_store().describe(plugin_id))
        except SettingsConflict as exc:
            raise Conflict(str(exc)) from exc
        except PluginError as exc:
            raise NotFound(str(exc)) from exc
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc

    @router.put("/api/ui/plugins/{plugin_id}/settings")
    async def update_plugin_settings(request: Request, plugin_id: str) -> JSONResponse:
        payload = await _body(request)
        values = payload.get("values")
        if not isinstance(values, dict):
            raise BadRequest('"values" must be an object')
        if not values:
            raise BadRequest('"values" must contain at least one field')
        store = settings_store()
        try:
            described = store.describe(plugin_id)
            expected = payload.get("expected_revision") or described.get("revision") or ""
            if not isinstance(expected, str) or not expected:
                raise BadRequest("the settings revision is not available")
            written = store.write(plugin_id, values, expected)
        except SettingsConflict as exc:
            raise Conflict(str(exc)) from exc
        except PluginError as exc:
            raise BadRequest(str(exc)) from exc
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
        return json_body(
            {
                "plugin_id": plugin_id,
                "revision": written.get("revision", ""),
                "values": written.get("values", {}),
                "changed": written.get("changed", ()),
                "live_changed": written.get("live_changed", ()),
                "restart_fields": written.get("restart_fields", ()),
                "restart_required": bool(written.get("restart_required", False)),
                "apply": written.get("apply", ""),
            }
        )

    @router.post("/api/ui/settings/permission")
    async def update_permission(request: Request) -> JSONResponse:
        payload = await _body(request)
        preset = payload.get("preset")
        if not isinstance(preset, str) or not preset.strip():
            raise BadRequest('"preset" must be a non-empty string')
        try:
            previous = permission_default(editor().load())
            config = editor().set_permission_default(preset)
        except (ValueError, ConfigError) as exc:
            raise _config_error(exc) from exc
        live = await _live_apply(
            {"permission.defaultPreset"},
            previous_permission_default=previous,
        )
        return json_body({"ok": True, "config": config, "live": live})

    # -- filesystem picker -------------------------------------------------

    @router.get("/api/ui/fs/dirs")
    async def fs_dirs(request: Request) -> JSONResponse:
        raw = request.query_params.get("path")
        path = raw if raw is not None and raw.strip() else None
        return json_body(await _service(service_provider).fs_dirs(path))

    @router.get("/api/ui/fs/roots")
    async def fs_roots() -> JSONResponse:
        return json_body(await _service(service_provider).fs_roots())

    # -- legacy API family -------------------------------------------------

    @router.get("/api/health")
    async def health() -> JSONResponse:
        return json_body(await _service(service_provider).health())

    @router.get("/api/sessions")
    async def legacy_sessions() -> JSONResponse:
        return json_body(await _service(service_provider).list_sessions())

    @router.post("/api/sessions")
    async def legacy_create(request: Request) -> JSONResponse:
        payload = CreateSessionRequest.from_payload(await _body(request))
        try:
            result = await _service(service_provider).create_session(payload)
        except (ValueError, ConfigError) as exc:
            raise _config_error(exc) from exc
        return json_body(result, status=201)

    @router.get("/api/sessions/{session_id}")
    async def legacy_status(request: Request) -> JSONResponse:
        return json_body(await _service(service_provider).status(_sid(request)))

    @router.post("/api/sessions/{session_id}/resume")
    async def legacy_resume(request: Request) -> JSONResponse:
        return json_body(await _service(service_provider).resume_session(_sid(request)))

    @router.get("/api/sessions/{session_id}/transcript")
    async def legacy_transcript(request: Request) -> JSONResponse:
        return json_body(await _service(service_provider).transcript(_sid(request)))

    @router.post("/api/sessions/{session_id}/messages")
    async def legacy_send(request: Request) -> JSONResponse:
        payload = SendMessageRequest.from_payload(await _body(request))
        return json_body(await _service(service_provider).send_message(_sid(request), payload), status=202)

    @router.post("/api/sessions/{session_id}/interrupt")
    async def legacy_interrupt(request: Request) -> JSONResponse:
        return json_body(await _service(service_provider).interrupt(_sid(request)))

    # -- SSE ---------------------------------------------------------------

    async def _events(request: Request) -> Response:
        service = _service(service_provider)
        session_id = _sid(request)
        after = _after(request)

        async def stream() -> AsyncIterator[bytes]:
            yield HEARTBEAT_FRAME
            async for frame in service.sse(session_id, after=after):
                yield frame

        return StreamingResponse(stream(), media_type="text/event-stream", headers=dict(sse_headers()))

    router.add_api_route("/api/ui/sessions/{session_id}/events", _events, methods=["GET"])
    router.add_api_route("/api/sessions/{session_id}/events", _events, methods=["GET"])

    return router


def _providers(service: Any) -> list[dict[str, Any]]:
    config = getattr(service.services, "config", None)
    if config is None:
        return []
    selected = config.agent.provider
    rendered: list[dict[str, Any]] = []
    for name, provider in config.providers.items():
        rendered.append(
            {
                "id": name,
                "api": provider.api,
                "base_url": provider.base_url,
                "configured": bool(provider.api_key or provider.api_key_env),
                "selected": name == selected,
                "models": [
                    {
                        "id": model.id,
                        "name": model.name or model.id,
                        "context_window": model.context_window,
                    }
                    for model in provider.models
                ],
            }
        )
    return rendered


def _defaults(service: Any) -> dict[str, Any]:
    config = getattr(service.services, "config", None)
    if config is None:
        return {}
    agent = config.agent
    return {
        "provider": agent.provider,
        "model": agent.model,
        "temperature": agent.temperature,
        "max_tokens": agent.max_tokens,
        "max_steps": agent.max_steps,
        "cwd": str(getattr(service, "default_cwd", "") or ""),
    }


__all__ = [
    "ALLOWED_DECISIONS",
    "ALLOWED_SCOPES",
    "COMMAND_GROUPS",
    "MAX_BODY_BYTES",
    "UiContext",
    "build_router",
    "json_body",
]
