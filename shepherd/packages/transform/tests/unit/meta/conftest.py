"""Shared fixtures for meta-task tests."""

from __future__ import annotations

from typing import Annotated

import pytest
from pydantic import BaseModel, Field
from shepherd_runtime.scope import Scope
from shepherd_runtime.task.authoring import Input, Output, task
from shepherd_tests import MockProvider


@pytest.fixture(autouse=True)
def mock_task_execution():
    """Disable auto-execution for all meta-task tests."""
    with Scope(root=True) as scope:
        scope.register_provider("default", MockProvider(), default=True)
        yield


# =============================================================================
# Sample Tasks for Testing
# =============================================================================


@task
class SimpleCalculator(BaseModel):
    """A simple calculator that adds two numbers."""

    a: Annotated[Input(int), Field(description="First operand")]
    b: Annotated[Input(int), Field(description="Second operand")]

    result: Annotated[Output(int), Field(description="Sum of a and b")]


@task
class TextProcessor(BaseModel):
    """Process text with various options."""

    text: Annotated[Input(str), Field(description="Input text")]
    uppercase: Annotated[Input(bool), Field(default=False, description="Convert to uppercase")]

    processed: Annotated[Output(str), Field(description="Processed text")]


@task
class TaskWithIssues(BaseModel):
    """A task with some design issues for critique testing.

    This task intentionally has issues:
    - Vague field names (x, y)
    - Missing descriptions
    - No docstring detail
    """

    x: Input(str)  # Vague name, no description
    y: Input(int)  # Vague name, no description

    out: Output(str)  # Vague name


@task
class WellDesignedTask(BaseModel):
    """Search for documents matching a query.

    This task searches a document collection using semantic search
    and returns the top matching documents along with relevance scores.

    Example:
        result = await scope.execute(WellDesignedTask(
            query="machine learning papers",
            max_results=5,
        ))
    """

    query: Annotated[
        Input(str),
        Field(description="Search query text", min_length=1),
    ]
    max_results: Annotated[
        Input(int),
        Field(default=10, description="Maximum number of results to return", ge=1, le=100),
    ]

    documents: Annotated[
        Output(list[str]),
        Field(description="List of matching document titles"),
    ]
    relevance_scores: Annotated[
        Output(list[float]),
        Field(description="Relevance scores for each document (0-1)"),
    ]


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def simple_calculator():
    """Return the SimpleCalculator task class."""
    return SimpleCalculator


@pytest.fixture
def completed_calculator():
    """Return a completed SimpleCalculator instance."""
    return SimpleCalculator(a=5, b=3)


@pytest.fixture
def completed_text_processor():
    """Return a completed TextProcessor instance."""
    return TextProcessor(text="hello world")


@pytest.fixture
def text_processor():
    """Return the TextProcessor task class."""
    return TextProcessor


@pytest.fixture
def task_with_issues():
    """Return a task with design issues."""
    return TaskWithIssues


@pytest.fixture
def well_designed_task():
    """Return a well-designed task."""
    return WellDesignedTask


@pytest.fixture
def sample_effect_stream():
    """Return a sample markdown effect stream."""
    return """# Effect Stream

## Task: SimpleCalculator
- Started: 2026-01-30T10:00:00
- Completed: 2026-01-30T10:00:01

### Inputs
- a: 5
- b: 3

### Outputs
- result: 8

### Effects
1. TaskStarted(task_name="SimpleCalculator")
2. InputProvided(name="a", value=5)
3. InputProvided(name="b", value=3)
4. OutputProduced(name="result", value=8)
5. TaskCompleted(task_name="SimpleCalculator")
"""


@pytest.fixture
def multiple_effect_streams():
    """Return multiple effect streams showing different executions."""
    return [
        """# Execution 1: Small inputs
- a: 1, b: 2 -> result: 3
- Duration: 50ms
""",
        """# Execution 2: Large inputs
- a: 999999, b: 1 -> result: 1000000
- Duration: 51ms
""",
        """# Execution 3: Negative inputs
- a: -5, b: 10 -> result: 5
- Duration: 49ms
""",
    ]
