"""Public runtime registry APIs."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, cast

from shepherd_core.config import is_strict_mode
from shepherd_core.errors import PluginLoadError
from shepherd_core.foundation.protocols.device import ContextStateBase

from ._task_discovery import PACKAGES_GROUP, TASKS_GROUP, discover_tasks_from_package
from ._task_discovery import discover_all_tasks as _discover_all_tasks

if TYPE_CHECKING:
    from shepherd_core.context.kernel import ExecutionContext
    from shepherd_core.provider import Provider

logger = logging.getLogger(__name__)

PROVIDERS_GROUP = "shepherd.providers"
CONTEXTS_GROUP = "shepherd.contexts"

ProviderFactory = Callable[[dict[str, Any]], "Provider"]
ContextDeserializer = Callable[[dict[str, Any]], ContextStateBase]

_PROVIDER_FACTORIES: dict[str, ProviderFactory] = {}
_CONTEXT_DESERIALIZERS: dict[str, ContextDeserializer] = {}


class ProviderCreationError(Exception):
    """Error during provider creation from config."""

    def __init__(self, provider_type: str, message: str):
        self.provider_type = provider_type
        super().__init__(f"Failed to create provider '{provider_type}': {message}")


class ContextDeserializationError(Exception):
    """Error during context deserialization."""

    def __init__(self, context_type: str, message: str):
        self.context_type = context_type
        super().__init__(f"Failed to deserialize context '{context_type}': {message}")


def _get_entry_points(group: str) -> dict[str, Any]:
    eps = entry_points(group=group)
    return {ep.name: ep for ep in eps}


def discover_all_tasks() -> dict[str, type]:
    """Discover tasks by walking all packages registered under ``shepherd.packages``."""
    return _discover_all_tasks(get_entry_points=_get_entry_points)


def discover_providers() -> dict[str, type[Provider]]:
    """Discover all registered providers."""
    result: dict[str, type[Provider]] = {}
    for name, ep in _get_entry_points(PROVIDERS_GROUP).items():
        try:
            result[name] = ep.load()
        except Exception as e:
            if is_strict_mode():
                raise PluginLoadError(name, PROVIDERS_GROUP, e) from e
            logger.warning("Failed to load provider '%s': %s", name, e, exc_info=logger.isEnabledFor(logging.DEBUG))
    return result


def discover_contexts() -> dict[str, type[ExecutionContext]]:
    """Discover all registered contexts."""
    result: dict[str, type[ExecutionContext]] = {}
    for name, ep in _get_entry_points(CONTEXTS_GROUP).items():
        try:
            result[name] = ep.load()
        except Exception as e:
            if is_strict_mode():
                raise PluginLoadError(name, CONTEXTS_GROUP, e) from e
            logger.warning("Failed to load context '%s': %s", name, e, exc_info=logger.isEnabledFor(logging.DEBUG))
    return result


def get_provider(name: str) -> type[Provider]:
    """Get a specific provider by entry-point name."""
    eps = _get_entry_points(PROVIDERS_GROUP)
    if name not in eps:
        available = list(eps.keys())
        raise KeyError(f"Provider '{name}' not found. Available: {available}")
    return cast("type[Provider]", eps[name].load())


def get_context(name: str) -> type[ExecutionContext]:
    """Get a specific context by entry-point name."""
    eps = _get_entry_points(CONTEXTS_GROUP)
    if name not in eps:
        available = list(eps.keys())
        raise KeyError(f"Context '{name}' not found. Available: {available}")
    return cast("type[ExecutionContext]", eps[name].load())


def register_provider_factory(provider_type: str, factory: ProviderFactory) -> None:
    """Register a factory for a provider type."""
    _PROVIDER_FACTORIES[provider_type] = factory


def get_provider_factory(provider_type: str) -> ProviderFactory | None:
    """Get the factory for a provider type."""
    return _PROVIDER_FACTORIES.get(provider_type)


def create_provider(config: dict[str, Any]) -> Provider:
    """Create a provider from configuration."""
    provider_type = config.get("provider_type")
    if not provider_type:
        raise ProviderCreationError("unknown", "config missing 'provider_type' field")

    factory = get_provider_factory(provider_type)
    if factory is None:
        available = list(_PROVIDER_FACTORIES.keys())
        raise ProviderCreationError(
            provider_type, f"no factory registered for provider type '{provider_type}'. Available: {available}"
        )

    try:
        return factory(config)
    except Exception as e:
        raise ProviderCreationError(provider_type, f"factory raised: {e}") from e


def list_registered_provider_types() -> list[str]:
    """Return the registered provider type names."""
    return list(_PROVIDER_FACTORIES.keys())


def register_context_deserializer(context_type: str, deserializer: ContextDeserializer) -> None:
    """Register a deserializer for a context type."""
    _CONTEXT_DESERIALIZERS[context_type] = deserializer


def get_context_deserializer(context_type: str) -> ContextDeserializer | None:
    """Get the deserializer for a context type."""
    return _CONTEXT_DESERIALIZERS.get(context_type)


def deserialize_context(
    state_data: dict[str, Any],
    rebind_env: Mapping[str, str] | None = None,
) -> ContextStateBase:
    """Deserialize and rebind a context state."""
    context_type = state_data.get("context_type")
    if not context_type:
        raise ContextDeserializationError("unknown", "state_data missing 'context_type' field")

    deserializer = get_context_deserializer(context_type)
    if deserializer is None:
        raise ContextDeserializationError(context_type, f"no deserializer registered for context type '{context_type}'")

    try:
        state = deserializer(state_data)
    except Exception as e:
        raise ContextDeserializationError(context_type, f"deserializer raised: {e}") from e

    if rebind_env:
        state = state.rebind(rebind_env)

    return state


def deserialize_all_contexts(
    states_data: Mapping[str, dict[str, Any]],
    rebind_env: Mapping[str, str] | None = None,
) -> dict[str, ContextStateBase]:
    """Deserialize and rebind multiple context states."""
    return {
        binding_name: deserialize_context(state_data, rebind_env) for binding_name, state_data in states_data.items()
    }


def list_registered_context_types() -> list[str]:
    """Return the registered context type names."""
    return list(_CONTEXT_DESERIALIZERS.keys())


__all__ = [
    "CONTEXTS_GROUP",
    "PACKAGES_GROUP",
    "PROVIDERS_GROUP",
    "TASKS_GROUP",
    "ContextDeserializationError",
    "ContextDeserializer",
    "ProviderCreationError",
    "ProviderFactory",
    "create_provider",
    "deserialize_all_contexts",
    "deserialize_context",
    "discover_all_tasks",
    "discover_contexts",
    "discover_providers",
    "discover_tasks_from_package",
    "get_context",
    "get_context_deserializer",
    "get_provider",
    "get_provider_factory",
    "list_registered_context_types",
    "list_registered_provider_types",
    "register_context_deserializer",
    "register_provider_factory",
]
