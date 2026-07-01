"""Tests for @task class auto-adaptation in combinators.

Combinators now accept @task classes directly without requiring explicit
task_fn() wrapping:

    # These are equivalent:
    retry(WriteCode, max_attempts=3)
    retry(task_fn(WriteCode), max_attempts=3)

This test file verifies that:
1. is_task_class() correctly identifies @task classes
2. ensure_task_fn() adapts @task classes to callables
3. All combinators accept @task classes directly
"""

import pytest
from pydantic import BaseModel
from shepherd_runtime.combinators import (
    Budget,
    Rejected,
    branch,
    budget,
    ensure_task_fn,
    fallback,
    # Effects
    filter_effects,
    # Gating
    gate,
    is_task_class,
    loop,
    map_effects,
    # Parallel
    parallel,
    parallel_all,
    race,
    recover,
    # Retry
    retry,
    scope_tap,
    # Composition
    sequence,
    speculate,
    tap,
    timeout,
)
from shepherd_runtime.scope import Scope
from shepherd_runtime.task.authoring import Input, Output, task
from shepherd_tests import MockProvider

# =============================================================================
# Test Fixtures: @task classes for testing
# =============================================================================
# Note: We use MockProvider for tests since composite tasks with execute()
# methods still need special handling in arun. The key thing being tested
# is that combinators auto-detect @task classes.


@task
class SimpleTask(BaseModel):
    """A simple task that doubles a number."""

    x: Input(int)
    result: Output(int) = 0


@task
class AddOneTask(BaseModel):
    """A task that adds one to input."""

    x: Input(int)
    result: Output(int) = 0


@task
class FlakyTask(BaseModel):
    """A task that can simulate failures."""

    x: Input(int)
    fail_count: Input(int) = 0
    result: Output(int) = 0


# =============================================================================
# Tests for is_task_class()
# =============================================================================


class TestIsTaskClass:
    """Tests for is_task_class() detection."""

    def test_detects_task_class(self):
        """is_task_class() returns True for @task decorated classes."""
        assert is_task_class(SimpleTask) is True
        assert is_task_class(AddOneTask) is True

    def test_rejects_plain_class(self):
        """is_task_class() returns False for plain classes."""

        class PlainClass:
            pass

        assert is_task_class(PlainClass) is False

    def test_rejects_function(self):
        """is_task_class() returns False for functions."""

        async def some_func(inputs, scope):
            return inputs

        assert is_task_class(some_func) is False

    def test_rejects_instance(self):
        """is_task_class() returns False for instances (not classes)."""
        # Note: We need a scope to instantiate
        # Just test with a plain object
        assert is_task_class(42) is False
        assert is_task_class("string") is False
        assert is_task_class([1, 2, 3]) is False


# =============================================================================
# Tests for ensure_task_fn()
# =============================================================================


class TestEnsureTaskFn:
    """Tests for ensure_task_fn() adaptation."""

    def test_adapts_task_class(self):
        """ensure_task_fn() wraps @task classes in a callable."""
        adapted = ensure_task_fn(SimpleTask)

        # Should be callable
        assert callable(adapted)
        # Should preserve name
        assert adapted.__name__ == "SimpleTask"

    def test_passes_through_callable(self):
        """ensure_task_fn() returns callables unchanged."""

        async def my_task(inputs, scope):
            return inputs["x"] * 2

        adapted = ensure_task_fn(my_task)
        assert adapted is my_task

    @pytest.mark.asyncio
    async def test_adapted_task_executes_with_mock(self):
        """Adapted task can be called with (inputs, scope) signature with MockProvider."""
        adapted = ensure_task_fn(SimpleTask)

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = await adapted({"x": 5}, scope)
            # With MockProvider, outputs get default/mock values
            assert hasattr(result, "result")


# =============================================================================
# Tests for combinators accepting @task classes
# =============================================================================
# These tests verify that combinators correctly detect and adapt @task classes.
# We use MockProvider since tasks without providers need it.


class TestGatingWithTaskClass:
    """Test that gating combinators accept @task classes."""

    def test_gate_accepts_task_class(self):
        """gate() correctly wraps @task class."""
        # The key test is that gate() doesn't raise when given a @task class
        gated = gate(SimpleTask, lambda r, e: True)
        assert callable(gated)
        assert "gate" in gated.__name__

    def test_budget_accepts_task_class(self):
        """budget() correctly wraps @task class."""
        budgeted = budget(SimpleTask, Budget(max_effects=10))
        assert callable(budgeted)
        assert "budget" in budgeted.__name__

    def test_timeout_accepts_task_class(self):
        """timeout() correctly wraps @task class."""
        timed = timeout(SimpleTask, seconds=5.0)
        assert callable(timed)
        assert "timeout" in timed.__name__


class TestRetryWithTaskClass:
    """Test that retry combinators accept @task classes."""

    def test_retry_accepts_task_class(self):
        """retry() correctly wraps @task class."""
        retrying = retry(SimpleTask, max_attempts=3)
        assert callable(retrying)
        assert "retry" in retrying.__name__

    def test_fallback_accepts_task_classes(self):
        """fallback() correctly wraps @task classes."""
        falling = fallback(SimpleTask, AddOneTask)
        assert callable(falling)
        assert "fallback" in falling.__name__

    def test_recover_accepts_task_class(self):
        """recover() correctly wraps @task class."""
        recovering = recover(SimpleTask, on_error=lambda e: None)
        assert callable(recovering)
        assert "recover" in recovering.__name__


class TestCompositionWithTaskClass:
    """Test that composition combinators accept @task classes."""

    @pytest.mark.asyncio
    async def test_sequence_accepts_task_functions(self):
        """sequence() works with async functions."""

        async def double(inputs, scope):
            return {"x": inputs["x"] * 2}

        async def add_one(inputs, scope):
            return {"x": inputs["x"] + 1}

        sequenced = sequence(double, add_one)

        with Scope() as scope:
            result = await sequenced({"x": 5}, scope)
            assert result["x"] == 11  # (5 * 2) + 1

    def test_branch_accepts_task_classes(self):
        """branch() correctly wraps @task classes."""
        branching = branch(
            lambda inputs: inputs["x"] > 10,
            if_true=SimpleTask,
            if_false=AddOneTask,
        )
        assert callable(branching)
        assert "branch" in branching.__name__

    def test_loop_accepts_task_class(self):
        """loop() correctly wraps @task class."""
        looping = loop(SimpleTask, until=lambda r: True, max_iterations=3)
        assert callable(looping)
        assert "loop" in looping.__name__


class TestParallelWithTaskClass:
    """Test that parallel combinators accept @task classes."""

    def test_parallel_accepts_task_classes(self):
        """parallel() correctly wraps @task classes."""
        combined = parallel(SimpleTask, AddOneTask)
        assert callable(combined)
        assert "parallel" in combined.__name__

    def test_race_accepts_task_classes(self):
        """race() correctly wraps @task classes."""
        racing = race(SimpleTask, AddOneTask)
        assert callable(racing)
        assert "race" in racing.__name__

    def test_parallel_all_accepts_task_classes(self):
        """parallel_all() correctly wraps @task classes."""
        combined = parallel_all(SimpleTask, AddOneTask, SimpleTask)
        assert callable(combined)
        assert "parallel_all" in combined.__name__


class TestEffectsWithTaskClass:
    """Test that effect combinators accept @task classes."""

    def test_filter_effects_accepts_task_class(self):
        """filter_effects() correctly wraps @task class."""
        filtered = filter_effects(SimpleTask, lambda e: True)
        assert callable(filtered)
        assert "filter_effects" in filtered.__name__

    def test_map_effects_accepts_task_class(self):
        """map_effects() correctly wraps @task class."""
        mapped = map_effects(SimpleTask, lambda e: e)
        assert callable(mapped)
        assert "map_effects" in mapped.__name__

    def test_tap_accepts_task_class(self):
        """tap() correctly wraps @task class."""
        tapping = tap(SimpleTask, lambda r, e: None)
        assert callable(tapping)
        assert "tap" in tapping.__name__

    def test_scope_tap_accepts_task_class(self):
        """scope_tap() correctly wraps @task class."""
        scope_tapping = scope_tap(SimpleTask, lambda s: None)
        assert callable(scope_tapping)
        assert "scope_tap" in scope_tapping.__name__


class TestSpeculationWithTaskClass:
    """Test that speculation combinators accept @task classes."""

    def test_speculate_accepts_task_class(self):
        """speculate() correctly wraps @task class."""
        speculating = speculate(SimpleTask)
        assert callable(speculating)
        assert "speculate" in speculating.__name__


# =============================================================================
# Execution tests using async functions (to avoid provider requirement)
# =============================================================================


class TestCombinatorExecution:
    """Test that combinators execute correctly with async functions."""

    @pytest.mark.asyncio
    async def test_gate_execution(self):
        """gate() correctly executes and gates based on predicate."""

        async def double_task(inputs, scope):
            class Result:
                def __init__(self, x):
                    self.result = x * 2

            return Result(inputs["x"])

        gated = gate(double_task, lambda r, e: r.result > 5)

        with Scope() as scope:
            # Should pass (10 > 5)
            result = await gated({"x": 5}, scope)
            assert result.result == 10

            # Should be rejected (4 <= 5)
            result = await gated({"x": 2}, scope)
            assert isinstance(result, Rejected)
            assert result.value.result == 4

    @pytest.mark.asyncio
    async def test_retry_execution(self):
        """retry() correctly retries on failure."""
        attempt_count = 0

        async def flaky_task(inputs, scope):
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                raise ValueError("Not yet!")
            return {"result": inputs["x"] * 2}

        retrying = retry(flaky_task, max_attempts=5)

        with Scope() as scope:
            result = await retrying({"x": 5}, scope)
            assert result["result"] == 10
            assert attempt_count == 3

    @pytest.mark.asyncio
    async def test_parallel_execution(self):
        """parallel() correctly runs tasks concurrently."""

        async def task_a(inputs, scope):
            return {"a": inputs["x"] * 2}

        async def task_b(inputs, scope):
            return {"b": inputs["x"] + 1}

        combined = parallel(task_a, task_b)

        with Scope() as scope:
            result_a, result_b = await combined({"x": 5}, scope)
            assert result_a["a"] == 10
            assert result_b["b"] == 6

    @pytest.mark.asyncio
    async def test_speculate_execution(self):
        """speculate() correctly captures result for manual commit."""

        async def double_task(inputs, scope):
            return {"result": inputs["x"] * 2}

        speculating = speculate(double_task)

        with Scope() as scope:
            spec_result = await speculating({"x": 5}, scope)

            assert spec_result.output["result"] == 10
            assert not spec_result.is_decided

            # Commit the result
            spec_result.commit()
            assert spec_result.is_decided
