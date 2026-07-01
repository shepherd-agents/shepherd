"""Unit tests for runtime TaskRef source handling."""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from shepherd_core.errors import TaskRefOutputError
from shepherd_core.types import ExecutionResult
from shepherd_runtime.scope import Scope
from shepherd_runtime.task.authoring import Input, Output, TaskRef, task
from shepherd_runtime.task.metadata import extract_task_metadata
from shepherd_runtime.task.output import TaskRefReconstructionPolicy, extract_outputs
from shepherd_tests import MockProvider


@task
class TaskRefOutputTask(BaseModel):
    """Task used to test TaskRef output extraction."""

    transformed: Output(TaskRef)


class TestTaskRefOutputExtraction:
    """Tests for extracting TaskRef outputs from structured provider results."""

    def test_extract_outputs_reconstructs_taskref_source(self):
        """A string TaskRef output is reconstructed to a task class."""
        meta = extract_task_metadata(TaskRefOutputTask)
        source = """
@task
class ReconstructedTask(BaseModel):
    query: Input(str)
    answer: Output(str)
"""
        result = ExecutionResult(structured_output={"transformed": source})

        outputs = extract_outputs(meta, result)

        transformed = outputs["transformed"]
        assert transformed is not None
        assert transformed.__name__ == "ReconstructedTask"
        assert getattr(transformed, "_task_source", None) == source

    def test_extract_outputs_rejects_non_string_taskref(self):
        """TaskRef outputs must be raw Python source strings."""
        meta = extract_task_metadata(TaskRefOutputTask)
        result = ExecutionResult(structured_output={"transformed": {"source": "not a string"}})

        with pytest.raises(TaskRefOutputError, match="expected a raw Python source string"):
            extract_outputs(meta, result)

    def test_extract_outputs_allowlisted_policy_reconstructs_domain_imports(self, tmp_path, monkeypatch):
        """Allowlisted policy should permit domain imports during TaskRef reconstruction."""
        domain_module = tmp_path / "my_domain.py"
        domain_module.write_text("Alias = str\n", encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))

        meta = extract_task_metadata(TaskRefOutputTask)
        source = "from my_domain import Alias\n@task\nclass DomainTask(BaseModel):\n    query: Input(Alias)\n    answer: Output(str)"
        result = ExecutionResult(structured_output={"transformed": source})

        outputs = extract_outputs(
            meta,
            result,
            taskref_policy=TaskRefReconstructionPolicy.allowlisted("my_domain"),
        )

        transformed = outputs["transformed"]
        assert transformed.__name__ == "DomainTask"
        assert getattr(transformed, "_task_source", None) == source

    @pytest.mark.asyncio
    async def test_arun_threads_taskref_policy_to_output_extraction(self, tmp_path, monkeypatch):
        """Task.arun should pass explicit TaskRef policy through lifecycle extraction."""
        domain_module = tmp_path / "my_domain.py"
        domain_module.write_text("Alias = str\n", encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        source = "from my_domain import Alias\n@task\nclass DomainTask(BaseModel):\n    query: Input(Alias)\n    answer: Output(str)"
        provider = MockProvider(
            mock_responses=[
                {
                    "structured": {"transformed": source},
                }
            ]
        )

        with Scope(root=True) as scope:
            scope.register_provider("default", provider, default=True)
            transformed = await TaskRefOutputTask.arun(
                scope=scope,
                taskref_policy=TaskRefReconstructionPolicy.allowlisted("my_domain"),
            )

        assert transformed.transformed.__name__ == "DomainTask"
        assert getattr(transformed.transformed, "_task_source", None) == source

    def test_extract_outputs_raises_for_invalid_task_source(self):
        """Invalid TaskRef source raises an explicit TaskRef output error."""
        meta = extract_task_metadata(TaskRefOutputTask)
        result = ExecutionResult(structured_output={"transformed": "def broken("})

        with pytest.raises(TaskRefOutputError, match="Syntax error"):
            extract_outputs(meta, result)


# =============================================================================
# TaskRef Type Tests
# =============================================================================


class TestTaskRefType:
    """Tests for the TaskRef type."""

    def test_taskref_with_input(self):
        """Test using Input(TaskRef) in a task definition."""

        @task
        class MetaTask(BaseModel):
            """A meta-task that operates on another task."""

            target: Input(TaskRef)
            instruction: Input(str)
            result: Output(str)

        # Should have target as an input field
        assert "target" in MetaTask.model_fields
        assert "target" in MetaTask._task_meta.inputs
        assert MetaTask._task_meta.inputs["target"].inner_type is TaskRef

    def test_taskref_with_output(self):
        """Test using Output(TaskRef) in a task definition."""
        import types
        from typing import Union, get_args, get_origin

        @task
        class TransformTask(BaseModel):
            """A task that transforms another task."""

            target: Input(TaskRef)
            transformed: Output(TaskRef)

        # Should have transformed as an output field
        assert "transformed" in TransformTask.model_fields
        assert "transformed" in TransformTask._task_meta.outputs
        # Output types include | None, so check if TaskRef is in the union
        inner = TransformTask._task_meta.outputs["transformed"].inner_type
        origin = get_origin(inner)
        if origin in (Union, types.UnionType):
            args = get_args(inner)
            assert TaskRef in args
        else:
            assert inner is TaskRef
