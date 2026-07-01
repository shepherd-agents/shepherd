"""Tests for test input generation.

These tests verify:
1. TaskInputSpec extraction from task classes
2. Type-based value generation
3. Boundary case generation
4. TestInputGenerator methods
5. Coverage analysis
6. Integration with behavioral_grounding
"""

from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Optional

import pytest
from pydantic import BaseModel
from shepherd_runtime.nucleus import task as nucleus_task
from shepherd_runtime.scope import Scope
from shepherd_runtime.task.authoring import Input, Output, task
from shepherd_tests import MockProvider
from shepherd_transform.grounding import (
    EquivalenceLevel,
    TaskInputSpec,
    analyze_coverage,
    behavioral_grounding,
    generate_for_type,
    get_boundary_values,
)
from shepherd_transform.grounding import (
    TestInputGenerator as InputGenerator,
)

# =============================================================================
# Test Fixtures - Task Classes
# =============================================================================


@dataclass
class MockTaskMeta:
    """Simple metadata for test tasks."""

    name: str
    inputs: dict[str, type]
    outputs: dict[str, type]


class SimpleCalculator(BaseModel):
    """Simple calculator for testing."""

    model_config = {"extra": "allow"}

    x: int
    y: int
    result: int = 0

    _task_meta: ClassVar[MockTaskMeta] = MockTaskMeta(
        name="SimpleCalculator",
        inputs={"x": int, "y": int},
        outputs={"result": int},
    )

    def compute_outputs(self) -> dict[str, Any]:
        return {"result": self.x + self.y}


@nucleus_task(name="function-adder")
def function_adder(x: int, y: int = 1) -> int:
    return x + y


# =============================================================================
# TaskInputSpec Tests
# =============================================================================


class TestTaskInputSpec:
    """Test TaskInputSpec dataclass and extraction."""

    def test_from_dict_simple(self):
        """Create spec from simple field dict."""
        spec = TaskInputSpec.from_dict({"x": int, "y": str})
        assert spec.fields == {"x": int, "y": str}
        assert spec.task_name == "Task"

    def test_from_dict_with_name(self):
        """Create spec with custom task name."""
        spec = TaskInputSpec.from_dict({"x": int}, task_name="MyTask")
        assert spec.task_name == "MyTask"

    def test_from_task_class_with_decorator(self):
        """Extract spec from @task decorated class."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)

            @task
            class Calculator(BaseModel):
                x: Input(int)
                y: Input(int)
                result: Output(int) = None

            spec = TaskInputSpec.from_task_class(Calculator)

            assert "x" in spec.fields
            assert "y" in spec.fields
            assert spec.fields["x"] == int
            assert spec.task_name == "Calculator"

    def test_from_task_class_with_defaults(self):
        """Extract spec with default values."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)

            @task
            class TaskWithDefaults(BaseModel):
                query: Input(str)
                max_results: Input(int) = 10
                answer: Output(str) = None

            spec = TaskInputSpec.from_task_class(TaskWithDefaults)

            assert "query" in spec.fields
            assert "max_results" in spec.fields
            # Defaults are only captured if not required
            # In this case, max_results has a default

    def test_empty_spec(self):
        """Empty spec has no fields."""
        spec = TaskInputSpec(task_name="Empty")
        assert spec.fields == {}
        assert spec.defaults == {}

    def test_from_function_form_task(self):
        """Extract spec from a function-form CallableTask."""
        spec = TaskInputSpec.from_task(function_adder)

        assert spec.task_name == "function-adder"
        assert spec.fields == {"x": int, "y": int}
        assert spec.defaults == {"y": 1}

    def test_from_task_dispatches_to_class_form(self):
        """from_task preserves the class-form extraction path."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)

            @task
            class Calculator(BaseModel):
                x: Input(int)
                y: Input(int)
                result: Output(int) = None

            spec = TaskInputSpec.from_task(Calculator)

        assert spec.fields == {"x": int, "y": int}
        assert spec.task_name == "Calculator"


# =============================================================================
# Type Generation Tests
# =============================================================================


class TestGenerateForType:
    """Test generate_for_type function."""

    def test_generate_int(self):
        """Generate int values."""
        value = generate_for_type(int, "signature")
        assert isinstance(value, int)

    def test_generate_float(self):
        """Generate float values."""
        value = generate_for_type(float, "signature")
        assert isinstance(value, float)

    def test_generate_str(self):
        """Generate string values."""
        value = generate_for_type(str, "signature")
        assert isinstance(value, str)

    def test_generate_bool(self):
        """Generate bool values."""
        value = generate_for_type(bool, "signature")
        assert isinstance(value, bool)

    def test_generate_list(self):
        """Generate list values."""
        value = generate_for_type(list[int], "signature")
        assert isinstance(value, list)
        assert all(isinstance(v, int) for v in value)

    def test_generate_dict(self):
        """Generate dict values."""
        value = generate_for_type(dict[str, int], "signature")
        assert isinstance(value, dict)

    def test_generate_literal(self):
        """Generate Literal values."""
        value = generate_for_type(Literal["a", "b", "c"], "signature")
        assert value in ("a", "b", "c")

    def test_generate_optional(self):
        """Generate Optional values."""
        # May return int or None
        values = [generate_for_type(Optional[int], "signature") for _ in range(20)]  # noqa: UP045
        # Should get at least some non-None values
        non_none = [v for v in values if v is not None]
        assert len(non_none) > 0

    def test_generate_with_constraints(self):
        """Generate values respecting constraints."""
        constraints = {"ge": 0, "le": 10}
        for _ in range(10):
            value = generate_for_type(int, "signature", constraints)
            assert 0 <= value <= 10

    def test_generate_boundary_int(self):
        """Boundary strategy generates edge case ints."""
        value = generate_for_type(int, "boundary")
        assert value in [0, 1, -1, -100, 100, 2147483647, -2147483648]

    def test_generate_boundary_list(self):
        """Boundary strategy generates empty list."""
        value = generate_for_type(list[str], "boundary")
        assert value == []


class TestGetBoundaryValues:
    """Test get_boundary_values function."""

    def test_int_boundaries(self):
        """Int has standard boundary values."""
        boundaries = get_boundary_values(int)
        assert 0 in boundaries
        assert 1 in boundaries
        assert -1 in boundaries

    def test_str_boundaries(self):
        """String has boundary values."""
        boundaries = get_boundary_values(str)
        assert "" in boundaries
        assert " " in boundaries

    def test_bool_boundaries(self):
        """Bool has both values."""
        boundaries = get_boundary_values(bool)
        assert True in boundaries
        assert False in boundaries

    def test_list_boundaries(self):
        """List has empty and non-empty cases."""
        boundaries = get_boundary_values(list[int])
        assert [] in boundaries
        assert any(len(b) > 0 for b in boundaries if isinstance(b, list))

    def test_literal_boundaries(self):
        """Literal returns all options."""
        boundaries = get_boundary_values(Literal["x", "y", "z"])
        assert set(boundaries) == {"x", "y", "z"}

    def test_optional_boundaries(self):
        """Optional includes None."""
        boundaries = get_boundary_values(Optional[int])  # noqa: UP045
        assert None in boundaries

    def test_constraints_respected(self):
        """Constraints filter boundary values."""
        boundaries = get_boundary_values(int, {"ge": 0, "le": 100})
        assert all(0 <= b <= 100 for b in boundaries)


# =============================================================================
# TestInputGenerator Tests
# =============================================================================


class TestInputGeneratorClass:
    """Test InputGenerator class."""

    @pytest.fixture
    def simple_spec(self):
        """Simple two-field spec."""
        return TaskInputSpec.from_dict({"x": int, "y": int}, task_name="Calculator")

    def test_generate_from_type(self, simple_spec):
        """Generate type-based inputs."""
        generator = InputGenerator(simple_spec, seed=42)
        inputs = generator.generate_from_type(count=5)

        assert len(inputs) == 5
        for inp in inputs:
            assert "x" in inp
            assert "y" in inp
            assert isinstance(inp["x"], int)
            assert isinstance(inp["y"], int)

    def test_generate_boundary_cases(self, simple_spec):
        """Generate boundary case inputs."""
        generator = InputGenerator(simple_spec, seed=42)
        inputs = generator.generate_boundary_cases()

        assert len(inputs) > 0
        # Should have some zeros
        has_zero = any(inp.get("x") == 0 or inp.get("y") == 0 for inp in inputs)
        assert has_zero

    def test_generate_random(self, simple_spec):
        """Generate random inputs."""
        generator = InputGenerator(simple_spec, seed=42)
        inputs = generator.generate_random(count=10)

        assert len(inputs) == 10
        for inp in inputs:
            assert "x" in inp
            assert "y" in inp

    def test_generate_all_produces_diverse_inputs(self, simple_spec):
        """generate_all combines strategies."""
        generator = InputGenerator(simple_spec, seed=42)
        inputs = generator.generate_all()

        # Should produce 15-20 inputs typically
        assert len(inputs) >= 10
        assert len(inputs) <= 50  # Upper bound

    def test_generate_all_deduplicates(self):
        """generate_all removes duplicate inputs."""
        spec = TaskInputSpec.from_dict({"flag": bool}, task_name="BoolTask")
        generator = InputGenerator(spec, seed=42)
        inputs = generator.generate_all()

        # For a single bool field, should have limited unique values
        unique = {tuple(sorted(inp.items())) for inp in inputs}
        assert len(unique) == len(inputs)  # No duplicates

    def test_seed_reproducibility(self, simple_spec):
        """Same seed produces same inputs."""
        gen1 = InputGenerator(simple_spec, seed=42)
        gen2 = InputGenerator(simple_spec, seed=42)

        inputs1 = gen1.generate_from_type(count=5)
        inputs2 = gen2.generate_from_type(count=5)

        assert inputs1 == inputs2

    def test_complex_types(self):
        """Handle complex type annotations."""
        spec = TaskInputSpec.from_dict(
            {
                "items": list[str],
                "config": dict[str, int],
                "mode": Literal["fast", "slow"],
            }
        )
        generator = InputGenerator(spec, seed=42)
        inputs = generator.generate_all()

        assert len(inputs) > 0
        for inp in inputs:
            assert "items" in inp
            assert "config" in inp
            assert "mode" in inp


# =============================================================================
# Coverage Analysis Tests
# =============================================================================


class TestCoverageAnalysis:
    """Test analyze_coverage function."""

    def test_empty_inputs(self):
        """Empty inputs return zero confidence."""
        spec = TaskInputSpec.from_dict({"x": int})
        report = analyze_coverage([], spec)

        assert report.total_inputs == 0
        assert report.confidence == 0.0

    def test_full_boundary_coverage(self):
        """Full boundary coverage increases confidence."""
        spec = TaskInputSpec.from_dict({"flag": bool})
        # Cover both True and False
        inputs = [{"flag": True}, {"flag": False}]
        report = analyze_coverage(inputs, spec)

        assert report.boundary_coverage["flag"] == 1.0

    def test_partial_boundary_coverage(self):
        """Partial coverage is calculated correctly."""
        spec = TaskInputSpec.from_dict({"x": int})
        # Only cover 0, not other boundaries
        inputs = [{"x": 0}]
        report = analyze_coverage(inputs, spec)

        assert 0 < report.boundary_coverage["x"] < 1.0

    def test_unique_combinations_counted(self):
        """Unique combinations are counted."""
        spec = TaskInputSpec.from_dict({"x": int})
        inputs = [
            {"x": 1},
            {"x": 2},
            {"x": 1},  # Duplicate
        ]
        report = analyze_coverage(inputs, spec)

        assert report.total_inputs == 3
        assert report.unique_combinations == 2

    def test_confidence_formula(self):
        """Confidence uses the documented formula."""
        spec = TaskInputSpec.from_dict({"flag": bool})
        # Full boundary coverage (both True and False)
        inputs = [{"flag": True}, {"flag": False}]
        report = analyze_coverage(inputs, spec)

        # boundary_coverage = 1.0
        # strategy_diversity ~ 1.0 (high)
        # input_count = 2/20 = 0.1
        # confidence = 1.0*0.4 + diversity*0.3 + 0.1*0.3
        assert report.confidence > 0.4  # At least boundary weight

    def test_report_str(self):
        """Report has readable string representation."""
        spec = TaskInputSpec.from_dict({"x": int, "y": str})
        inputs = [{"x": 0, "y": "test"}]
        report = analyze_coverage(inputs, spec)

        text = str(report)
        assert "Coverage Report:" in text
        assert "Total inputs:" in text
        assert "Confidence:" in text


# =============================================================================
# Integration Tests
# =============================================================================


class TestIntegration:
    """Test integration with behavioral_grounding."""

    def test_generated_inputs_work_with_grounding(self):
        """Generated inputs can be used with behavioral_grounding."""
        spec = TaskInputSpec.from_dict({"x": int, "y": int})
        generator = InputGenerator(spec, seed=42)
        test_cases = generator.generate_all()

        # Use the generated inputs with behavioral_grounding
        result = behavioral_grounding(
            original_class=SimpleCalculator,
            transformed_class=SimpleCalculator,
            test_cases=test_cases,
            equivalence=EquivalenceLevel.OUTCOME,
        )

        assert result.passed is True
        assert result.match_rate == 1.0

    def test_from_task_class_integration(self):
        """Full integration: task class -> spec -> inputs -> grounding."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)

            @task
            class Adder(BaseModel):
                a: Input(int)
                b: Input(int)
                sum_value: Output(int) = None

                def compute_outputs(self):
                    return {"sum_value": (self.a or 0) + (self.b or 0)}

            spec = TaskInputSpec.from_task_class(Adder)
            generator = InputGenerator(spec, seed=42)
            test_cases = generator.generate_all()

            # Verify we got inputs
            assert len(test_cases) > 0
            assert all("a" in tc and "b" in tc for tc in test_cases)

    def test_coverage_improves_with_more_inputs(self):
        """More inputs improve coverage confidence."""
        spec = TaskInputSpec.from_dict({"x": int, "y": int})
        generator = InputGenerator(spec, seed=42)

        # Few inputs
        few_inputs = generator.generate_from_type(count=2)
        few_report = analyze_coverage(few_inputs, spec)

        # Many inputs
        many_inputs = generator.generate_all()
        many_report = analyze_coverage(many_inputs, spec)

        assert many_report.confidence > few_report.confidence


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_empty_spec(self):
        """Generator works with empty spec."""
        spec = TaskInputSpec(task_name="Empty", fields={})
        generator = InputGenerator(spec)
        inputs = generator.generate_all()

        # Should return empty dicts
        assert len(inputs) > 0
        assert all(inp == {} for inp in inputs)

    def test_single_field(self):
        """Generator works with single field."""
        spec = TaskInputSpec.from_dict({"x": int})
        generator = InputGenerator(spec, seed=42)
        inputs = generator.generate_all()

        assert len(inputs) > 0
        assert all("x" in inp for inp in inputs)

    def test_any_type(self):
        """Any type generates something."""
        value = generate_for_type(Any, "signature")
        assert value is not None or isinstance(value, (int, str, bool))

    def test_nested_generic_types(self):
        """Nested generics are handled."""
        value = generate_for_type(list[list[int]], "signature")
        assert isinstance(value, list)
        if value:
            assert isinstance(value[0], list)

    def test_tuple_type(self):
        """Tuple type is handled."""
        value = generate_for_type(tuple[int, str], "signature")
        assert isinstance(value, tuple)
        assert len(value) == 2

    def test_set_type(self):
        """Set type is handled."""
        value = generate_for_type(set[int], "signature")
        assert isinstance(value, set)

    def test_string_constraints(self):
        """String min/max length constraints work."""
        constraints = {"min_length": 5, "max_length": 10}
        for _ in range(10):
            value = generate_for_type(str, "signature", constraints)
            assert 5 <= len(value) <= 10
