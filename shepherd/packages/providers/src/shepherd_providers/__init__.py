"""Shepherd Providers - Provider implementations for shepherd-core.

This package provides concrete provider implementations for various LLM SDKs:
- ClaudeProvider: Claude Agent SDK adapter
- OpenAIProvider: OpenAI Agents SDK adapter

Providers translate abstract ProviderBinding from shepherd-core into
SDK-specific configurations and handle execution.

Usage:
    # Import specific providers
    from shepherd_providers.claude import ClaudeProvider
    from shepherd_providers.openai import OpenAIProvider

    # Or import from top-level (lazy-loaded)
    from shepherd_providers import ClaudeProvider, OpenAIProvider

    # Create and use providers
    provider = ClaudeProvider(
        name="analyst",
        model="claude-sonnet-4-20250514",
    )

Optional Dependencies:
    - claude: Install with `pip install shepherd-providers[claude]`
    - openai: Install with `pip install shepherd-providers[openai]`
    - all: Install with `pip install shepherd-providers[all]`

The providers use lazy SDK imports, so you can import providers without
having the SDK installed. The SDK is only required when executing.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.3.0"

# Lazy-loaded provider classes
_PROVIDER_MAP = {
    "ClaudeProvider": "shepherd_providers.claude",
    "OpenAIProvider": "shepherd_providers.openai",
    "OpenCodeProvider": "shepherd_providers.opencode",
}

# Eager exports (always available)
from shepherd_providers.verbose import VerboseConfig, VerboseFormatter


def __getattr__(name: str) -> Any:
    """Lazy import for provider classes.

    This allows importing ClaudeProvider and OpenAIProvider from the
    top-level package without loading their dependencies until actually used.
    """
    if name in _PROVIDER_MAP:
        import importlib

        module = importlib.import_module(_PROVIDER_MAP[name])
        return getattr(module, name)
    raise AttributeError(f"module 'shepherd_providers' has no attribute '{name}'")


def __dir__() -> list[str]:
    """List available attributes including lazy-loaded ones."""
    return [*list(_PROVIDER_MAP.keys()), "__version__", "VerboseConfig", "VerboseFormatter"]


__all__ = [
    # Providers (lazy-loaded)
    "ClaudeProvider",
    "OpenAIProvider",
    "OpenCodeProvider",
    # Verbose output
    "VerboseConfig",
    "VerboseFormatter",
    "__version__",
]
