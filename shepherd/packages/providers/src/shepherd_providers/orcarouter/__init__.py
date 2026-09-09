"""OrcaRouter provider subpackage.

This module provides the OrcaRouterProvider implementation for OrcaRouter's
OpenAI-compatible gateway, which exposes the OpenAI Responses API.

Usage:
    from shepherd_providers.orcarouter import OrcaRouterProvider

    provider = OrcaRouterProvider(
        name="fetcher",
        model="openai/gpt-5.5",
    )
"""

from shepherd_providers.orcarouter.provider import OrcaRouterProvider

__all__ = ["OrcaRouterProvider"]
