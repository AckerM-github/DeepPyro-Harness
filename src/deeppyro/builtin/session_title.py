"""Name a session from its first exchange: one small LLM request.

The native session fold gives a new session its fallback title, so it is never
called "(untitled)". This plugin upgrades that title once, after the first turn
finishes, using a single small request. Schema 1 has no v4 record vocabulary, so
the request detail rides a plugin event and the title is the core
session.title event:

    plugin.session-title.title-llm-request {titleProvider, messageSeqs, route, system, messages, maxTokens}
    session.title                          {title, source: "llm"}

Nothing here can break a turn: the hook only schedules a task, every failure is
logged and swallowed, and an existing title (a rename, or one a plugin wrote) is
never overwritten.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from typing import Any, ClassVar, Literal, cast

from pydantic import BaseModel

from deeppyro.api import Plugin, PluginConfig, PluginContext
from deeppyro.api.services import require_service
from deeppyro.builtin.llm import LlmPlugin
from deeppyro.core.ids import new_message_id, now_ms
from deeppyro.core.llm import ModelRequest, TextDelta
from deeppyro.core.messages import Message as NativeMessage
from deeppyro.core.messages import PluginSource, SystemSource, TextPart
from deeppyro.core.messages import message_text as native_message_text
from deeppyro.hooks import AGENT_TURN_STOPPING
from deeppyro.hooks.service import HookService
from deeppyro.kernel.config_service import KernelConfig
from deeppyro.session.schema import EventKind, NativeSession

logger = logging.getLogger(__name__)

#: The provider name recorded in the request detail; it never changes.
TITLE_PROVIDER_FIRST_PROMPT = "session-title-first-prompt-llm"
#: The plugin name carried as the message source of the naming request.
TITLE_PROMPT_SOURCE = "dpyro-session-title-llm"
SOURCE_LLM = "llm"
#: upstream's prompt, kept verbatim so titles read the same in both harnesses.
SYSTEM_PROMPT = (
    "Create a concise title for an AI coding-assistant session from the supplied human messages.\n"
    "Return only the title on one line, **in plain text of natural language**, with no quotes, "
    "prefix, explanation, Markdown, XML, or terminal control codes. No code is allowed.\n"
    "Use the language of the messages.\n"
    "Aim for about 5 words in non-CJK languages or 10 CJK characters.\n"
    "If the messages give little to name, still return a short best-effort title, such as "
    "Greeting, instead of explaining."
)
PROMPT_TEMPLATE = "Generate the session title from this JSON array of human messages:\n{payload}"
MAX_TOKENS = 64
TIMEOUT_SECONDS = 20.0
#: Schema 1 has no request record type; the request detail rides a plugin event.
NATIVE_TITLE_REQUEST_EVENT = "plugin.session-title.title-llm-request"
_TITLE_PREFIXES = ("title:", "title -", "\u6807\u9898\uff1a", "\u6807\u9898:")
_TRAILING = " \t\u3002\uff0e.:\uff1a,\uff0c;\uff1b!\uff01?\uff1f"


class TitleOptions(BaseModel):
    """The [title] table of the user config."""

    enabled: bool = True
    maxChars: int = 24
    model: str = ""
    maxTokens: int = MAX_TOKENS
    timeout: float = TIMEOUT_SECONDS


class SessionTitlePlugin(Plugin):
    """Upgrade the derived title of a session with one LLM gist."""

    plugin_id = "session-title"
    version = "0.3.0"
    description = "name a session from its first exchange (one small request)"
    inject: ClassVar[tuple[str, ...]] = ("hooks", "llm", "config")

    def __init__(self, ctx: PluginContext, config: PluginConfig = None) -> None:
        super().__init__(ctx, config)
        self._ctx = ctx
        self.options = options_for(ctx)
        self._tasks: set[asyncio.Task[None]] = set()

    async def setup(self) -> None:
        """Subscribe to the end of a turn; do nothing else at mount time."""
        if not self.options.enabled:
            return
        hooks = self.ctx.require(HookService)
        # Subscribe eagerly: effect() only records a teardown callback.
        self._dispose = hooks.on(AGENT_TURN_STOPPING, self._on_turn_stopping)
        self.ctx.effect(self._dispose, label="session-title")

    async def close(self) -> None:
        """Drop in-flight title requests."""
        tasks = list(self._tasks)
        self._tasks.clear()
        for task in tasks:
            task.cancel()

    def _schedule(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- hooks -------------------------------------------------------------
    def _on_turn_stopping(self, payload: Any) -> None:
        """Schedule the one-shot naming request; never await it inline."""
        getter = getattr(payload, "get", None)
        if getter is None:
            return
        agent = getter("agent")
        if agent is None or int(getattr(agent, "turn", 0) or 0) != 1:
            return
        session = getattr(agent, "session", None)
        if session is None:
            return
        native = _native_session_of(session)
        if native is None or _native_title(native.events) is not None:
            return
        self._schedule(self._name_native(agent, native))

    # -- native path -------------------------------------------------------
    async def _name_native(self, agent: Any, native: NativeSession) -> None:
        provider_name = str(getattr(agent, "provider_name", "") or "")
        model = self.options.model or str(getattr(agent, "model", "") or "")
        if not provider_name or not model:
            return
        await self.name_native_session(native, provider_name=provider_name, model=model)

    async def name_native_session(
        self,
        native_session: NativeSession,
        *,
        provider_name: str,
        model: str,
    ) -> str | None:
        """Name a schema 1 session; failures are logged and swallowed."""
        try:
            found = _first_native_user_message(native_session.events)
            if found is None:
                return None
            seq, text = found
            if not provider_name or not model:
                return None
            if _native_title(native_session.events) is not None:
                return None
            prompt = _prompt(seq, text)
            native_session.append(
                NATIVE_TITLE_REQUEST_EVENT,
                self._request(prompt, provider_name, model, seq),
            )
            answer = await self._complete(provider_name, model, prompt)
            title = clean_title(answer, limit=self.options.maxChars)
            if title:
                native_session.append(EventKind.SESSION_TITLE, {"title": title, "source": SOURCE_LLM})
                return title
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a title must never break a session
            name = type(exc).__name__
            if name in {"ClosedResourceError", "CancelledError"}:
                logger.debug("native session title request abandoned: %s", name)
            else:
                logger.warning("native session title request failed: %s: %s", name, exc)
            return None

    def _request(self, prompt: str, provider_name: str, model: str, seq: int) -> dict[str, Any]:
        return {
            # The shared constant is a plain str; the record always names this provider.
            "titleProvider": cast(Literal["session-title-first-prompt-llm"], TITLE_PROVIDER_FIRST_PROMPT),
            "messageSeqs": [seq],
            "route": {"provider": provider_name, "model": model},
            "system": SYSTEM_PROMPT,
            "messages": [
                _message("user", prompt, PluginSource(TITLE_PROMPT_SOURCE)).to_dict(),
            ],
            "maxTokens": self.options.maxTokens,
            "reasoningEffort": "off",
        }

    async def _complete(self, provider_name: str, model: str, prompt: str) -> str:
        provider = require_service(self._ctx, LlmPlugin).provider(provider_name)
        if provider is None:
            raise RuntimeError(f"unknown provider {provider_name!r}")
        request = ModelRequest(
            provider=provider_name,
            model=model,
            system=_message("system", SYSTEM_PROMPT, SystemSource()),
            messages=(_message("user", prompt, PluginSource(TITLE_PROMPT_SOURCE)),),
            max_tokens=self.options.maxTokens,
        )
        chunks: list[str] = []
        async with asyncio.timeout(self.options.timeout):
            async for event in provider.stream(request):
                if isinstance(event, TextDelta):
                    chunks.append(event.text)
        return "".join(chunks)


def options_for(ctx: PluginContext) -> TitleOptions:
    """Read [title] from the user config, falling back to the defaults."""
    try:
        cfg = ctx.require(KernelConfig).value
    except Exception:  # noqa: BLE001 - the plugin must load without a config service
        return TitleOptions()
    table = getattr(cfg, "title", None)
    if table is None and isinstance(cfg, dict):
        table = cfg.get("title")
    if isinstance(table, TitleOptions):
        return table
    if isinstance(table, dict):
        try:
            return TitleOptions.model_validate(table)
        except Exception:  # noqa: BLE001 - a broken table must not brick boot
            logger.warning("[title] is invalid, using the defaults")
    return TitleOptions()


def _message(role: Literal["system", "user"], text: str, source: Any) -> NativeMessage:
    return NativeMessage(
        id=new_message_id(),
        role=role,
        content=(TextPart(text),),
        created_at=now_ms(),
        source=source,
    )


def _collapse(text: object, *, limit: int) -> str:
    """Collapse whitespace and truncate to limit characters (native title rule)."""
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(1, limit - 1)].rstrip() + "\u2026"


def _prompt(seq: int, text: str) -> str:
    payload = json.dumps([{"seq": seq, "text": text}], ensure_ascii=False)
    return PROMPT_TEMPLATE.format(payload=payload)


def clean_title(text: object, *, limit: int) -> str | None:
    """Take the first non-empty line and strip quotes, prefixes and Markdown."""
    lines = [line.strip() for line in str(text or "").splitlines()]
    line = next((line for line in lines if line), "")
    line = line.strip().strip("`*\\\"'\u201c\u201d\u2018\u2019\u300c\u300d\u300e\u300f").strip()
    lowered = line.lower()
    for prefix in _TITLE_PREFIXES:
        if lowered.startswith(prefix):
            line = line[len(prefix) :].strip()
            break
    line = line.rstrip(_TRAILING)
    return _collapse(line, limit=limit) or None


def _native_session_of(session: object) -> NativeSession | None:
    """Return the NativeSession behind a hook payload session, if any."""
    if isinstance(session, NativeSession):
        return session
    candidate = getattr(session, "native_session", None)
    return candidate if isinstance(candidate, NativeSession) else None


def _native_title(events: Sequence[Any]) -> str | None:
    """The last non-empty native session.title title, if one was recorded."""
    for event in reversed(list(events)):
        if getattr(event, "type", None) != EventKind.SESSION_TITLE.value:
            continue
        data = getattr(event, "data", None)
        if not isinstance(data, dict):
            continue
        title = data.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    return None


def _first_native_user_message(events: Sequence[Any]) -> tuple[int, str] | None:
    """Locate the first native user message as (seq, text), or None."""
    for event in events:
        if getattr(event, "type", None) != EventKind.USER_MESSAGE.value:
            continue
        data = getattr(event, "data", None)
        text = _native_message_text(data.get("message") if isinstance(data, dict) else None)
        if text:
            return int(getattr(event, "seq", 0) or 0), text
    return None


def _native_message_text(raw: object) -> str:
    if not isinstance(raw, dict):
        return ""
    try:
        return native_message_text(NativeMessage.from_dict(dict(raw)))
    except Exception:  # noqa: BLE001 - malformed payloads must not raise here
        return ""
