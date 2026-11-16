"""Name a session from its first exchange: a fallback, then one LLM gist.

The store derives a short fallback title from the first user message, so a new
session is never called ``(untitled)``. This plugin upgrades that title once,
after the first turn finishes, using a single small request. Both records follow
DSH's shape:

    session/title-llm-request {titleProvider, messageSeqs, route, system, messages, maxTokens}
    session/title              {title, messageSeqs, source: {kind: "llm"}}

Nothing here can break a turn: the hook only schedules a task, every failure is
logged and swallowed, and an existing title (a rename, or one a plugin wrote) is
never overwritten.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, ClassVar

from pydantic import BaseModel

from deeppyro.api import Plugin
from deeppyro.api.services import require_service
from deeppyro.builtin.llm import LlmPlugin
from deeppyro.core.ids import new_message_id
from deeppyro.core.protocols import LlmRequest, TextDelta
from deeppyro.core.wire import Message, TextPart
from deeppyro.hooks import AGENT_TURN_STOPPING
from deeppyro.hooks.service import HookService
from deeppyro.kernel.config_service import KernelConfig
from deeppyro.session.metadata import (
    SOURCE_LLM,
    TITLE_LLM_REQUEST_EVENT,
    TITLE_PROVIDER_FIRST_PROMPT,
    collapse,
    existing_title,
    first_user_message,
    record_title,
)

logger = logging.getLogger(__name__)

TITLE_PROMPT_SOURCE = "dpyro-session-title-llm"
#: DSH's prompt, kept verbatim so titles read the same in both harnesses.
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
_TITLE_PREFIXES = ("title:", "title -", "\u6807\u9898\uff1a", "\u6807\u9898:")
_TRAILING = " \t。．.:：,，;；!！?？"


class TitleOptions(BaseModel):
    """The ``[title]`` table of the user config."""

    enabled: bool = True
    maxChars: int = 24
    model: str = ""
    maxTokens: int = MAX_TOKENS
    timeout: float = TIMEOUT_SECONDS


class SessionTitlePlugin(Plugin):
    """Upgrade the derived title of a session with one LLM gist."""

    api_version = 1
    plugin_id = "session-title"
    version = "0.1.0"
    description = "name a session from its first exchange (one small request)"
    inject: ClassVar[list[str]] = ["hooks", "llm", "config"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx, config)
        self._ctx = ctx
        self.options = options_for(ctx)
        self._tasks: set[asyncio.Task[None]] = set()

    async def setup(self) -> None:
        """Subscribe to the end of a turn; do nothing else at mount time."""
        if not self.options.enabled:
            return
        hooks = self.ctx.require(HookService)
        self.ctx.effect(
            lambda: hooks.on(AGENT_TURN_STOPPING, self._on_turn_stopping),
            label="session-title",
        )

    async def close(self) -> None:
        """Drop in-flight title requests."""
        tasks = list(self._tasks)
        self._tasks.clear()
        for task in tasks:
            task.cancel()

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
        if session is None or existing_title(getattr(session, "records", ())) is not None:
            return
        task = asyncio.create_task(self._name(agent, session))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- request -----------------------------------------------------------
    async def _name(self, agent: Any, session: Any) -> None:
        try:
            found = first_user_message(getattr(session, "records", ()))
            if found is None:
                return
            seq, text = found
            provider_name = str(getattr(agent, "provider_name", "") or "")
            model = self.options.model or str(getattr(agent, "model", "") or "")
            if not provider_name or not model:
                return
            prompt = _prompt(seq, text)
            session.append(
                TITLE_LLM_REQUEST_EVENT,
                self._request(prompt, provider_name, model, seq),
            )
            answer = await self._complete(provider_name, model, prompt)
            title = clean_title(answer, limit=self.options.maxChars)
            if title:
                record_title(session, title, source={"kind": SOURCE_LLM})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a title must never break a session
            # A one-shot run (`dpyro --headless`) closes the provider as soon as
            # the turn ends, so an in-flight request can lose its connection.
            # That is not a failure worth reporting: the fallback title stands.
            name = type(exc).__name__
            if name in {"ClosedResourceError", "CancelledError"}:
                logger.debug("session title request abandoned: %s", name)
            else:
                logger.warning("session title request failed: %s: %s", name, exc)

    def _request(self, prompt: str, provider_name: str, model: str, seq: int) -> dict[str, Any]:
        return {
            "titleProvider": TITLE_PROVIDER_FIRST_PROMPT,
            "messageSeqs": [seq],
            "route": {"provider": provider_name, "model": model},
            "system": SYSTEM_PROMPT,
            "messages": [
                Message(
                    id=new_message_id(),
                    role="user",
                    content=[TextPart(text=prompt)],
                    source={"kind": TITLE_PROMPT_SOURCE},
                ).model_dump(mode="json", exclude_none=True)
            ],
            "maxTokens": self.options.maxTokens,
            "reasoningEffort": "off",
        }

    async def _complete(self, provider_name: str, model: str, prompt: str) -> str:
        provider = require_service(self._ctx, LlmPlugin).provider(provider_name)
        if provider is None:
            raise RuntimeError(f"unknown provider {provider_name!r}")
        request = LlmRequest(
            provider=provider_name,
            model=model,
            system=SYSTEM_PROMPT,
            messages=[
                Message(
                    id=new_message_id(),
                    role="user",
                    content=[TextPart(text=prompt)],
                    source={"kind": TITLE_PROMPT_SOURCE},
                )
            ],
            max_tokens=self.options.maxTokens,
        )
        chunks: list[str] = []
        async with asyncio.timeout(self.options.timeout):
            async for event in provider.stream(request):
                if isinstance(event, TextDelta):
                    chunks.append(event.text)
        return "".join(chunks)


def options_for(ctx: Any) -> TitleOptions:
    """Read ``[title]`` from the user config, falling back to the defaults."""
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
    return collapse(line, limit=limit) or None
