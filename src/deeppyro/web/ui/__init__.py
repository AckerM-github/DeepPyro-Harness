"""Local web surface for DeepPyro (P1 single-page shell).

The shell package is UI-only: it owns static markup and browser assets and never
opens sockets or reads agent state. Host integration lives in the web server.
"""

from __future__ import annotations
