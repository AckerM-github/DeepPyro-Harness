"""Layer 9 of the DeepPyro TUI app: Model, provider and preset selection layer."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from deeppyro.tui.app_sessions import SessionLayer


class ModelLayer(SessionLayer):
    """Model, provider and preset selection layer."""

    def _provider_names(self) -> list[str]:
        """Configured provider names, falling back to the active one."""
        getter = getattr(self.runtime, "providers", None)
        if callable(getter):
            read: Callable[[], Any] = getter
            # the command must keep working
            with contextlib.suppress(Exception):
                values = [str(name) for name in read()]
                if values:
                    return values
        active = str(getattr(self.runtime, "provider_name", "") or "")
        return [active] if active else []

    def _model_choices(self) -> list[tuple[str, str]]:
        """Every configured (provider, model) pair, if the runtime knows them."""
        getter = getattr(self.runtime, "model_choices", None)
        if callable(getter):
            read_choices: Callable[[], Any] = getter
            with contextlib.suppress(Exception):
                return [(str(provider), str(model)) for provider, model in read_choices()]
        models_getter = getattr(self.runtime, "models", None)
        if callable(models_getter):
            read_models: Callable[[], Any] = models_getter
            with contextlib.suppress(Exception):
                provider = str(getattr(self.runtime, "provider_name", "") or "")
                return [(provider, str(model)) for model in read_models()]
        return []

    def _model_value(self, provider: str, model: str) -> str:
        """Picker value: bare model with one provider, provider/model otherwise."""
        return f"{provider}/{model}" if len(self._provider_names()) > 1 else model

    def _report_persist_error(self) -> None:
        message = getattr(self.runtime, "last_persist_error", None)
        if message:
            self.conversation.add_info(f"could not save the default provider/model: {message}")

    async def _refresh_model_catalog(self) -> None:
        """Refresh the llm catalog before showing model choices (best effort).

        Discovered models are a convenience: when the refresh fails the picker
        simply keeps showing the configured models.
        """
        refresher = getattr(self.runtime, "refresh_models", None)
        if not callable(refresher):
            return
        read: Callable[[], Any] = refresher
        try:
            await read()
        except Exception:  # noqa: BLE001 - the configured catalog stays usable
            return

    async def provider_command(self, argument: str) -> None:
        """Show or switch the active LLM provider (/provider)."""
        if self._working:
            self.notify("cannot switch provider during a running turn", severity="warning")
            return
        runtime = self.runtime
        providers = self._provider_names()
        current = str(getattr(runtime, "provider_name", "") or "")
        if not argument:
            if len(providers) > 1:
                items: list[tuple[str, str, str]] = []
                has_key = getattr(runtime, "has_api_key", None)
                for name in providers:
                    note = "current" if name == current else ""
                    if note != "current" and callable(has_key):
                        try:
                            note = "" if has_key(name) else "missing API key"
                        except Exception:  # noqa: BLE001 - notes are cosmetic
                            note = ""
                    items.append((name, name, note))
                choice = await self._pick("Select provider", items)
                if choice is None:
                    self.conversation.add_info(f"current provider: {current}")
                    self.conversation.add_info("available: " + ", ".join(providers))
                    self.conversation.add_info("use /provider <name>")
                    return
                argument = choice
            else:
                self.conversation.add_info(f"current provider: {current}")
                self.conversation.add_info("use /provider <name>")
                return
        argument = argument.strip()
        if argument not in providers:
            self.conversation.add_error(f"unknown provider: {argument} (choose from {', '.join(providers)})")
            return
        try:
            provider_name, model_id = await runtime.set_provider(argument)
        except Exception as exc:  # noqa: BLE001 - show the runtime message
            self.conversation.add_error(f"provider switch failed: {exc}")
            return
        self.conversation.add_info(f"provider switched to {provider_name} · {model_id}")
        self._report_persist_error()
        self.refresh_status()

    async def model_command(self, argument: str) -> None:
        """Show or switch the model, including across providers (/model)."""
        if self._working:
            self.notify("cannot switch model during a running turn", severity="warning")
            return
        runtime = self.runtime
        await self._refresh_model_catalog()
        providers = self._provider_names()
        choices = self._model_choices()
        current = (
            str(getattr(runtime, "provider_name", "") or ""),
            str(getattr(runtime, "model", "") or ""),
        )
        if not argument:
            if len(choices) > 1:
                items: list[tuple[str, str, str]] = []
                for provider_name, model_id in choices:
                    value = self._model_value(provider_name, model_id)
                    label = model_id if len(providers) == 1 else value
                    note = "current" if (provider_name, model_id) == current else ""
                    items.append((value, label, note))
                choice = await self._pick("Select model", items)
                if choice is None:
                    self.conversation.add_info(f"current model: {current[0]}/{current[1]}")
                    self.conversation.add_info("available: " + ", ".join(value for value, _, _ in items))
                    self.conversation.add_info("use /model <id> or /model <provider>/<id>")
                    return
                argument = choice
            else:
                self.conversation.add_info(f"current model: {current[0]}/{current[1]}")
                self.conversation.add_info("use /model <id>")
                return
        argument = argument.strip()
        target: tuple[str, str] | None = None
        if "/" in argument:
            provider_name, _, model_id = argument.partition("/")
            provider_name = provider_name.strip()
            model_id = model_id.strip()
            if provider_name not in providers:
                self.conversation.add_error(
                    f"unknown provider: {provider_name} (choose from {', '.join(providers)})"
                )
                return
            offered = [model for provider, model in choices if provider == provider_name]
            if model_id not in offered:
                self.conversation.add_error(
                    f"unknown model for provider {provider_name}: {model_id}"
                    f" (available: {', '.join(offered) or 'none'})"
                )
                return
            target = (provider_name, model_id)
        else:
            if argument in [model for _, model in choices]:
                target = (current[0], argument)
            else:
                matches = [pair for pair in choices if pair[1] == argument]
                if len(matches) == 1:
                    target = matches[0]
                elif len(matches) > 1:
                    labels = ", ".join(f"{provider}/{model}" for provider, model in matches)
                    self.conversation.add_error(
                        f"model {argument} is available on several providers: {labels}"
                    )
                    return
                else:
                    available = (
                        ", ".join(self._model_value(provider, model) for provider, model in choices) or "none"
                    )
                    self.conversation.add_error(f"unknown model: {argument} (available: {available})")
                    return
        assert target is not None
        target_provider, target_model = target
        try:
            if target_provider == runtime.provider_name:
                await runtime.set_model(target_model)
            else:
                await runtime.set_provider(target_provider, model=target_model)
        except Exception as exc:  # noqa: BLE001 - show the runtime message
            self.conversation.add_error(f"model switch failed: {exc}")
            return
        self.conversation.add_info(f"model switched to {target_provider}/{target_model}")
        self._report_persist_error()
        self.refresh_status()

    def _preset_specs(self) -> list[Any]:
        value = getattr(self.runtime, "presets", None)
        if callable(value):
            read: Callable[[], Any] = value
            try:
                value = read()
            except Exception:  # noqa: BLE001 - the command must keep working
                return []
        return list(value or [])

    def _agent_preset_summary(self, spec: Any) -> tuple[str, str, str, str, str]:
        """Preset fields used by /preset: (id, name, presentation, mode, description)."""
        preset_id = str(getattr(spec, "id", "") or "")
        display_name = getattr(spec, "display_name", None)
        name = ""
        if callable(display_name):
            try:
                name = str(display_name() or "").strip()
            except Exception:  # noqa: BLE001 - a broken spec must not hide the list
                name = ""
        name = name or str(getattr(spec, "name", "") or "").strip() or preset_id
        presentation = str(getattr(spec, "presentation", "") or "native")
        mode = str(getattr(spec, "mode", "") or "default")
        description = str(getattr(spec, "description", "") or "")
        return preset_id, name, presentation, mode, description

    def _agent_preset_items(self) -> list[tuple[str, str, str]]:
        """Picker rows: value, display label with presentation, description."""
        items: list[tuple[str, str, str]] = []
        current = self._agent_preset()
        for spec in self._preset_specs():
            preset_id, name, presentation, mode, description = self._agent_preset_summary(spec)
            label = f"{name}  ({presentation} · {mode} mode)"
            marker = "(current) " if preset_id == current else ""
            items.append((preset_id, label, marker + description))
        return items

    def _agent_preset_lines(self) -> list[str]:
        """Text rows for /preset, e.g. standard  Standard  native tools · ..."""
        lines: list[str] = []
        current = self._agent_preset()
        for spec in self._preset_specs():
            preset_id, name, presentation, _mode, description = self._agent_preset_summary(spec)
            line = f"{preset_id}  {name}  {presentation} tools"
            if description:
                line += f" · {description}"
            if preset_id == current:
                line += "  (current)"
            lines.append(line)
        return lines

    def _report_agent_presets(self) -> None:
        lines = self._agent_preset_lines()
        if lines:
            self.conversation.add_info("agent presets:")
            for line in lines:
                self.conversation.add_info(line)
        else:
            self.conversation.add_info(f"current agent preset: {self._agent_preset()}")
        self.conversation.add_info("use /preset <id>")

    async def preset_command(self, argument: str) -> None:
        presets = self._preset_specs()
        if self._working:
            self.notify("cannot switch agent preset during a turn", severity="warning")
            return
        if not argument:
            if len(presets) > 1:
                choice = await self._pick("Select agent preset", self._agent_preset_items())
                if choice is None:
                    self._report_agent_presets()
                    return
                argument = choice
            else:
                self._report_agent_presets()
                return
        try:
            selected = await self.runtime.set_agent_preset(argument)
        except Exception as exc:  # noqa: BLE001 - show the service message
            self.conversation.add_error(f"preset switch failed: {exc}")
            return
        mode = self._agent_mode()
        presentation = self._agent_presentation()
        display = self._agent_preset_display()
        if presentation == "ptc":
            hint = "run_code-only tool presentation"
        elif mode == "team":
            hint = "team tools enabled for the lead"
        else:
            hint = "subagent delegation enabled"
        self.conversation.add_info(
            f"agent preset switched to {display} ({selected}) · "
            f"{presentation} presentation · {mode} mode ({hint})"
        )
        self.refresh_status()
