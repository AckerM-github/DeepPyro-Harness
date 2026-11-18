"""The commands service: slash commands contributed by plugins."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Callable, Iterable
from typing import Any

from deeppyro.api import CommandSpec, PluginConfig, PluginContext, PluginDependencyError, Service
from deeppyro.api.services import lookup_service
from deeppyro.core.bus import EventBus

logger = logging.getLogger(__name__)


class CommandsPlugin(Service):
    """Registry of slash commands; the frontend resolves them through this."""

    provide = "commands"
    plugin_id = "commands"
    version = "0.2.0"
    description = "slash command registry"

    def __init__(self, ctx: PluginContext, config: PluginConfig) -> None:
        super().__init__(ctx, config)
        self._commands: list[CommandSpec] = []
        self._pending: set[asyncio.Task[Any]] = set()

    def add(
        self,
        name: str,
        handler: Any,
        *,
        summary: str = "",
        usage: str = "",
        aliases: Iterable[str] = (),
        plugin_id: str = "",
    ) -> Callable[[], None]:
        """Register one command; the returned callable removes it."""
        name = name.strip().lstrip("/").lower()
        if not name:
            raise PluginDependencyError("command name must not be empty")
        aliases = tuple(alias.strip().lstrip("/").lower() for alias in aliases if alias.strip())
        for existing in self._commands:
            if existing.matches(name) or any(existing.matches(alias) for alias in aliases):
                raise PluginDependencyError(
                    f"command {name!r} clashes with {existing.name!r} from {existing.plugin_id!r}"
                )
        spec = CommandSpec(
            name=name,
            summary=summary,
            usage=usage,
            aliases=aliases,
            handler=self._observed(name, handler, plugin_id),
            plugin_id=plugin_id,
        )
        self._commands.append(spec)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._commands.remove(spec)

        return remove

    def _bus(self) -> Any | None:
        """The event bus, when the kernel has one (it does from HooksPlugin on)."""
        try:
            return lookup_service(self.ctx, EventBus)
        except Exception:  # noqa: BLE001 - an absent bus must not break a command
            return None

    def _observed(self, name: str, handler: Any, plugin_id: str) -> Any:
        """Wrap one handler so every run publishes ``command/executed``.

        The TUI dispatches specs directly (it never goes through
        :meth:`invoke`), so the wrapper has to live on the handler itself: both
        paths then report the same event without either one knowing about the
        other.
        """
        if handler is None:
            return None

        def run(host: Any, argument: str = "") -> Any:
            try:
                result = handler(host, argument)
            except Exception as exc:
                self._publish(name, ok=False, error=type(exc).__name__, plugin_id=plugin_id)
                raise
            if inspect.isawaitable(result):
                return self._await_and_publish(name, result, plugin_id)
            self._publish(name, ok=True, plugin_id=plugin_id)
            return result

        return run

    async def _await_and_publish(self, name: str, awaitable: Any, plugin_id: str) -> Any:
        try:
            value = await awaitable
        except Exception as exc:
            self._publish(name, ok=False, error=type(exc).__name__, plugin_id=plugin_id)
            raise
        self._publish(name, ok=True, plugin_id=plugin_id)
        return value

    def _publish(self, name: str, *, ok: bool, error: str = "", plugin_id: str = "") -> None:
        bus = self._bus()
        if bus is None:
            return
        payload = {"name": name, "ok": ok, "error": error, "plugin": plugin_id}
        try:
            pending = bus.emit("command/executed", payload)
        except Exception:
            logger.debug("could not publish command/executed for %s", name, exc_info=True)
            return
        if not inspect.isawaitable(pending):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(pending)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def all(self) -> list[CommandSpec]:
        return list(self._commands)

    def get(self, token: str) -> CommandSpec | None:
        for spec in self._commands:
            if spec.matches(token):
                return spec
        return None

    def resolve(self, line: str) -> tuple[CommandSpec, str] | None:
        """Split /name args into (spec, args); None when the name is unknown."""
        stripped = line.strip()
        if not stripped.startswith("/"):
            return None
        body = stripped[1:].strip()
        if not body:
            return None
        name, _, argument = body.partition(" ")
        spec = self.get(name)
        if spec is None:
            return None
        return spec, argument.strip()

    async def invoke(self, spec: CommandSpec, host: Any, argument: str = "") -> Any:
        if spec.handler is None:
            return None
        result = spec.handler(host, argument)
        if inspect.isawaitable(result):
            return await result
        return result

    def names(self) -> list[str]:
        return [spec.name for spec in self._commands]
