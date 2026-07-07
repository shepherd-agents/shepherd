"""Cross-package integration test configuration.

These tests verify that all Shepherd packages work together correctly.
"""

import os

import pytest
from shepherd_runtime.scope import Scope
from shepherd_tests import MockProvider


@pytest.fixture(autouse=True)
def _isolate_process_environment():
    """Snapshot and restore ``os.environ`` around every test (W0.2).

    This suite mixes in-process ``CliRunner`` invocations (test_openai_api.py)
    with env-sensitive tests the same way the top-level ``integration-tests/``
    suite does. Belt-and-braces: it protects against *any* test that writes
    process env directly.
    """
    saved = dict(os.environ)
    try:
        yield
    finally:
        for key in set(os.environ) - set(saved):
            del os.environ[key]
        for key, value in saved.items():
            if os.environ.get(key) != value:
                os.environ[key] = value


@pytest.fixture
def mock_provider() -> MockProvider:
    """Pre-configured MockProvider for integration tests."""
    return MockProvider(
        name="integration-mock",
        default_output={"result": "integration_test_result"},
    )


@pytest.fixture
def integration_scope(mock_provider: MockProvider):
    """Isolated scope for integration testing."""
    with Scope(root=True) as scope:
        scope.register_provider("default", mock_provider, default=True)
        yield scope
