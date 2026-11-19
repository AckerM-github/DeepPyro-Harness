"""Loopback web surface for the native agent runtime.

The JSON API in api.py is a thin projection over WebSessionHost; the browser is a
prebuilt React SPA served from static/. No module here opens a socket, boots a
kernel, or reads a session log directly. The ASGI composition lives in
deeppyro.web.ui.server.
"""

from __future__ import annotations
