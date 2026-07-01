"""Tests for StepOutputError and step integration scenarios."""

import asyncio
from typing import Literal

import pytest
from pydantic import BaseModel
from shepherd_core.effects import StepCompleted, StepStarted
from shepherd_runtime.scope import Scope
from shepherd_runtime.step.api import StepOutputError, step
from shepherd_runtime.task.authoring import Input, Output, task
from shepherd_tests import MockProvider

# =============================================================================
# Error Handling
# =============================================================================


class TestStepOutputError:
    """Test StepOutputError exception."""

    def test_error_attributes(self):
        """StepOutputError has correct attributes."""
        error = StepOutputError(
            step_name="my_step",
            expected_type=str,
            received=42,
            reason="Type mismatch",
        )
        assert error.step_name == "my_step"
        assert error.expected_type is str
        assert error.received == 42
        assert error.reason == "Type mismatch"

    def test_error_message_format(self):
        """StepOutputError has informative message."""
        error = StepOutputError(
            step_name="classify",
            expected_type=Literal["a", "b"],
            received="c",
            reason="Not in allowed values",
        )
        message = str(error)
        assert "classify" in message
        assert "Literal" in message
        assert "'c'" in message
        assert "Not in allowed values" in message

    def test_error_is_exception(self):
        """StepOutputError is an Exception."""
        error = StepOutputError("step", str, None, "reason")
        assert isinstance(error, Exception)


# =============================================================================
# Integration Tests
# =============================================================================


class TestStepIntegration:
    """Integration tests combining multiple step features."""

    def test_chained_steps(self):
        """Multiple steps can be chained together."""

        @task
        class ChainedTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            @step
            def step_one(self, text: str) -> Literal["a", "b"]:
                """First step."""

            @step
            def step_two(self, category: str, original: str) -> str:
                """Second step using first step's output."""

            def execute(self):
                category = self.step_one(self.input_val)
                self.output_val = self.step_two(category, self.input_val)

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            t = ChainedTask(input_val="test")
        assert t.output_val is not None

    def test_mixed_step_types(self):
        """Method steps and inline steps can be mixed."""

        @task
        class MixedTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            @step
            def method_step(self, text: str) -> str:
                """Method-based step."""

            def execute(self):
                intermediate = self.method_step(self.input_val)
                final = self.step[Literal["done", "pending"]]("Status for: {val}", val=intermediate)
                self.output_val = final

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            t = MixedTask(input_val="test")
        assert t.output_val in ("done", "pending")

    def test_conditional_step_execution(self):
        """Steps can be conditionally executed."""

        @task
        class ConditionalTask(BaseModel):
            input_val: Input(str)
            mode: Input(str)
            output_val: Output(str)

            @step
            def process_mode_a(self, text: str) -> str:
                """Process in mode A."""

            @step
            def process_mode_b(self, text: str) -> str:
                """Process in mode B."""

            def execute(self):
                if self.mode == "a":
                    self.output_val = self.process_mode_a(self.input_val)
                else:
                    self.output_val = self.process_mode_b(self.input_val)

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            t_a = ConditionalTask(input_val="test", mode="a")
            t_b = ConditionalTask(input_val="test", mode="b")
        assert t_a.output_val is not None
        assert t_b.output_val is not None


# =============================================================================
# Async Custom Execute Tests
# =============================================================================


class TestAsyncCustomExecute:
    """Tests that arun() respects custom execute() methods."""

    @pytest.mark.asyncio
    async def test_arun_calls_custom_execute(self):
        """arun() should call custom execute(), not go to LLM."""

        @task
        class CustomExecTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            def execute(self):
                self.output_val = self.input_val.upper()

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = await CustomExecTask.arun(input_val="hello")
        assert result.output_val == "HELLO"

    @pytest.mark.asyncio
    async def test_arun_calls_custom_execute_with_steps(self):
        """arun() should call custom execute() that uses @step methods."""

        @task
        class SteppedTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            @step
            def classify(self, text: str) -> Literal["a", "b"]:
                """Classify text."""

            def execute(self):
                category = self.classify(self.input_val)
                self.output_val = f"category:{category}"

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = await SteppedTask.arun(input_val="test")
        assert result.output_val.startswith("category:")

    @pytest.mark.asyncio
    async def test_arun_calls_async_custom_execute(self):
        """arun() should support async def execute()."""

        @task
        class AsyncExecTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            async def execute(self):
                self.output_val = self.input_val.upper()

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = await AsyncExecTask.arun(input_val="hello")
        assert result.output_val == "HELLO"

    @pytest.mark.asyncio
    async def test_arun_without_custom_execute_uses_provider(self):
        """arun() without custom execute() should still use LLM provider."""

        @task
        class PlainTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = await PlainTask.arun(input_val="test")
        # MockProvider populates outputs with mock values
        assert result.output_val is not None


# =============================================================================
# Async Step Execution Tests
# =============================================================================


class TestAsyncStepExecution:
    """Tests that @step methods work natively in async contexts."""

    @pytest.mark.asyncio
    async def test_async_step_basic(self):
        """await self.my_step() should work from async execute()."""

        @task
        class AsyncStepTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            @step
            def classify(self, text: str) -> Literal["a", "b"]:
                """Classify text."""

            async def execute(self):
                category = await self.classify(self.input_val)
                self.output_val = f"category:{category}"

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = await AsyncStepTask.arun(input_val="test")
        assert result.output_val.startswith("category:")

    @pytest.mark.asyncio
    async def test_async_step_parallel_gather(self):
        """Multiple steps can run concurrently via asyncio.gather."""

        @task
        class ParallelStepTask(BaseModel):
            input_val: Input(str)
            cat_result: Output(str)
            sent_result: Output(str)

            @step
            def classify(self, text: str) -> Literal["a", "b"]:
                """Classify text."""

            @step
            def detect_sentiment(self, text: str) -> Literal["pos", "neg"]:
                """Detect sentiment."""

            async def execute(self):
                cat, sent = await asyncio.gather(
                    self.classify(self.input_val),
                    self.detect_sentiment(self.input_val),
                )
                self.cat_result = cat
                self.sent_result = sent

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = await ParallelStepTask.arun(input_val="test")
        assert result.cat_result in ("a", "b")
        assert result.sent_result in ("pos", "neg")

    @pytest.mark.asyncio
    async def test_async_inline_step(self):
        """await self.step[T](...) should work from async execute()."""

        @task
        class AsyncInlineTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            async def execute(self):
                result = await self.step[Literal["yes", "no"]]("Is {val} good?", val=self.input_val)
                self.output_val = result

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = await AsyncInlineTask.arun(input_val="test")
        assert result.output_val in ("yes", "no")

    def test_sync_step_still_works(self):
        """Sync step path must be unaffected (regression)."""

        @task
        class SyncStepTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            @step
            def classify(self, text: str) -> Literal["a", "b"]:
                """Classify text."""

            def execute(self):
                self.output_val = self.classify(self.input_val)

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            t = SyncStepTask(input_val="test")
        assert t.output_val in ("a", "b")

    @pytest.mark.asyncio
    async def test_sync_execute_via_arun_still_works(self):
        """Sync execute() called via arun() must NOT get coroutines from steps."""

        @task
        class SyncExecViaArun(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            @step
            def classify(self, text: str) -> Literal["a", "b"]:
                """Classify text."""

            def execute(self):
                # This runs inside arun() with a running event loop,
                # but steps must still return sync values
                self.output_val = self.classify(self.input_val)

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = await SyncExecViaArun.arun(input_val="test")
        assert result.output_val in ("a", "b")

    @pytest.mark.asyncio
    async def test_async_step_effects(self):
        """StepStarted/StepCompleted effects emitted correctly from async path."""

        @task
        class EffectTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            @step
            def classify(self, text: str) -> str:
                """Classify text."""

            async def execute(self):
                self.output_val = await self.classify(self.input_val)

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = await EffectTask.arun(input_val="test")
        started = list(result.effects.query(StepStarted))
        completed = list(result.effects.query(StepCompleted))
        assert len(started) == 1
        assert len(completed) == 1

    @pytest.mark.asyncio
    async def test_step_without_await_returns_coroutine(self):
        """Steps called without await in async execute() return coroutines.

        This is the expected behavior: users must await steps in async
        execute(). Forgetting await gives a coroutine object, which will
        fail at assignment or produce a clear error.
        """

        @task
        class ForgetAwaitTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            @step
            def classify(self, text: str) -> Literal["a", "b"]:
                """Classify text."""

            async def execute(self):
                result = self.classify(self.input_val)
                assert asyncio.iscoroutine(result), "Step should return coroutine in async context"
                # Must await to get the actual value
                self.output_val = await result

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = await ForgetAwaitTask.arun(input_val="test")
        assert result.output_val in ("a", "b")

    @pytest.mark.asyncio
    async def test_async_parent_sync_child_no_contextvar_leak(self):
        """Sync child called from async parent must not inherit _async_execute_mode.

        Regression: _async_execute_mode=True leaked via ContextVar into nested
        sync execute(), causing steps to return coroutines instead of values.
        """

        @task
        class SyncChild(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            @step
            def classify(self, text: str) -> Literal["a", "b"]:
                """Classify text."""

            def execute(self):
                result = self.classify(self.input_val)
                assert not asyncio.iscoroutine(result), "_async_execute_mode leaked: sync step returned coroutine"
                self.output_val = f"category:{result}"

        @task
        class AsyncParent(BaseModel):
            input_val: Input(str)
            child_result: Output(str)

            async def execute(self):
                child = await SyncChild.arun(input_val=self.input_val)
                self.child_result = child.output_val

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = await AsyncParent.arun(input_val="test")
        assert result.child_result.startswith("category:")
