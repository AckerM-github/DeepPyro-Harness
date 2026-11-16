"""Lookup helpers for test doubles that stand in for PluginContext.

Several plugin tests build minimal contexts exposing a name-keyed service dict.
After the typed-service migration plugin setup asks ctx.require(ContractClass),
so those doubles share this small adapter instead of re-implementing the
contract-to-name resolution.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from deeppyro.api import PluginDependencyError, ServiceNotFoundError
from deeppyro.api.services import ServiceTypeError, contract_path, service_name


def service_lookup(services: Mapping[str, Any], service: str | type[Any]) -> Any | None:
    """Resolve a string name or a contract class against a name-keyed mapping."""
    if isinstance(service, str):
        return services.get(service)
    try:
        name = service_name(service)
    except ServiceTypeError:
        return None
    return services.get(name)


def service_require(services: Mapping[str, Any], service: str | type[Any], *, owner: str = "plugin") -> Any:
    """Like service_lookup, but raise the same errors PluginContext raises."""
    value = service_lookup(services, service)
    if value is not None:
        return value
    if isinstance(service, str):
        raise PluginDependencyError(f"plugin {owner!r} requires service {service!r}, which is not available")
    name = service_name(service)
    raise ServiceNotFoundError(
        f"plugin {owner!r} requires service {name!r} ({contract_path(service)}), which is not available",
        service=name,
        contract=service,
    )
