"""Tests for the transform-owned reconstruction facade."""

from __future__ import annotations

import sys

import pytest
from pydantic import BaseModel
from shepherd_runtime.task.authoring import Input, Output, task
from shepherd_transform.source import (
    ReconstructionError,
    ReconstructionResult,
    SourceValidationError,
    extract_task_source,
    reconstruct_task_class,
    try_reconstruct_task_class,
)

SIMPLE_TASK = '''
@task
class SimpleTask(BaseModel):
    """A simple task."""
    query: Input(str)
    answer: Output(str)
'''

IMPORT_OS = """
import os
@task
class MaliciousTask(BaseModel):
    cmd: Input(str)
    result: Output(str)
"""

EVAL_ESCAPE = """
@task
class MaliciousTask(BaseModel):
    code: Input(str)
    result: Output(str)

    def execute(self):
        self.result = eval(self.code)
"""


class TestTryReconstructTaskClass:
    """Test the non-throwing reconstruction wrapper."""

    def test_success_returns_task_class(self) -> None:
        result = try_reconstruct_task_class(SIMPLE_TASK)
        assert result.success is True
        assert result.task_class is not None
        assert result.error is None
        assert result.error_type is None

    def test_security_error_returns_structured_result(self) -> None:
        result = try_reconstruct_task_class(IMPORT_OS)
        assert result.success is False
        assert result.task_class is None
        assert result.error is not None
        assert result.error_type == "SECURITY_ERROR"

    def test_syntax_error_returns_structured_result(self) -> None:
        result = try_reconstruct_task_class("class Foo(BaseModel)  # missing colon")
        assert result.success is False
        assert result.task_class is None
        assert result.error is not None
        assert result.error_type == "SYNTAX_ERROR"

    def test_missing_task_returns_structured_result(self) -> None:
        source = '''
class NotATask(BaseModel):
    """Not decorated with @task."""
    x: int
'''
        result = try_reconstruct_task_class(source)
        assert result.success is False
        assert result.task_class is None
        assert result.error is not None
        assert result.error_type == "MISSING_TASK"


class TestReconstructionResult:
    """Test ReconstructionResult dataclass."""

    def test_success_result(self) -> None:
        result = ReconstructionResult(success=True, task_class=type)
        assert result.success is True
        assert result.task_class is type
        assert result.error is None
        assert result.error_type is None

    def test_error_result(self) -> None:
        result = ReconstructionResult(
            success=False,
            error="Test error",
            error_type="TEST_ERROR",
        )
        assert result.success is False
        assert result.task_class is None
        assert result.error == "Test error"
        assert result.error_type == "TEST_ERROR"


class TestErrorMessages:
    """Test that error messages are helpful."""

    def test_import_error_mentions_module(self) -> None:
        result = try_reconstruct_task_class(IMPORT_OS)
        assert result.error is not None
        assert "os" in result.error.lower() or "import" in result.error.lower()

    def test_security_error_is_actionable(self) -> None:
        result = try_reconstruct_task_class(EVAL_ESCAPE)
        assert result.error is not None
        assert result.error_type == "SECURITY_ERROR"
        assert len(result.error) > 10

    def test_missing_task_error_is_clear(self) -> None:
        result = try_reconstruct_task_class("class Foo(BaseModel): pass")
        assert result.error is not None
        assert "@task" in result.error.lower() or "no" in result.error.lower()


@task
class RoundTripSimpleTask(BaseModel):
    """A simple task for reconstruction round-trips."""

    query: Input(str)
    answer: Output(str)


@task
class TaskWithExecute(BaseModel):
    """A task with custom execute method."""

    x: Input(int)
    doubled: Output(int)

    def execute(self):
        self.doubled = self.x * 2


@task
class TaskWithHelpers(BaseModel):
    """A task with helper methods."""

    numbers: Input(list[int])
    stats: Output(dict[str, float])

    def execute(self):
        self.stats = {
            "sum": self._compute_sum(),
            "mean": self._compute_mean(),
        }

    def _compute_sum(self) -> float:
        return float(sum(self.numbers))

    def _compute_mean(self) -> float:
        return self._compute_sum() / len(self.numbers) if self.numbers else 0.0


class TestReconstructTaskClass:
    """Tests for reconstruct_task_class."""

    def test_round_trip_simple_task(self):
        source = extract_task_source(RoundTripSimpleTask)
        reconstructed = reconstruct_task_class(source)

        assert reconstructed.__name__ == "RoundTripSimpleTask"
        assert hasattr(reconstructed, "_task_meta")
        assert reconstructed._task_source == source
        assert "query" in reconstructed._task_meta.inputs
        assert "answer" in reconstructed._task_meta.outputs

    def test_reconstructed_task_can_be_re_extracted(self):
        source = extract_task_source(RoundTripSimpleTask)
        reconstructed = reconstruct_task_class(source)

        assert extract_task_source(reconstructed) == source

    def test_round_trip_preserves_execute(self):
        source = extract_task_source(TaskWithExecute)
        reconstructed = reconstruct_task_class(source)

        assert hasattr(reconstructed, "execute")
        assert callable(getattr(reconstructed, "execute", None))

    def test_round_trip_preserves_helpers(self):
        source = extract_task_source(TaskWithHelpers)
        reconstructed = reconstruct_task_class(source)

        assert hasattr(reconstructed, "_compute_sum")
        assert hasattr(reconstructed, "_compute_mean")

    def test_reconstruct_with_validation_catches_malicious(self):
        malicious = """
import os
@task
class Evil(BaseModel):
    x: Input(str)
"""
        with pytest.raises(SourceValidationError):
            reconstruct_task_class(malicious, validate=True)

    def test_reconstruct_without_validation_allows_valid_source(self):
        source = """
@task
class TestTask(BaseModel):
    x: Input(str)
    y: Output(str)
"""
        result = reconstruct_task_class(source, validate=True)
        assert result.__name__ == "TestTask"

    def test_missing_task_decorator_raises_error(self):
        source = """
class NotATask(BaseModel):
    x: Input(str)
"""
        with pytest.raises(ReconstructionError) as exc_info:
            reconstruct_task_class(source)

        assert exc_info.value.error_type == "MISSING_TASK_DECORATOR"

    def test_syntax_error_raises_reconstruction_error(self):
        broken = "@task\nclass Foo(BaseModel)\n    x: Input(str)"
        with pytest.raises(ReconstructionError) as exc_info:
            reconstruct_task_class(broken, validate=False)

        assert exc_info.value.error_type in ("SYNTAX_ERROR", "INDENTATION_ERROR")
        assert exc_info.value.recoverable is True

    def test_undefined_name_raises_reconstruction_error(self):
        source = """
@task
class TestTask(BaseModel):
    x: Input(UndefinedType)
"""
        with pytest.raises(ReconstructionError) as exc_info:
            reconstruct_task_class(source, validate=False)

        assert exc_info.value.error_type in ("TYPE_HINT_ERROR", "UNDEFINED_NAME")
        assert exc_info.value.recoverable is True


class TestSyntheticModuleCleanup:
    """Tests for synthetic module cleanup."""

    def test_no_module_leak_on_success(self):
        initial_modules = {k for k in sys.modules if k.startswith("shepherd_reconstructed")}

        source = extract_task_source(RoundTripSimpleTask)
        reconstruct_task_class(source)

        final_modules = {k for k in sys.modules if k.startswith("shepherd_reconstructed")}

        assert initial_modules == final_modules

    def test_no_module_leak_on_failure(self):
        initial_modules = {k for k in sys.modules if k.startswith("shepherd_reconstructed")}

        try:
            broken = "@task\nclass Foo(BaseModel)\n    x: str"
            reconstruct_task_class(broken, validate=False)
        except ReconstructionError:
            pass

        final_modules = {k for k in sys.modules if k.startswith("shepherd_reconstructed")}

        assert initial_modules == final_modules


class TestSourceManipulationIntegration:
    """Integration tests for source manipulation workflow."""

    def test_extract_transform_reconstruct_workflow(self):
        original_source = extract_task_source(RoundTripSimpleTask)
        transformed_source = original_source.replace(
            "answer: Output(str)",
            "answer: Output(str)\n    confidence: Output(float)",
        )

        reconstructed = reconstruct_task_class(transformed_source)

        assert "confidence" in reconstructed._task_meta.outputs
        assert "answer" in reconstructed._task_meta.outputs
        assert "query" in reconstructed._task_meta.inputs
