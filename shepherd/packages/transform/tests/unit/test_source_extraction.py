"""Tests for transform-owned source extraction helpers."""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from shepherd_runtime.task.authoring import Input, Output, task
from shepherd_transform.source import (
    SourceExtractionError,
    extract_task_imports,
    extract_task_source,
    extract_task_with_imports,
)


@task
class SimpleTask(BaseModel):
    """A simple task for testing."""

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


class TestExtractTaskSource:
    """Tests for extract_task_source."""

    def test_extract_simple_task(self):
        source = extract_task_source(SimpleTask)

        assert "@task" in source
        assert "class SimpleTask" in source
        assert "query: Input(str)" in source
        assert "answer: Output(str)" in source

    def test_extract_task_with_execute(self):
        source = extract_task_source(TaskWithExecute)

        assert "def execute(self):" in source
        assert "self.doubled = self.x * 2" in source

    def test_extract_task_with_helpers(self):
        source = extract_task_source(TaskWithHelpers)

        assert "def _compute_sum" in source
        assert "def _compute_mean" in source

    def test_file_defined_task_carries_stored_source(self):
        source = extract_task_source(SimpleTask)

        assert getattr(SimpleTask, "_task_source", None) == source

    def test_extract_non_task_raises_error(self):
        class NotATask(BaseModel):
            x: str

        with pytest.raises(SourceExtractionError) as exc_info:
            extract_task_source(NotATask)

        assert "not decorated with @task" in str(exc_info.value)


class TestExtractTaskImports:
    """Tests for extract_task_imports."""

    def test_extract_imports_from_this_file(self):
        imports = extract_task_imports(SimpleTask)

        assert len(imports) > 0
        import_text = "\n".join(imports)
        assert "pytest" in import_text or "pydantic" in import_text


class TestExtractTaskWithImports:
    """Tests for extract_task_with_imports."""

    def test_extract_both_source_and_imports(self):
        source, imports = extract_task_with_imports(SimpleTask)

        assert "@task" in source
        assert "class SimpleTask" in source
        assert isinstance(imports, list)
