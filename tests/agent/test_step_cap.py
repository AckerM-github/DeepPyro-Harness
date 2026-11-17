"""The step cap is opt-in: DSH's loop runs until the turn stops by itself.

deeppyro used to stop after 100 steps by default. DSH has no cap at all, and a
default cap silently truncates long investigations, so the cap became an
explicit opt-in while ``[agent].maxSteps`` keeps working for scripts that want
a guard rail.
"""

from __future__ import annotations

from deeppyro.agent.options import AgentOptions
from deeppyro.config import Config


def test_the_agent_options_default_to_no_cap() -> None:
    assert AgentOptions().max_steps is None


def test_the_agent_config_defaults_to_no_cap() -> None:
    assert Config().agent.max_steps is None


def test_a_configured_cap_survives() -> None:
    options = AgentOptions(max_steps=7)

    assert options.max_steps == 7
