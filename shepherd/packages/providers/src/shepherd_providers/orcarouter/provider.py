"""OrcaRouter Responses API provider implementation.

This module provides OrcaRouterProvider, a first-class provider for
OrcaRouter's OpenAI-compatible gateway. It reuses the OpenAI Responses API
agent loop from ``shepherd_providers.openai`` and only overrides the
provider identity, the default endpoint, and the credential source.

OrcaRouter (https://www.orcarouter.ai) exposes the OpenAI Responses API
at ``https://api.orcarouter.ai/v1``, so the entire bounded agent loop —
tool validation, dispatch, session chaining, streaming, and structured
output extraction — works unchanged.

Usage:
    provider = OrcaRouterProvider(
        name="fetcher",
        model="openai/gpt-5.5",
    )
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from shepherd_core.types import ProviderCapabilities
from shepherd_runtime.registry import register_provider_factory

from shepherd_providers.openai.provider import OpenAIProvider

if TYPE_CHECKING:
    from shepherd_providers.verbose import VerboseConfig

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "openai/gpt-5.5"
_DEFAULT_BASE_URL = "https://api.orcarouter.ai/v1"


@dataclass
class OrcaRouterProvider(OpenAIProvider):
    """OrcaRouter Responses API provider implementation.

    A thin, named integration over OrcaRouter's OpenAI-compatible gateway.
    Inherits the full OpenAI Responses API agent loop; only the defaults
    and the provider identity differ.

    Attributes:
        name: Human-readable name for this provider instance
        model: OrcaRouter model to use (default: openai/gpt-5.5)
        max_turns: Maximum tool-call turns before forced exit (default: 30)
        api_key: Optional API key override (default: ORCAROUTER_API_KEY env var)
        base_url: Optional base URL override (default: https://api.orcarouter.ai/v1)
        verbose: Verbose output configuration
    """

    name: str
    model: str = _DEFAULT_MODEL
    max_turns: int = 30
    api_key: str | None = None
    base_url: str | None = _DEFAULT_BASE_URL
    verbose: VerboseConfig | None = None

    def __post_init__(self) -> None:
        """Initialize verbose formatter and default the API key from the env."""
        super().__post_init__()
        if self.api_key is None:
            self.api_key = os.environ.get("ORCAROUTER_API_KEY")

    # -----------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------

    @property
    def provider_id(self) -> str:
        return f"provider:orcarouter:{self.model}:{self.name}:{self._id}"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_type="orcarouter",
            supports_streaming=True,
            supports_tools=True,
            supports_structured_output=True,
            supports_session=True,
            supports_fork_session=True,
            supports_images=True,
        )

    # -----------------------------------------------------------------
    # Serialization
    # -----------------------------------------------------------------

    def to_config(self) -> dict[str, Any]:
        """Serialize provider to config dict for container transfer."""
        config: dict[str, Any] = {
            "provider_type": "orcarouter",
            "name": self.name,
            "model": self.model,
        }
        if self.max_turns != 30:
            config["max_turns"] = self.max_turns
        # Note: api_key is intentionally omitted — the container should
        # pick it up from the ORCAROUTER_API_KEY env var.
        if self.base_url is not None:
            config["base_url"] = self.base_url
        return config

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> OrcaRouterProvider:
        """Reconstruct provider from config dict."""
        return cls(
            name=config.get("name", "container"),
            model=config.get("model", _DEFAULT_MODEL),
            max_turns=config.get("max_turns", 30),
            api_key=config.get("api_key"),
            base_url=config.get("base_url"),
            verbose=None,
        )


# ---------------------------------------------------------------------------
# Factory registration
# ---------------------------------------------------------------------------


def _register_provider_factory() -> None:
    """Register OrcaRouterProvider factory with the provider registry."""
    try:
        register_provider_factory("orcarouter", OrcaRouterProvider.from_config)
    except ImportError:
        logger.debug("Skipping container provider factory registration (device module unavailable)")


_register_provider_factory()


__all__ = ["OrcaRouterProvider"]
