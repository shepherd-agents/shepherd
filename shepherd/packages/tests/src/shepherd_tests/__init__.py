"""Shared test utilities for Shepherd packages.

This package provides common testing infrastructure:
- Base test classes for providers and contexts
- Common pytest fixtures
- VCR utilities for recording/replaying API calls
- Mock utilities
- Shared test context implementations
"""

from shepherd_tests.base import (
    BaseContextTests,
    BaseProviderTests,
)
from shepherd_tests.contexts import (
    CounterContext,
    NoOpContext,
    SimpleContext,
)
from shepherd_tests.mock_provider import (
    MockProvider,
)
from shepherd_tests.mocks import (
    FileModifyingMockProvider,
    MockContainerDevice,
    MockOverlay,
    MockSandbox,
    create_mock_binding,
    create_mock_context,
    create_mock_result,
)
from shepherd_tests.scope import mock_steps
from shepherd_tests.tasks import (
    INLINE_STEP_TEST_CASES,
    RETURN_TYPE_TEST_CASES,
    InlineStepCase,
    StepReturnTypeCase,
    make_inline_step_task,
    make_step_task,
)

__version__ = "0.2.0"

__all__ = [
    "INLINE_STEP_TEST_CASES",
    # Test case data
    "RETURN_TYPE_TEST_CASES",
    "BaseContextTests",
    # Base test classes
    "BaseProviderTests",
    # Shared test contexts
    "CounterContext",
    # Mock utilities
    "FileModifyingMockProvider",
    "InlineStepCase",
    "MockContainerDevice",
    "MockOverlay",
    "MockProvider",
    "MockSandbox",
    "NoOpContext",
    "SimpleContext",
    # Test case types
    "StepReturnTypeCase",
    "create_mock_binding",
    "create_mock_context",
    "create_mock_result",
    "make_inline_step_task",
    # Task factories
    "make_step_task",
    "mock_steps",
]
