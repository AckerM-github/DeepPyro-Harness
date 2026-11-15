"""PyInstaller entry script for a frozen build.

PyInstaller needs a script rather than a console-script entry point.  This keeps
the ``dpyro`` contract identical to the wheel: no arguments boots the TUI,
prompts and flags behave the same, and the hidden ``--internal-*`` re-entry
flags are dispatched by ``deeppyro.tui.main`` before the runtime is imported.
"""

from __future__ import annotations

import sys

if __name__ == "__main__":
    from deeppyro.tui.main import main

    sys.exit(main())
