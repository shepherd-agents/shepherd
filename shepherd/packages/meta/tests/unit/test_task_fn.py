"""Tests for task_fn() adapter.

Tests that task_fn() correctly wraps @task classes as combinator-compatible
callables, preserving identity and enabling composition.
"""

import pytest
from pydantic import BaseModel
from shepherd.adapters import TaskAdapter, task_fn
from shepherd_runtime.scope import Scope
from shepherd_runtime.task.authoring import Input, Output, task
from shepherd_tests import MockProvider

# =============================================================================
# Test Tasks
# =============================================================================


@task
class SimpleTask(BaseModel):
    """Simple task for testing."""

    prompt: Input(str)
    result: Output(str)


@task
class MultiInputTask(BaseModel):
    """Task with multiple inputs."""

    first: Input(str)
    second: Input(int)
    combined: Output(str)


# =============================================================================
# Tests for task_fn()
# =============================================================================


class TestTaskFn:
    """Tests for task_fn() function."""

    def test_task_fn_returns_task_adapter(self):
        """task_fn() returns a TaskAdapter instance."""
        adapter = task_fn(SimpleTask)
        assert isinstance(adapter, TaskAdapter)

    def test_task_fn_preserves_name(self):
        """task_fn() preserves __name__ from task class."""
        adapter = task_fn(SimpleTask)
        assert adapter.__name__ == "SimpleTask"

    def test_task_fn_preserves_qualname(self):
        """task_fn() preserves __qualname__ from task class."""
        adapter = task_fn(SimpleTask)
        assert "SimpleTask" in adapter.__qualname__

    def test_task_fn_stores_task_class(self):
        """task_fn() stores reference to original task class."""
        adapter = task_fn(SimpleTask)
        assert adapter.task_class is SimpleTask

    def test_task_fn_repr(self):
        """task_fn() produces readable repr."""
        adapter = task_fn(SimpleTask)
        assert "SimpleTask" in repr(adapter)
        assert "TaskAdapter" in repr(adapter)


# =============================================================================
# Tests for TaskAdapter execution
# =============================================================================


class TestTaskAdapterExecution:
    """Tests for TaskAdapter.__call__() execution."""

    @pytest.mark.asyncio
    async def test_adapter_calls_task_arun(self):
        """TaskAdapter calls task_class.arun() with inputs and scope."""
        adapter = task_fn(SimpleTask)

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = await adapter({"prompt": "hello"}, scope)

            # Result should be an instance of the task class
            assert isinstance(result, SimpleTask)
            assert result.prompt == "hello"

    @pytest.mark.asyncio
    async def test_adapter_with_multiple_inputs(self):
        """TaskAdapter handles multiple inputs correctly."""
        adapter = task_fn(MultiInputTask)

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = await adapter({"first": "hello", "second": 42}, scope)

            assert isinstance(result, MultiInputTask)
            assert result.first == "hello"
            assert result.second == 42


# =============================================================================
# Tests for TaskAdapter.with_kwargs()
# =============================================================================


class TestTaskAdapterWithKwargs:
    """Tests for TaskAdapter.with_kwargs() partial application."""

    def test_with_kwargs_returns_partial_adapter(self):
        """with_kwargs() returns a partial adapter."""
        adapter = task_fn(MultiInputTask)

        with Scope() as scope:
            partial = adapter.with_kwargs(scope, first="pre-bound")

            assert partial._bound_kwargs == {"first": "pre-bound"}

    @pytest.mark.asyncio
    async def test_with_kwargs_merges_inputs(self):
        """with_kwargs() merges pre-bound kwargs with call-time inputs."""
        adapter = task_fn(MultiInputTask)

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            partial = adapter.with_kwargs(scope, first="pre-bound")

            result = await partial({"second": 42}, scope)

            assert result.first == "pre-bound"
            assert result.second == 42

    @pytest.mark.asyncio
    async def test_with_kwargs_call_time_overrides(self):
        """Call-time inputs override pre-bound kwargs."""
        adapter = task_fn(MultiInputTask)

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            partial = adapter.with_kwargs(scope, first="pre-bound")

            result = await partial({"first": "overridden", "second": 42}, scope)

            assert result.first == "overridden"
            assert result.second == 42


# =============================================================================
# Tests for combinator compatibility
# =============================================================================


class TestCombinatorCompatibility:
    """Tests that TaskAdapter works with combinators."""

    @pytest.mark.asyncio
    async def test_adapter_with_retry_combinator(self):
        """TaskAdapter can be passed to retry() combinator."""
        from shepherd_runtime.combinators import retry

        adapter = task_fn(SimpleTask)
        retrying = retry(adapter, max_attempts=3)

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = await retrying({"prompt": "hello"}, scope)

            assert isinstance(result, SimpleTask)

    @pytest.mark.asyncio
    async def test_adapter_with_gate_combinator(self):
        """TaskAdapter can be passed to gate() combinator."""
        from shepherd_runtime.combinators import gate

        adapter = task_fn(SimpleTask)
        # Gate that always passes (takes result and effects)
        gated = gate(adapter, lambda r, e: True)

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = await gated({"prompt": "hello"}, scope)

            assert isinstance(result, SimpleTask)
