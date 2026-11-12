"""Agent loop options."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class AgentOptions:
    """Tuning for deeppyro.agent.session.AgentSession.

    The first five fields are the frozen interface (INTERFACES section 7).
    The remaining fields are additive conveniences for wiring a session from
    the CLI without threading a dozen separate arguments.
    """

    max_steps: int = 100
    max_tokens: int | None = None
    temperature: float | None = None
    compact_threshold: float = 0.85
    context_window: int = 32768
    # -- additive, optional ------------------------------------------------
    model: str = ""
    permission_preset: str | None = None
    approval_policy: str | None = None
    instructions: list[str] = field(default_factory=list)
