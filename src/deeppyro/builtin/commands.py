"""The commands service: slash commands contributed by plugins."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from typing import Any

from deeppyro.api import CommandSpec, PluginDependencyError, Service


class CommandsPlugin(Service):
    """Registry of slash commands; the frontend resolves them through this."""

    provide = "commands"
    plugin_id = "commands"
    version = "0.1.0"
    description = "slash command registry"

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self._commands: list[CommandSpec] = []

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
            handler=handler,
            plugin_id=plugin_id,
        )
        self._commands.append(spec)

        def remove() -> None:
            try:
                self._commands.remove(spec)
            except ValueError:
                pass

        return remove

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
