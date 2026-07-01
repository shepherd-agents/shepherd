"""Tests for behavioral grounding functions.

These tests verify:
1. GroundingResult dataclass properties
2. behavioral_grounding() function
3. ground_transformation() function
4. Task execution and output extraction
5. Goal checking
"""

from dataclasses import dataclass
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel
from shepherd_runtime.nucleus import reset_workspace_for_tests, workspace
from shepherd_runtime.nucleus import task as nucleus_task
from shepherd_runtime.scope import Scope
from shepherd_tests import MockProvider
from shepherd_transform.grounding import (
    EquivalenceLevel,
    GroundingResult,
    Mismatch,
    behavioral_grounding,
    ground_transformation,
)

# =============================================================================
# Test Fixtures - Plain Task Classes (for behavioral grounding tests)
#
# Note: We use plain Pydantic models with task_meta and compute_outputs()
# instead of @task decorator to avoid execution lifecycle complexity.
# This matches the pattern used in spike_behavioral_grounding.py.
# =============================================================================


@dataclass
class MockTaskMeta:
    """Simple metadata for test tasks."""

    name: str
    inputs: dict[str, type]
    outputs: dict[str, type]


class Calculator(BaseModel):
    """Simple calculator task."""

    model_config = {"extra": "allow"}

    x: int
    y: int
    result: int = 0

    _task_meta: ClassVar[MockTaskMeta] = MockTaskMeta(
        name="Calculator",
        inputs={"x": int, "y": int},
        outputs={"result": int},
    )

    def compute_outputs(self) -> dict[str, Any]:
        return {"result": self.x + self.y}


class CalculatorWithLogging(BaseModel):
    """Calculator with added logging output."""

    model_config = {"extra": "allow"}

    x: int
    y: int
    result: int = 0
    log: str = ""

    _task_meta: ClassVar[MockTaskMeta] = MockTaskMeta(
        name="CalculatorWithLogging",
        inputs={"x": int, "y": int},
        outputs={"result": int, "log": str},
    )

    def compute_outputs(self) -> dict[str, Any]:
        result = self.x + self.y
        return {
            "result": result,
            "log": f"Computed {self.x} + {self.y} = {result}",
        }


class CalculatorBroken(BaseModel):
    """Calculator with bug - does subtraction instead of addition."""

    model_config = {"extra": "allow"}

    x: int
    y: int
    result: int = 0

    _task_meta: ClassVar[MockTaskMeta] = MockTaskMeta(
        name="CalculatorBroken",
        inputs={"x": int, "y": int},
        outputs={"result": int},
    )

    def compute_outputs(self) -> dict[str, Any]:
        return {"result": self.x - self.y}  # Bug!


class CalculatorWithValidation(BaseModel):
    """Calculator with validation output."""

    model_config = {"extra": "allow"}

    x: int
    y: int
    result: int = 0
    validated: bool = False

    _task_meta: ClassVar[MockTaskMeta] = MockTaskMeta(
        name="CalculatorWithValidation",
        inputs={"x": int, "y": int},
        outputs={"result": int, "validated": bool},
    )

    def compute_outputs(self) -> dict[str, Any]:
        return {
            "result": self.x + self.y,
            "validated": isinstance(self.x, int) and isinstance(self.y, int),
        }


@nucleus_task
def add_values(x: int, y: int) -> dict[str, int]:
    return {"result": x + y}


@nucleus_task
def add_values_with_log(x: int, y: int) -> dict[str, object]:
    result = x + y
    return {"result": result, "log": f"{x}+{y}={result}"}


@nucleus_task
def subtract_values(x: int, y: int) -> dict[str, int]:
    return {"result": x - y}


@pytest.fixture
def nucleus_workspace(tmp_path):
    reset_workspace_for_tests()
    workspace(model="offline-grounding", root=tmp_path)
    yield
    reset_workspace_for_tests()


# =============================================================================
# GroundingResult Tests
# =============================================================================


class TestGroundingResult:
    """Test GroundingResult dataclass."""

    def test_match_rate_calculation(self):
        """Match rate is correctly calculated."""
        result = GroundingResult(test_count=10, match_count=8)
        assert result.match_rate == 0.8

    def test_match_rate_zero_tests(self):
        """Match rate is 0 when no tests run."""
        result = GroundingResult(test_count=0, match_count=0)
        assert result.match_rate == 0.0

    def test_passed_with_high_match_rate(self):
        """Passes when match rate >= 95% and goal achieved."""
        result = GroundingResult(
            test_count=100,
            match_count=96,
            goal_achieved=True,
        )
        assert result.passed is True

    def test_fails_with_low_match_rate(self):
        """Fails when match rate < 95%."""
        result = GroundingResult(
            test_count=100,
            match_count=90,
            goal_achieved=True,
        )
        assert result.passed is False

    def test_fails_when_goal_not_achieved(self):
        """Fails when goal not achieved even with high match rate."""
        result = GroundingResult(
            test_count=100,
            match_count=100,
            goal_achieved=False,
        )
        assert result.passed is False

    def test_summary_output(self):
        """Summary method produces readable output."""
        result = GroundingResult(
            test_count=10,
            match_count=8,
            goal_achieved=True,
            confidence=0.75,
            mismatches=[
                Mismatch(
                    test_input={"x": 5, "y": 3},
                    original_output={"result": 8},
                    transformed_output={"result": 2},
                ),
            ],
        )
        summary = result.summary()
        assert "FAILED" in summary  # 80% < 95%
        assert "8/10" in summary
        assert "75%" in summary


class TestMismatch:
    """Test Mismatch dataclass."""

    def test_to_dict(self):
        """Mismatch serializes to dict."""
        mismatch = Mismatch(
            test_input={"x": 5},
            original_output={"result": 8},
            transformed_output={"result": 2},
            error=None,
        )
        d = mismatch.to_dict()
        assert d["test_input"] == {"x": 5}
        assert d["original_output"] == {"result": 8}
        assert d["transformed_output"] == {"result": 2}

    def test_to_dict_with_error(self):
        """Mismatch with error includes it in dict."""
        mismatch = Mismatch(
            test_input={"x": 5},
            error="Execution failed",
        )
        d = mismatch.to_dict()
        assert d["error"] == "Execution failed"


# =============================================================================
# behavioral_grounding() Tests
# =============================================================================


class TestBehavioralGrounding:
    """Test behavioral_grounding() function."""

    def test_identical_tasks_pass(self):
        """Identical task classes produce 100% match."""
        result = behavioral_grounding(
            original_class=Calculator,
            transformed_class=Calculator,
            test_cases=[
                {"x": 5, "y": 3},
                {"x": 10, "y": 20},
                {"x": -5, "y": 5},
            ],
        )
        assert result.match_rate == 1.0
        assert result.passed is True
        assert len(result.mismatches) == 0

    def test_behavior_preserving_transformation(self):
        """Transformation that adds output but preserves core behavior passes."""
        result = behavioral_grounding(
            original_class=Calculator,
            transformed_class=CalculatorWithLogging,
            test_cases=[
                {"x": 5, "y": 3},
                {"x": 10, "y": 20},
            ],
            equivalence=EquivalenceLevel.OUTCOME,  # Extra outputs allowed
        )
        assert result.passed is True
        # Core output should match
        assert (
            all(m.original_output.get("result") == m.transformed_output.get("result") for m in result.mismatches)
            if result.mismatches
            else True
        )

    def test_behavior_breaking_transformation_fails(self):
        """Transformation that breaks behavior fails."""
        result = behavioral_grounding(
            original_class=Calculator,
            transformed_class=CalculatorBroken,
            test_cases=[
                {"x": 5, "y": 3},  # Original: 8, Broken: 2
                {"x": 10, "y": 5},  # Original: 15, Broken: 5
            ],
        )
        assert result.passed is False
        assert len(result.mismatches) > 0

    def test_empty_test_cases(self):
        """Empty test cases returns 0 confidence."""
        result = behavioral_grounding(
            original_class=Calculator,
            transformed_class=Calculator,
            test_cases=[],
        )
        assert result.test_count == 0
        assert result.confidence == 0.0

    def test_goal_check_passes(self):
        """Goal check function is called and affects result."""

        def has_log_output(cls):
            return hasattr(cls, "log") or "log" in str(cls.model_fields)

        result = behavioral_grounding(
            original_class=Calculator,
            transformed_class=CalculatorWithLogging,
            test_cases=[{"x": 5, "y": 3}],
            goal_check=has_log_output,
        )
        assert result.goal_achieved is True

    def test_goal_check_fails(self):
        """Goal check failure affects result."""

        def requires_validation(cls):
            return hasattr(cls, "validated") or "validated" in str(cls.model_fields)

        result = behavioral_grounding(
            original_class=Calculator,
            transformed_class=CalculatorWithLogging,  # No validated field
            test_cases=[{"x": 5, "y": 3}],
            goal_check=requires_validation,
        )
        assert result.goal_achieved is False

    def test_strict_equivalence(self):
        """STRICT equivalence rejects extra outputs."""
        result = behavioral_grounding(
            original_class=Calculator,
            transformed_class=CalculatorWithLogging,
            test_cases=[{"x": 5, "y": 3}],
            equivalence=EquivalenceLevel.STRICT,
        )
        # Should fail because transformed has extra 'log' output
        assert result.passed is False

    def test_relaxed_equivalence_with_important_fields(self):
        """RELAXED equivalence only checks important fields."""
        result = behavioral_grounding(
            original_class=Calculator,
            transformed_class=CalculatorWithLogging,
            test_cases=[{"x": 5, "y": 3}],
            equivalence=EquivalenceLevel.RELAXED,
            important_fields={"result"},
        )
        assert result.passed is True

    def test_function_form_identical_tasks_pass(self, nucleus_workspace):
        """Function-form callable tasks can be grounded directly."""
        result = behavioral_grounding(
            original_class=add_values,
            transformed_class=add_values,
            test_cases=[{"x": 1, "y": 2}, {"x": -5, "y": 10}],
        )

        assert result.passed is True
        assert result.match_count == result.test_count

    def test_function_form_extra_outputs_preserve_outcome(self, nucleus_workspace):
        """Function-form transforms may add outputs under OUTCOME equivalence."""
        result = behavioral_grounding(
            original_class=add_values,
            transformed_class=add_values_with_log,
            test_cases=[{"x": 1, "y": 2}],
            equivalence=EquivalenceLevel.OUTCOME,
        )

        assert result.passed is True

    def test_function_form_behavior_breaking_transform_fails(self, nucleus_workspace):
        """Function-form transforms that change behavior fail grounding."""
        result = behavioral_grounding(
            original_class=add_values,
            transformed_class=subtract_values,
            test_cases=[{"x": 5, "y": 3}],
        )

        assert result.passed is False
        assert len(result.mismatches) == 1


# =============================================================================
# ground_transformation() Tests
# =============================================================================


# Note: These source strings use @task decorator because ground_transformation
# uses secure_reconstruct which expects @task decorated classes.
# The reconstructed classes must have compute_outputs() or execute() method
# that returns a dict without requiring a Scope.

VALID_TRANSFORMED_SOURCE = '''
@task
class CalculatorTransformed(BaseModel):
    """Transformed calculator."""
    x: Input(int)
    y: Input(int)
    result: Output(int) = None
    doubled: Output(int) = None

    def compute_outputs(self):
        result = (self.x or 0) + (self.y or 0)
        return {"result": result, "doubled": result * 2}
'''


BROKEN_TRANSFORMED_SOURCE = '''
@task
class CalculatorBroken(BaseModel):
    """Broken calculator."""
    x: Input(int)
    y: Input(int)
    result: Output(int) = None

    def compute_outputs(self):
        return {"result": (self.x or 0) - (self.y or 0)}  # Bug!
'''


MALICIOUS_SOURCE = """
import os
@task
class MaliciousTask(BaseModel):
    x: Input(int)
    result: Output(str) = None
"""


class TestGroundTransformation:
    """Test ground_transformation() function.

    Note: These tests use Scope with MockProvider because secure_reconstruct creates
    @task decorated classes that require a Scope for instantiation.
    """

    def test_successful_transformation(self):
        """Successful reconstruction and grounding."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result, transformed_class = ground_transformation(
                original_class=Calculator,
                transformed_source=VALID_TRANSFORMED_SOURCE,
                test_cases=[{"x": 5, "y": 3}, {"x": 10, "y": 20}],
            )
            assert transformed_class is not None
            assert result.passed is True
            # Core result should match
            assert result.match_count == result.test_count

    def test_broken_transformation_fails_grounding(self):
        """Broken transformation fails behavioral grounding."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result, transformed_class = ground_transformation(
                original_class=Calculator,
                transformed_source=BROKEN_TRANSFORMED_SOURCE,
                test_cases=[{"x": 5, "y": 3}],
            )
            assert transformed_class is not None
            assert result.passed is False
            assert len(result.mismatches) > 0

    def test_malicious_source_fails_reconstruction(self):
        """Malicious source fails secure reconstruction."""
        result, transformed_class = ground_transformation(
            original_class=Calculator,
            transformed_source=MALICIOUS_SOURCE,
            test_cases=[{"x": 5, "y": 3}],
        )
        assert transformed_class is None
        assert result.passed is False
        assert "Reconstruction failed" in str(result.mismatches[0].error)

    def test_syntax_error_fails_reconstruction(self):
        """Syntax error in source fails reconstruction."""
        result, transformed_class = ground_transformation(
            original_class=Calculator,
            transformed_source="class Foo(: pass",  # Syntax error
            test_cases=[{"x": 5, "y": 3}],
        )
        assert transformed_class is None
        assert result.passed is False

    def test_extra_namespace(self):
        """Extra namespace bindings are passed to reconstruction."""
        source_with_constant = """
@task
class TaskWithConstant(BaseModel):
    x: Input(int)
    result: Output(int) = None

    def compute_outputs(self):
        return {"result": (self.x or 0) + MAGIC_NUMBER}
"""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            _result, transformed_class = ground_transformation(
                original_class=Calculator,
                transformed_source=source_with_constant,
                test_cases=[{"x": 5, "y": 3}],
                extra_namespace={"MAGIC_NUMBER": 100},
            )
            # Should succeed reconstruction
            assert transformed_class is not None


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_task_with_compute_outputs_method(self):
        """Tasks with compute_outputs() method are supported."""

        class TaskWithComputeOutputs(BaseModel):
            x: int
            result: int = 0

            _task_meta = type("Meta", (), {"outputs": {"result"}})()

            def compute_outputs(self):
                return {"result": self.x * 2}

        result = behavioral_grounding(
            original_class=TaskWithComputeOutputs,
            transformed_class=TaskWithComputeOutputs,
            test_cases=[{"x": 5}],
        )
        assert result.passed is True

    def test_task_execution_error_is_mismatch(self):
        """Task that throws exception is treated as mismatch."""

        class ThrowingTask(BaseModel):
            x: int
            y: int
            result: int = 0

            _task_meta = type("Meta", (), {"outputs": {"result"}})()

            def compute_outputs(self):
                raise ValueError("Intentional error")

        result = behavioral_grounding(
            original_class=Calculator,
            transformed_class=ThrowingTask,
            test_cases=[{"x": 5, "y": 3}],
        )
        assert result.passed is False
        assert any(m.error for m in result.mismatches)

    def test_both_tasks_fail_is_mismatch(self):
        """Both tasks failing on same input does not prove behavioral equivalence."""

        class AlwaysThrows(BaseModel):
            x: int
            result: int = 0

            _task_meta = type("Meta", (), {"outputs": {"result"}})()

            def compute_outputs(self):
                raise ValueError("Always fails")

        result = behavioral_grounding(
            original_class=AlwaysThrows,
            transformed_class=AlwaysThrows,
            test_cases=[{"x": 5}],
        )
        assert result.passed is False
        assert result.match_count == 0
        assert len(result.mismatches) == 1
        assert "original failed" in (result.mismatches[0].error or "")
        assert "transformed failed" in (result.mismatches[0].error or "")
