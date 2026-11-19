"""UI-addressable commands for the web surface.

The web host does not reuse the Textual command dispatcher: that one talks to
widgets. This module translates the same command registry into JSON results the
browser can render, and it refuses to pretend: commands whose backing service is
not wired to the web host answer with an explicit error instead of a silent
success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from deeppyro.builtin.commands_builtin import core_command_specs
from deeppyro.core.errors import ConfigError
from deeppyro.web.service import WebSessionHost
from deeppyro.web.wire import CreateSessionRequest, HttpError

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_REDIRECT = "redirect"

SETTINGS_SECTIONS: dict[str, str] = {
    "agent": "agent",
    "behavior": "agent",
    "model": "provider",
    "models": "provider",
    "provider": "provider",
    "permission": "permissions",
    "permissions": "permissions",
    "commands": "commands",
    "plugin": "plugins",
    "plugins": "plugins",
    "sessions": "sessions",
    "web": "web",
    "about": "about",
}


@dataclass(frozen=True, slots=True)
class CommandResult:
    """One command outcome, encoded for POST /api/ui/commands."""

    status: str
    message: str = ""
    redirect: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status, "message": self.message}
        if self.redirect is not None:
            payload["redirect"] = self.redirect
        if self.data:
            payload["data"] = self.data
        return payload


def _ok(message: str = "", **data: Any) -> CommandResult:
    return CommandResult(STATUS_OK, message, data=data)


def _error(message: str) -> CommandResult:
    return CommandResult(STATUS_ERROR, message)


def _redirect(path: str, message: str) -> CommandResult:
    return CommandResult(STATUS_REDIRECT, message, redirect=path)


async def execute(
    service: WebSessionHost,
    context: Any,
    name: str,
    args: list[str] | None = None,
    session_id: str | None = None,
) -> CommandResult:
    """Execute one command against the live web host."""
    command = (name or "").strip().lstrip("/").lower()
    values = [str(value) for value in (args or [])]
    target = session_id or service.active_session_id
    try:
        return await _dispatch(service, context, command, values, target)
    except HttpError as exc:
        return _error(exc.message)
    except (ValueError, ConfigError) as exc:
        return _error(str(exc))


async def _dispatch(
    service: WebSessionHost,
    context: Any,
    command: str,
    args: list[str],
    target: str | None,
) -> CommandResult:
    if command in {"help", "h", "?"}:
        return _help()
    if command == "new":
        title = " ".join(args).strip()
        created = await service.create_session(
            CreateSessionRequest(cwd="", preset="", provider="", model="", title=title)
        )
        return CommandResult(
            STATUS_OK,
            f"Started session {created['session_id']}.",
            data={"session_id": created["session_id"], "session": created},
        )
    if command == "resume":
        return await _resume(service, args, target)
    if command == "sessions":
        return await _sessions(service)
    if command == "rename":
        if not target:
            return _error("no session is open")
        if not args:
            return _error("usage: /rename <title>")
        renamed = await service.rename_session(target, " ".join(args))
        return _ok(f"Renamed to {renamed['title']!r}.", **renamed)
    if command == "archive":
        return await _archive(service, target, args, archived=True)
    if command == "unarchive":
        if not args:
            return _error("usage: /unarchive <session-id-or-title>")
        resolved = await _resolve_session(service, args[0], target)
        if resolved is None:
            return _error(f"no session matches {args[0]!r}")
        archived = await service.archive_session(resolved, archived=False)
        return _ok(f"Restored {resolved}.", **archived)
    if command == "permission":
        return await _permission(service, target, args)
    if command == "status":
        return await _status(service, target)
    if command == "plan":
        return await _plan(service, target)
    if command == "goal":
        return await _goal(service, target)
    if command == "cwd":
        return await _cwd(service, target, args)
    if command == "plugins":
        return _plugins(context)
    if command == "settings":
        section = args[0].lower() if args else ""
        path = f"/settings/{SETTINGS_SECTIONS[section]}" if section in SETTINGS_SECTIONS else "/settings"
        return _redirect(path, "Opened settings.")
    if command == "panel":
        return _redirect("/settings/plugins", "Opened plugin settings.")
    if command == "clear":
        return _ok("Cleared the view.", action="clear")
    if command in {"provider", "model", "preset"}:
        return await _selection(service, command, args)
    if command == "compact":
        return _error("compaction is not wired to the web surface yet; the runtime compacts before send")
    if command in {"jobs", "team", "thinking", "hooks"}:
        return _error(f"/{command} is not available on the web surface yet")
    if command in {"exit", "quit", "q"}:
        return _error("close the browser tab or press Ctrl-C in the terminal to stop the host")
    return _error(f"unknown command: /{command}")


def _help() -> CommandResult:
    rows = [
        {"name": spec.name, "summary": spec.summary, "usage": spec.usage, "aliases": list(spec.aliases)}
        for spec in core_command_specs()
    ]
    text = "\n".join(f"/{row['name']} - {row['summary']}" for row in rows)
    return _ok(text, commands=rows)


async def _resume(service: WebSessionHost, args: list[str], target: str | None) -> CommandResult:
    sessions = (await service.list_sessions(include_archived=True))["sessions"]
    if not sessions:
        return _error("no sessions to resume")
    choice: str | None = None
    if args:
        value = args[0]
        if value.isdigit():
            index = int(value) - 1
            if index < 0 or index >= len(sessions):
                return _error(f"session index out of range: {value}")
            choice = sessions[index]["session_id"]
        else:
            resolved = await _resolve_session(service, value, target)
            if resolved is None:
                return _error(f"no session matches {value!r}")
            choice = resolved
    else:
        choice = sessions[0]["session_id"]
    if not choice:
        return _error("no resumable session found")
    resumed = await service.resume_session(choice)
    return _ok(
        f"Resumed session {resumed['session_id']}.",
        session_id=resumed["session_id"],
        session=resumed,
    )


async def _sessions(service: WebSessionHost) -> CommandResult:
    catalog = await service.list_sessions(include_archived=True)
    rows = catalog["sessions"]
    lines = []
    for row in rows:
        marks = []
        if row["active"]:
            marks.append("active")
        if row["archived"]:
            marks.append("archived")
        suffix = f" [{', '.join(marks)}]" if marks else ""
        lines.append(f"{row['session_id']}  {row['title']}  ({row['turns']} turns){suffix}")
    return _ok("\n".join(lines) if lines else "No sessions yet.", sessions=rows)


async def _archive(
    service: WebSessionHost, target: str | None, args: list[str], *, archived: bool
) -> CommandResult:
    target_id = target
    if args:
        resolved = await _resolve_session(service, args[0], target)
        if resolved is None:
            return _error(f"no session matches {args[0]!r}")
        target_id = resolved
    if not target_id:
        return _error("no session is open")
    was_active = target_id == service.active_session_id
    result = await service.archive_session(target_id, archived=archived)
    message = f"Archived {target_id}." if archived else f"Restored {target_id}."
    data: dict[str, Any] = dict(result)
    if archived and was_active:
        created = await service.create_session(CreateSessionRequest())
        message += f" Started {created['session_id']}."
        data["session_id"] = created["session_id"]
        data["session"] = created
    return _ok(message, **data)


async def _permission(service: WebSessionHost, target: str | None, args: list[str]) -> CommandResult:
    names = list(service.services.permission_presets.names())
    if not args:
        current = None
        if target:
            try:
                status = await service.status(target)
            except HttpError:
                current = None
            else:
                current = status.get("permission_preset")
        return _ok(
            f"Permission presets: {', '.join(names)}. Current: {current or 'unknown'}.",
            presets=names,
            current=current,
        )
    if not target:
        return _error("no session is open")
    applied = await service.apply_permission(target, args[0])
    return _ok(f"Permission preset set to {applied['permission_preset']}.", **applied)


async def _status(service: WebSessionHost, target: str | None) -> CommandResult:
    if not target:
        return _error("no session is open")
    status = await service.status(target)
    facts = (await service.facts(target))["state"]
    lines = [
        f"session: {status['session_id']}",
        f"title: {status.get('title') or '(untitled)'}",
        f"model: {status.get('provider')}/{status.get('model')}",
        f"preset: {status.get('preset')}",
        f"permission: {status.get('permission_preset')}",
        f"cwd: {status.get('cwd')}",
        f"turns: {facts.get('rounds_started', 0)} rounds",
        f"context: {status.get('context_tokens', 0)} / {status.get('context_window', 0)} tokens",
        f"busy: {bool(status.get('busy'))}",
    ]
    return _ok("\n".join(lines), status=status, state=facts)


async def _plan(service: WebSessionHost, target: str | None) -> CommandResult:
    if not target:
        return _error("no session is open")
    state = (await service.facts(target))["state"]
    todos = state.get("todos") or []
    plan_mode = state.get("plan_mode")
    lines = [f"plan mode: {plan_mode or 'off'}"]
    lines.extend(f"- [{item['status']}] {item['content']}" for item in todos)
    if not todos:
        lines.append("no todos")
    return _ok("\n".join(lines), plan_mode=plan_mode, todos=todos)


async def _goal(service: WebSessionHost, target: str | None) -> CommandResult:
    if not target:
        return _error("no session is open")
    state = (await service.facts(target))["state"]
    goal = state.get("goal")
    if not goal:
        return _ok("No goal is set.", goal=None)
    lines = [
        f"goal: {goal['objective']}",
        f"phase: {goal['phase']}",
        f"revision: {goal['revision']}",
        f"rounds: {state.get('rounds_started', 0)} / {goal['max_goal_rounds']}",
    ]
    if goal.get("blocked_reason"):
        lines.append(f"blocked: {goal['blocked_reason']['message']}")
    return _ok("\n".join(lines), goal=goal)


async def _cwd(service: WebSessionHost, target: str | None, args: list[str]) -> CommandResult:
    if args:
        requested = " ".join(args).strip()
        if not requested:
            return _error("usage: /cwd <absolute-path>")
        created = await service.create_session(
            CreateSessionRequest(cwd=requested, preset="", provider="", model="", title="")
        )
        workspace = created.get("cwd") or requested
        return _ok(
            f"Opened a new session in {workspace}. The previous session is still in the sidebar.",
            session_id=created["session_id"],
            session=created,
            cwd=workspace,
        )
    if not target:
        return _error("no session is open")
    status = await service.status(target)
    return _ok(
        f"Working directory: {status.get('cwd')}. "
        "Use /cwd <absolute-path> or the + button to start a session elsewhere.",
        cwd=status.get("cwd"),
    )


def _plugins(context: Any) -> CommandResult:
    app = getattr(context, "kernel_app", None)
    if app is None:
        return _error("the plugin host is not available on this surface")
    rows = [row for row in app.row_states() if not row.get("group")]
    active = sum(1 for row in rows if row.get("state") == "active")
    text = "\n".join(f"{row['id']}  {row['state']}" for row in rows)
    return _ok(f"{active} of {len(rows)} plugins active.\n{text}", plugins=rows)


async def _selection(service: WebSessionHost, command: str, args: list[str]) -> CommandResult:
    config = service.services.config
    if command == "provider":
        providers = sorted(config.providers)
        current = config.agent.provider
        if not args:
            return _ok(
                f"Provider: {current}. Available: {', '.join(providers)}.",
                current=current,
                providers=providers,
            )
        return _redirect(
            "/settings/provider",
            "provider selection is edited in settings and applies to new sessions",
        )
    if command == "model":
        current = config.agent.model
        provider = config.agent.provider
        models = (
            [model.id for model in config.providers[provider].models] if provider in config.providers else []
        )
        if not args:
            return _ok(
                f"Model: {current}. Configured: {', '.join(models) or 'none'}.",
                current=current,
                models=models,
            )
        return _redirect(
            "/settings/provider",
            "model selection is edited in settings and applies to new sessions",
        )
    ids = service.services.presets.ids()
    current = service.services.presets.default().id
    if not args:
        return _ok(f"Preset: {current}. Available: {', '.join(ids)}.", current=current, presets=ids)
    return _redirect(
        "/settings/agent",
        "preset selection is edited in settings and applies to new sessions",
    )


async def _resolve_session(service: WebSessionHost, value: str, active: str | None) -> str | None:
    if active and (value == active or active.startswith(value)):
        return active
    catalog = (await service.list_sessions(include_archived=True))["sessions"]
    for row in catalog:
        if row["session_id"] == value:
            return str(row["session_id"])
    matches = [row for row in catalog if value.lower() in str(row["title"]).lower()]
    if len(matches) == 1:
        return str(matches[0]["session_id"])
    prefix = [row for row in catalog if str(row["session_id"]).startswith(value)]
    if len(prefix) == 1:
        return str(prefix[0]["session_id"])
    return None


__all__ = ["SETTINGS_SECTIONS", "CommandResult", "execute"]
