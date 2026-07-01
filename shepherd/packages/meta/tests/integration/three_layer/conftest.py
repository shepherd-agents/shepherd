"""Shared fixtures for three-layer architecture tests.

Mock Provider and Context implementations for testing without SDK dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from shepherd_core.provider import Provider
from shepherd_core.types import ExecutionResult, ProviderBinding, ProviderCapabilities, ReversibilityLevel

if TYPE_CHECKING:
    from collections.abc import Sequence

    from shepherd_core.effects import Effect
    from shepherd_core.provider import ProviderRuntime
    from shepherd_runtime.context import Sandbox


@dataclass
class MockProvider(Provider):
    """A mock provider for testing without SDK dependencies."""

    name: str = "mock"
    model: str = "mock-model"
    _response: str = "Mock response"
    _should_fail: bool = False

    @property
    def provider_id(self) -> str:
        return f"provider:mock:{self.name}"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_type="mock",
            supports_streaming=False,
            supports_tools=True,
            supports_structured_output=True,
            supports_session=False,
            supports_fork_session=False,
            supports_images=False,
        )

    async def execute_sdk(
        self,
        prompt: str,
        binding: ProviderBinding | None,
        runtime: ProviderRuntime,
    ) -> ExecutionResult:
        """Mock execution that returns configured response."""
        if self._should_fail:
            return ExecutionResult(
                success=False,
                output_text="Mock failure",
                error_message="Simulated failure",
            )

        return ExecutionResult(
            success=True,
            output_text=self._response,
            metadata={"prompt_length": len(prompt)},
        )


@dataclass
class MockContext:
    """A mock execution context for testing lifecycle phases."""

    name: str
    _prepared: bool = False
    _captured: bool = False
    _cleaned_up: bool = False
    _prepare_should_fail: bool = False
    _cleanup_error: Exception | None = None

    @property
    def context_id(self) -> str:
        return f"mock:{self.name}"

    @property
    def reversibility(self) -> ReversibilityLevel:
        return ReversibilityLevel.AUTO

    def configure(self, capabilities: ProviderCapabilities | None = None) -> ProviderBinding:
        """Return a simple binding with custom description."""
        return ProviderBinding(
            context_id=self.context_id,
            context_type="MockContext",
            context_description=f"Mock context: {self.name}",
            capabilities=frozenset({"read", "write"}),  # Default capabilities
        )

    def prepare(self) -> Self:
        """Prepare the context, potentially failing if configured to."""
        if self._prepare_should_fail:
            raise RuntimeError(f"Simulated preparation failure for {self.name}")
        self._prepared = True
        return self

    def extract_effects(
        self,
        sandbox: Sandbox | None,
        result: ExecutionResult,
    ) -> Sequence[Effect]:
        """Extract effects from execution. No effects for mock context."""
        self._captured = True
        return []

    def apply_effect(self, effect: Effect) -> Self:
        """Apply effect to derive new state. No state change for mock context."""
        return self

    def cleanup(self, error: Exception | None) -> None:
        """Clean up resources."""
        self._cleaned_up = True
        if self._cleanup_error:
            raise self._cleanup_error
