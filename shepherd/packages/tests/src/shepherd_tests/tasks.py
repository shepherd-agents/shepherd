"""Task factories for testing @task and @step decorated classes.

This module provides factory functions to generate task classes dynamically,
eliminating copy-paste boilerplate in tests.

IMPORTANT: This module must NOT use `from __future__ import annotations`.
Dynamic class generation requires runtime type evaluation. PEP 563's
stringified annotations break the @task decorator's type introspection.

Example:
    from shepherd_tests.tasks import make_step_task, RETURN_TYPE_TEST_CASES
    from shepherd_tests import mock_steps

    # Create a task with specific return type
    Task = make_step_task(Literal["yes", "no"])

    with mock_steps():
        t = Task(input_val="test")
        assert t.output_val == "yes"  # First literal value

    # Use in parameterized tests
    @pytest.mark.parametrize("case", RETURN_TYPE_TEST_CASES, ids=lambda c: c.id)
    def test_return_types(self, case):
        Task = make_step_task(case.return_type)
        with mock_steps():
            t = Task(input_val="test")
            # ... assertions
"""

# NOTE: No `from __future__ import annotations` — intentional!
# See module docstring for explanation.

from enum import Enum
from typing import Literal, NamedTuple, TypeVar

from pydantic import BaseModel
from shepherd_runtime.step.api import step
from shepherd_runtime.task.authoring import Input, Output, task

T = TypeVar("T")


# =============================================================================
# Factory Function
# =============================================================================


def make_step_task(return_type: type[T]) -> type[BaseModel]:
    """Create a @task class with a @step method of the specified return type.

    This factory eliminates the need to define identical task structures
    in each test. It creates a task with:
    - input_val: Input(str)
    - output_val: Output(<normalized return_type>)
    - process(): A @step method with the specified return type

    Args:
        return_type: The return type annotation for the step method.

    Returns:
        A @task decorated class ready for testing.

    Raises:
        TypeError: If return_type is None.

    Example:
        Task = make_step_task(Literal["yes", "no"])
        with mock_steps():
            t = Task(input_val="test")
            assert t.output_val == "yes"
    """
    if return_type is None:
        raise TypeError("return_type cannot be None")

    # Normalize output type (Literal -> str, Enum -> str, etc.)
    out_type = _normalize_output_type(return_type)

    @task
    class GeneratedTask(BaseModel):
        input_val: Input(str)
        output_val: Output(out_type)

        def execute(self) -> None:
            self.output_val = self.process(self.input_val)

    # Create and attach the step method
    def process(self, text: str) -> None:
        """Process the input."""

    process.__annotations__ = {"text": str, "return": return_type}
    decorated = step()(process)
    GeneratedTask.process = decorated

    # Set descriptive name for debugging
    GeneratedTask.__name__ = f"StepTask[{_type_name(return_type)}]"
    GeneratedTask.__qualname__ = GeneratedTask.__name__

    return GeneratedTask


# =============================================================================
# Inline Step Factory
# =============================================================================


def make_inline_step_task(
    return_type: type[T],
    prompt_template: str = "Process: {val}",
) -> type[BaseModel]:
    """Create a @task class using self.step[T]() inline syntax.

    This factory creates tasks that use the inline step syntax instead of
    decorated @step methods. Useful for testing self.step[T]() behavior.

    Args:
        return_type: The type parameter for self.step[T]().
        prompt_template: The prompt template with {val} placeholder.
            Default: "Process: {val}"

    Returns:
        A @task decorated class ready for testing.

    Raises:
        TypeError: If return_type is None.

    Example:
        Task = make_inline_step_task(Literal["yes", "no"], "Is {val} good?")
        with mock_steps():
            t = Task(input_val="test")
            assert t.output_val == "yes"
    """
    if return_type is None:
        raise TypeError("return_type cannot be None")

    out_type = _normalize_output_type(return_type)

    # We need to capture return_type in a closure for the execute method.
    # Can't use a default argument because Pydantic inspects the signature.
    captured_return_type = return_type
    captured_template = prompt_template

    @task
    class GeneratedTask(BaseModel):
        input_val: Input(str)
        output_val: Output(out_type)

        def execute(self) -> None:
            self.output_val = self.step[captured_return_type](captured_template, val=self.input_val)

    GeneratedTask.__name__ = f"InlineStepTask[{_type_name(return_type)}]"
    GeneratedTask.__qualname__ = GeneratedTask.__name__

    return GeneratedTask


# =============================================================================
# Internal Helpers
# =============================================================================


def _normalize_output_type(return_type: type) -> type:
    """Convert step return type to appropriate Output field type.

    Handles:
    - Literal["a", "b"] -> str
    - Enum subclass -> str
    - list[T] -> list
    - dict[K, V] -> dict
    - Everything else -> pass through
    """
    origin = getattr(return_type, "__origin__", None)

    if origin is Literal:
        return str
    if isinstance(return_type, type) and issubclass(return_type, Enum):
        return str
    if origin is list:
        return list
    if origin is dict:
        return dict

    return return_type


def _type_name(t: type) -> str:
    """Get a readable name for a type (used in generated class names)."""
    # Check for generic types first (Literal, list[T], etc.)
    # In Python 3.12+, Literal['a', 'b'] has __name__ = 'Literal', so we
    # need to check __origin__ before __name__ to get the full representation.
    origin = getattr(t, "__origin__", None)
    if origin is Literal:
        args = getattr(t, "__args__", ())
        if len(args) <= 2:
            return f"Literal[{', '.join(repr(a) for a in args)}]"
        return f"Literal[{args[0]!r}, ...]"

    # For simple named types (str, int, custom classes)
    if hasattr(t, "__name__"):
        return t.__name__

    return repr(t)


# =============================================================================
# Test Case Data
# =============================================================================


class StepReturnTypeCase(NamedTuple):
    """Test case for @step return type parameterization.

    Attributes:
        return_type: The type to use as the step's return annotation.
        expected: The expected mock value (or substring for partial match).
        partial_match: If True, check `expected in str(actual)` instead of equality.
        id: Pytest test ID for readable output.
    """

    return_type: type
    expected: object
    partial_match: bool
    id: str


# Standard test cases for @step return type handling.
#
# Coverage notes:
# - literal, string, int, bool, list: Refactored from existing tests
# - float, dict: NEW coverage (not previously tested in test_step.py)
#
# Excluded types (kept as inline tests):
# - Enum: Original test uses explicit `.value` coercion in execute().
#         Factory pattern works (Severity.LOW == "low" via str.__eq__),
#         but stored value differs (enum vs string). Keep inline for clarity.
# - Pydantic models: Require custom model definitions with specific fields.
#
RETURN_TYPE_TEST_CASES: list[StepReturnTypeCase] = [
    # Refactored from existing tests:
    StepReturnTypeCase(Literal["alpha", "beta", "gamma"], "alpha", False, "literal"),
    StepReturnTypeCase(str, "[mock", True, "string"),
    StepReturnTypeCase(int, 0, False, "int"),
    StepReturnTypeCase(bool, True, False, "bool"),
    StepReturnTypeCase(list[str], [], False, "list"),
    # NEW coverage:
    StepReturnTypeCase(float, 0.0, False, "float"),
    StepReturnTypeCase(dict[str, int], {}, False, "dict"),
]


class InlineStepCase(NamedTuple):
    """Test case for self.step[T]() inline syntax parameterization.

    Attributes:
        return_type: The type parameter for self.step[T]().
        prompt_template: The prompt template to use.
        expected: The expected mock value (or substring for partial match).
        partial_match: If True, check `expected in str(actual)` instead of equality.
        id: Pytest test ID for readable output.
    """

    return_type: type
    prompt_template: str
    expected: object
    partial_match: bool
    id: str


# Test cases for self.step[T]() inline syntax.
#
# Coverage notes:
# - literal, string: Refactored from existing tests
#
# Excluded (kept as inline tests):
# - Multiple placeholders: Tests unique template behavior
# - Custom timeout: Tests timeout parameter passing
#
INLINE_STEP_TEST_CASES: list[InlineStepCase] = [
    InlineStepCase(
        Literal["yes", "no"],
        "Is {val} good?",
        ("yes", "no"),  # Expected to be one of these values
        False,
        "literal",
    ),
    InlineStepCase(
        str,
        "Summarize: {val}",
        "[mock",
        True,  # Partial match
        "string",
    ),
]


__all__ = [
    "INLINE_STEP_TEST_CASES",
    # Test case data
    "RETURN_TYPE_TEST_CASES",
    "InlineStepCase",
    # Test case types
    "StepReturnTypeCase",
    "make_inline_step_task",
    # Factories
    "make_step_task",
]
