"""Tests for @step decorator basics and inline step syntax.

This module tests:
1. @step decorator basics (method-based steps)
2. self.step[T](...) inline step syntax
3. Step lifecycle effects (StepStarted, StepCompleted, StepFailed)
"""

from typing import Literal

import pytest
from pydantic import BaseModel
from shepherd_core.effects import StepCompleted, StepStarted
from shepherd_runtime.scope import Scope
from shepherd_runtime.step.api import step
from shepherd_runtime.task.authoring import Input, Output, task
from shepherd_tests import MockProvider
from shepherd_tests.tasks import (
    INLINE_STEP_TEST_CASES,
    RETURN_TYPE_TEST_CASES,
    make_inline_step_task,
    make_step_task,
)

from .conftest import Severity

# =============================================================================
# Step Decorator Basics
# =============================================================================


class TestStepDecoratorBasics:
    """Test @step decorator fundamentals.

    Return type tests (literal, string, int, bool, list, float, dict) are
    consolidated into test_step_return_type_mock_values using the factory
    from shepherd_tests.tasks. Tests for unique behaviors remain inline.
    """

    # -------------------------------------------------------------------------
    # Parameterized return type tests (replaces 5 individual tests, adds 2 new)
    # -------------------------------------------------------------------------

    @pytest.mark.parametrize("case", RETURN_TYPE_TEST_CASES, ids=lambda c: c.id)
    def test_step_return_type_mock_values(self, case):
        """@step methods return correct mock values for different return types.

        This parameterized test consolidates 5 return-type tests and adds
        coverage for float and dict (not previously tested).
        """
        Task = make_step_task(case.return_type)

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            t = Task(input_val="test")
        if case.partial_match:
            assert case.expected in str(t.output_val), f"Expected '{case.expected}' in output, got '{t.output_val}'"
        else:
            assert t.output_val == case.expected, f"Expected {case.expected!r}, got {t.output_val!r}"

    # -------------------------------------------------------------------------
    # Tests that remain inline (unique behavior being tested)
    # -------------------------------------------------------------------------

    def test_step_creates_callable_method(self):
        """INLINE: Tests basic @step callable behavior, not mock values."""

        @task
        class TestTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            @step
            def classify(self, text: str) -> Literal["a", "b"]:
                """Classify the text."""

            def execute(self):
                result = self.classify(self.input_val)
                self.output_val = result

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            t = TestTask(input_val="test")
        # Should complete without error
        assert t.output_val is not None

    def test_step_with_enum_return_type(self):
        """INLINE: Tests Enum with explicit .value coercion in execute().

        The factory pattern works for assertion (Severity.LOW == "low"),
        but the original test explicitly calls result.value, which is
        different behavior worth preserving.
        """

        @task
        class TestTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            @step
            def classify_severity(self, text: str) -> Severity:
                """Classify the severity."""

            def execute(self):
                result = self.classify_severity(self.input_val)
                self.output_val = result.value

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            t = TestTask(input_val="test")
        assert t.output_val == "low"  # First enum value

    def test_step_with_multiple_parameters(self):
        """INLINE: Tests multi-parameter step methods."""

        @task
        class TestTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            @step
            def combine(self, text: str, prefix: str, count: int) -> str:
                """Combine inputs into result."""

            def execute(self):
                self.output_val = self.combine(self.input_val, "pre", 5)

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            t = TestTask(input_val="test")
        assert t.output_val is not None

    def test_step_with_shepherd_false(self):
        """INLINE: Tests shepherd=False parameter."""

        @task
        class TestTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            @step(shepherd=False)
            def pure_reasoning(self, text: str) -> str:
                """Pure reasoning step without tools."""

            def execute(self):
                self.output_val = self.pure_reasoning(self.input_val)

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            t = TestTask(input_val="test")
        assert t.output_val is not None

    def test_step_with_custom_timeout(self):
        """INLINE: Tests timeout metadata storage."""

        @step(timeout=300)
        def long_step(self, text: str) -> str:
            """A long-running step."""

        assert hasattr(long_step, "_step_metadata")
        assert long_step._step_metadata.timeout == 300


# =============================================================================
# Inline Step Syntax
# =============================================================================


class TestInlineStepSyntax:
    """Test self.step[T](...) inline syntax.

    Return type tests (literal, string) have been consolidated into
    test_inline_step_return_types using the factory pattern. Tests for
    unique behaviors (multiple placeholders, custom timeout) remain inline.
    """

    # -------------------------------------------------------------------------
    # Parameterized return type tests (replaces 2 individual tests)
    # -------------------------------------------------------------------------

    @pytest.mark.parametrize("case", INLINE_STEP_TEST_CASES, ids=lambda c: c.id)
    def test_inline_step_return_types(self, case):
        """self.step[T]() returns correct mock values for different types.

        This parameterized test consolidates the literal and string return
        type tests using the factory pattern.
        """
        Task = make_inline_step_task(case.return_type, case.prompt_template)

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            t = Task(input_val="test")
        if case.partial_match:
            assert case.expected in str(t.output_val), f"Expected '{case.expected}' in output, got '{t.output_val}'"
        elif isinstance(case.expected, tuple):
            # For Literal types, check if output is one of expected values
            assert t.output_val in case.expected, f"Expected one of {case.expected}, got '{t.output_val}'"
        else:
            assert t.output_val == case.expected, f"Expected {case.expected!r}, got {t.output_val!r}"

    # -------------------------------------------------------------------------
    # Tests that remain inline (unique behavior being tested)
    # -------------------------------------------------------------------------

    def test_inline_step_with_multiple_placeholders(self):
        """INLINE: Tests multiple template placeholders."""

        @task
        class TestTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            def execute(self):
                self.output_val = self.step[str](
                    "Compare {a} with {b} considering {c}",
                    a="first",
                    b="second",
                    c="context",
                )

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            t = TestTask(input_val="test")
        assert t.output_val is not None

    def test_inline_step_with_custom_timeout(self):
        """INLINE: Tests timeout parameter passing."""

        @task
        class TestTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            def execute(self):
                self.output_val = self.step[str](
                    "Process: {val}",
                    val=self.input_val,
                    timeout=60,  # Custom timeout
                )

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            t = TestTask(input_val="test")
        assert t.output_val is not None


# =============================================================================
# Step Effects
# =============================================================================


class TestStepEffects:
    """Test effect emission during step execution."""

    def test_step_emits_started_and_completed(self):
        """Steps emit StepStarted and StepCompleted effects."""

        @task
        class TestTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            @step
            def do_thing(self, x: str) -> str:
                """Do the thing."""

            def execute(self):
                self.output_val = self.do_thing(self.input_val)

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            t = TestTask(input_val="test")
            effects = [layer.effect for layer in t.effects]
            effect_types = [type(e).__name__ for e in effects]

            assert "StepStarted" in effect_types
            assert "StepCompleted" in effect_types

    def test_step_started_has_correct_attributes(self):
        """StepStarted effect has correct step_name and parent_task."""

        @task
        class MyTestTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            @step
            def my_step_method(self, x: str) -> str:
                """My step method."""

            def execute(self):
                self.output_val = self.my_step_method(self.input_val)

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            t = MyTestTask(input_val="test")
            started_effects = [layer.effect for layer in t.effects if isinstance(layer.effect, StepStarted)]
            assert len(started_effects) == 1
            assert started_effects[0].step_name == "my_step_method"
            assert started_effects[0].parent_task == "MyTestTask"

    def test_step_completed_has_outputs_summary(self):
        """StepCompleted effect includes outputs_summary."""

        @task
        class TestTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            @step
            def classify(self, x: str) -> Literal["a", "b"]:
                """Classify."""

            def execute(self):
                self.output_val = self.classify(self.input_val)

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            t = TestTask(input_val="test")
            completed_effects = [layer.effect for layer in t.effects if isinstance(layer.effect, StepCompleted)]
            assert len(completed_effects) == 1
            assert completed_effects[0].outputs_summary != ""

    def test_inline_step_emits_effects(self):
        """Inline steps also emit StepStarted and StepCompleted."""

        @task
        class TestTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            def execute(self):
                self.output_val = self.step[str]("Process: {val}", val=self.input_val)

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            t = TestTask(input_val="test")
            effect_types = [type(layer.effect).__name__ for layer in t.effects]
            assert "StepStarted" in effect_types
            assert "StepCompleted" in effect_types

    def test_multiple_steps_emit_multiple_effects(self):
        """Multiple steps emit separate effect pairs."""

        @task
        class TestTask(BaseModel):
            input_val: Input(str)
            output_val: Output(str)

            @step
            def step_one(self, x: str) -> str:
                """First step."""

            @step
            def step_two(self, x: str) -> str:
                """Second step."""

            def execute(self):
                r1 = self.step_one(self.input_val)
                r2 = self.step_two(r1)
                self.output_val = r2

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            t = TestTask(input_val="test")
            started_effects = [layer.effect for layer in t.effects if isinstance(layer.effect, StepStarted)]
            completed_effects = [layer.effect for layer in t.effects if isinstance(layer.effect, StepCompleted)]

            assert len(started_effects) == 2
            assert len(completed_effects) == 2
