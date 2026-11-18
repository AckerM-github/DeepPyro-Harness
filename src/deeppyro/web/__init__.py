"""Host-only loopback web UI for deeppyro.

Public surface: WebHost (start/stop), WebHostPlugin (kernel service provide
"web") and the WebSurface/Route registry other plugins extend.
"""

from __future__ import annotations

from deeppyro.web.host import WebHost
from deeppyro.web.plugin import WebHostConfig, WebHostPlugin
from deeppyro.web.service import HostServices, WebSessionHost, resolve_plugin_services
from deeppyro.web.surface import Route, RouteRegistry, WebSurface
from deeppyro.web.version import __version__

__all__ = [
    "HostServices",
    "Route",
    "RouteRegistry",
    "WebHost",
    "WebHostConfig",
    "WebHostPlugin",
    "WebSessionHost",
    "WebSurface",
    "__version__",
    "resolve_plugin_services",
]
