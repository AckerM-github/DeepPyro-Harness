"""NiceGUI surface for the loopback web host.

The package is presentation-only: pages call WebUiBridge, and the bridge narrows
the native WebSessionHost to dictionaries. No module here opens a socket, boots a
kernel, or reads a session log directly. The ASGI composition lives in
deeppyro.web.ui.server.
"""

from __future__ import annotations
