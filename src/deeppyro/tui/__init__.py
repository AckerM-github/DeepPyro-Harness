"""DeepPyro terminal UI (Textual).

The TUI is the only product front end. Everything lives in this package:

* app.py      - DeepPyroApp, the composition root
* runtime.py  - kernel boot + session/agent wiring
* sink.py     - agent event sink feeding the widgets
* widgets/    - conversation, status bar, prompt
* screens/    - approval modal (and future pickers)
* commands.py - slash-command dispatch

Imports of Textual are optional at package import time so tooling can import
deeppyro.tui.format without a terminal.
"""

from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    if name in ("DeepPyroApp", "run_app"):
        from deeppyro.tui import app as app_module

        return getattr(app_module, name)
    if name == "main":
        from deeppyro.tui.main import main as main_func

        return main_func
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["DeepPyroApp", "main", "run_app"]
