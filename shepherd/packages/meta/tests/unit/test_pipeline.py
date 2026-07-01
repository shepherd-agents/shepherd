"""Tests for Pipeline fluent API.

Tests that Pipeline provides a convenient fluent interface for composing
tasks with combinators like retry, gate, and timeout.
"""

import pytest
from pydantic import BaseModel
from shepherd.pipeline import Pipeline, PipelineResult
from shepherd_core.errors import ScopeNotConfiguredError
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
class ValidationTask(BaseModel):
    """Task with a field we can validate."""

    code: Input(str)
    is_valid: Output(bool)
    message: Output(str)


# =============================================================================
# Tests for Pipeline construction
# =============================================================================


class TestPipelineConstruction:
    """Tests for Pipeline construction."""

    def test_pipeline_wraps_task_class(self):
        """Pipeline stores reference to task class."""
        pipeline = Pipeline(SimpleTask)
        assert pipeline._task_class is SimpleTask

    def test_pipeline_starts_with_empty_combinators(self):
        """Pipeline starts with no combinators."""
        pipeline = Pipeline(SimpleTask)
        assert pipeline._combinators == []

    def test_pipeline_repr_shows_task_name(self):
        """Pipeline repr includes task class name."""
        pipeline = Pipeline(SimpleTask)
        assert "SimpleTask" in repr(pipeline)


# =============================================================================
# Tests for Pipeline.retry()
# =============================================================================


class TestPipelineRetry:
    """Tests for Pipeline.retry() method."""

    def test_retry_returns_self_for_chaining(self):
        """retry() returns self for method chaining."""
        pipeline = Pipeline(SimpleTask)
        result = pipeline.retry(max_attempts=3)
        assert result is pipeline

    def test_retry_adds_combinator(self):
        """retry() adds a combinator to the list."""
        pipeline = Pipeline(SimpleTask)
        pipeline.retry(max_attempts=3)
        assert len(pipeline._combinators) == 1

    def test_retry_can_chain_multiple_times(self):
        """Can chain multiple retry() calls."""
        pipeline = Pipeline(SimpleTask).retry(3).retry(2)
        assert len(pipeline._combinators) == 2


# =============================================================================
# Tests for Pipeline.gate()
# =============================================================================


class TestPipelineGate:
    """Tests for Pipeline.gate() method."""

    def test_gate_returns_self_for_chaining(self):
        """gate() returns self for method chaining."""
        pipeline = Pipeline(SimpleTask)
        result = pipeline.gate(lambda r: True)
        assert result is pipeline

    def test_gate_adds_combinator(self):
        """gate() adds a combinator to the list."""
        pipeline = Pipeline(SimpleTask)
        pipeline.gate(lambda r: True)
        assert len(pipeline._combinators) == 1

    def test_gate_accepts_single_arg_predicate(self):
        """gate() accepts single-arg predicate."""
        pipeline = Pipeline(SimpleTask)
        pipeline.gate(lambda r: r.result == "good")
        assert len(pipeline._combinators) == 1

    def test_gate_accepts_two_arg_predicate(self):
        """gate() accepts two-arg predicate (result, effects)."""
        pipeline = Pipeline(SimpleTask)
        pipeline.gate(lambda r, e: r.result == "good" and len(e) == 0)
        assert len(pipeline._combinators) == 1


# =============================================================================
# Tests for Pipeline.timeout()
# =============================================================================


class TestPipelineTimeout:
    """Tests for Pipeline.timeout() method."""

    def test_timeout_returns_self_for_chaining(self):
        """timeout() returns self for method chaining."""
        pipeline = Pipeline(SimpleTask)
        result = pipeline.timeout(30)
        assert result is pipeline

    def test_timeout_adds_combinator(self):
        """timeout() adds a combinator to the list."""
        pipeline = Pipeline(SimpleTask)
        pipeline.timeout(30)
        assert len(pipeline._combinators) == 1


# =============================================================================
# Tests for Pipeline.build()
# =============================================================================


class TestPipelineBuild:
    """Tests for Pipeline.build() method."""

    def test_build_returns_callable(self):
        """build() returns a callable."""
        pipeline = Pipeline(SimpleTask)
        built = pipeline.build()
        assert callable(built)

    def test_build_applies_combinators(self):
        """build() applies all configured combinators."""
        pipeline = Pipeline(SimpleTask).retry(3).gate(lambda r, e: True)
        built = pipeline.build()
        # The built callable should be wrapped by combinators
        assert callable(built)


# =============================================================================
# Tests for Pipeline.run() and Pipeline.arun()
# =============================================================================


class TestPipelineExecution:
    """Tests for Pipeline execution methods."""

    def test_run_returns_pipeline_result(self):
        """run() returns a PipelineResult."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = Pipeline(SimpleTask).run(scope=scope, prompt="hello")

            assert isinstance(result, PipelineResult)

    @pytest.mark.asyncio
    async def test_arun_returns_pipeline_result(self):
        """arun() returns a PipelineResult."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = await Pipeline(SimpleTask).arun(scope=scope, prompt="hello")

            assert isinstance(result, PipelineResult)

    def test_run_with_retry(self):
        """run() with retry combinator works."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = Pipeline(SimpleTask).retry(3).run(scope=scope, prompt="hello")

            assert isinstance(result, PipelineResult)
            assert not result.rejected

    def test_run_with_passing_gate(self):
        """run() with passing gate returns non-rejected result."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = (
                Pipeline(SimpleTask)
                .gate(lambda r, e: True)  # Always pass
                .run(scope=scope, prompt="hello")
            )

            assert isinstance(result, PipelineResult)
            assert not result.rejected

    def test_run_with_failing_gate(self):
        """run() with failing gate returns rejected result."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = (
                Pipeline(SimpleTask)
                .gate(lambda r, e: False)  # Always fail
                .run(scope=scope, prompt="hello")
            )

            assert isinstance(result, PipelineResult)
            assert result.rejected

    def test_run_without_scope_raises_error(self):
        """run() without configured scope raises ScopeNotConfiguredError."""
        with pytest.raises(ScopeNotConfiguredError):
            Pipeline(SimpleTask).run(prompt="hello")


# =============================================================================
# Tests for PipelineResult
# =============================================================================


class TestPipelineResult:
    """Tests for PipelineResult dataclass."""

    def test_pipeline_result_delegates_attribute_access(self):
        """PipelineResult delegates attribute access to value."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = Pipeline(SimpleTask).run(scope=scope, prompt="hello")

            # Should delegate to value.prompt
            assert result.prompt == "hello"

    def test_pipeline_result_rejected_has_reason(self):
        """Rejected PipelineResult has reason."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = Pipeline(SimpleTask).gate(lambda r, e: False).run(scope=scope, prompt="hello")

            assert result.rejected
            # Reason may be None or a string depending on gate implementation
            # Just verify we can access it
            _ = result.reason

    def test_pipeline_result_has_effects(self):
        """PipelineResult has effects stream."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = Pipeline(SimpleTask).run(scope=scope, prompt="hello")

            # Should have access to effects
            assert hasattr(result, "effects")

    def test_pipeline_result_dir_includes_value_attrs(self):
        """PipelineResult.__dir__() includes value's attributes."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = Pipeline(SimpleTask).run(scope=scope, prompt="hello")

            attrs = dir(result)
            assert "value" in attrs
            assert "effects" in attrs
            assert "rejected" in attrs
            # Should also include delegated attrs
            assert "prompt" in attrs

    def test_pipeline_result_repr_shows_value(self):
        """PipelineResult repr shows value."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = Pipeline(SimpleTask).run(scope=scope, prompt="hello")

            repr_str = repr(result)
            assert "PipelineResult" in repr_str

    def test_pipeline_result_repr_shows_rejected(self):
        """Rejected PipelineResult repr shows rejected status."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = Pipeline(SimpleTask).gate(lambda r, e: False).run(scope=scope, prompt="hello")

            repr_str = repr(result)
            assert "rejected" in repr_str.lower()


# =============================================================================
# Tests for chaining
# =============================================================================


class TestPipelineChaining:
    """Tests for Pipeline method chaining."""

    def test_fluent_chaining(self):
        """Can chain multiple methods fluently."""
        pipeline = Pipeline(SimpleTask).retry(max_attempts=3).gate(lambda r, e: True).timeout(30)

        assert len(pipeline._combinators) == 3

    def test_chained_pipeline_executes(self):
        """Chained pipeline executes successfully."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = Pipeline(SimpleTask).retry(2).gate(lambda r, e: True).run(scope=scope, prompt="hello")

            assert isinstance(result, PipelineResult)
            assert not result.rejected


# =============================================================================
# Tests for Pipeline.recover()
# =============================================================================


class TestPipelineRecover:
    """Tests for Pipeline.recover() method."""

    def test_recover_returns_self_for_chaining(self):
        """recover() returns self for method chaining."""
        pipeline = Pipeline(SimpleTask)
        result = pipeline.recover(lambda e: None)
        assert result is pipeline

    def test_recover_adds_combinator(self):
        """recover() adds a combinator to the list."""
        pipeline = Pipeline(SimpleTask)
        pipeline.recover(lambda e: None)
        assert len(pipeline._combinators) == 1

    def test_recover_chains_with_retry(self):
        """recover() can chain after retry() - try N times, then fallback."""
        pipeline = Pipeline(SimpleTask).retry(3).recover(lambda e: None)
        assert len(pipeline._combinators) == 2

    def test_recover_chains_with_gate(self):
        """recover() can chain with gate()."""
        pipeline = Pipeline(SimpleTask).recover(lambda e: None).gate(lambda r, e: True)
        assert len(pipeline._combinators) == 2

    def test_recover_in_full_chain(self):
        """recover() works in a full combinator chain."""
        pipeline = (
            Pipeline(SimpleTask).retry(max_attempts=3).gate(lambda r, e: True).timeout(30).recover(lambda e: None)
        )
        assert len(pipeline._combinators) == 4

    def test_recover_build_returns_callable(self):
        """Pipeline with recover() builds to a callable."""
        pipeline = Pipeline(SimpleTask).recover(lambda e: None)
        built = pipeline.build()
        assert callable(built)

    def test_recover_execution_on_success(self):
        """recover() passes through successful results."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = (
                Pipeline(SimpleTask)
                .recover(lambda e: SimpleTask(prompt="fallback", result="fallback"))
                .run(scope=scope, prompt="hello")
            )

            assert isinstance(result, PipelineResult)
            assert not result.rejected
            # Should have original prompt, not fallback
            assert result.prompt == "hello"
