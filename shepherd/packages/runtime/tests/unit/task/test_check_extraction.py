"""Unit tests for Check extraction into TaskMetadata."""

from typing import Annotated

from pydantic import BaseModel
from shepherd_runtime.task.authoring import Check, FileExists, Input, InRange, MaxLength, NonEmpty, Output, task
from shepherd_runtime.task.metadata import _extract_checks, extract_task_metadata

# =============================================================================
# _extract_checks helper
# =============================================================================


class TestExtractChecks:
    def test_returns_empty_for_no_metadata(self):
        assert _extract_checks(int) == []

    def test_returns_empty_when_no_checks(self):
        hint = Input(str)
        # Input(str) produces Annotated[str, InputMarker()] — no Check
        assert _extract_checks(hint) == []

    def test_extracts_single_check(self):
        c = NonEmpty()
        hint = Annotated[Input(str), c]
        checks = _extract_checks(hint)
        assert len(checks) == 1
        assert checks[0] is c

    def test_extracts_multiple_checks(self):
        c1 = NonEmpty()
        c2 = MaxLength(100)
        hint = Annotated[Input(str), c1, c2]
        checks = _extract_checks(hint)
        assert len(checks) == 2
        assert checks[0] is c1
        assert checks[1] is c2


# =============================================================================
# extract_task_metadata integration
# =============================================================================


class TestMetadataExtraction:
    def test_input_checks_extracted(self):
        @task
        class T(BaseModel):
            """Test."""

            path: Annotated[Input(str), FileExists()]
            answer: Output(str)

        meta = extract_task_metadata(T)
        assert "path" in meta.input_checks
        assert len(meta.input_checks["path"]) == 1
        assert isinstance(meta.input_checks["path"][0], Check)

    def test_output_checks_extracted(self):
        @task
        class T(BaseModel):
            """Test."""

            query: Input(str)
            result: Annotated[Output(str), NonEmpty()]

        meta = extract_task_metadata(T)
        assert "result" in meta.output_checks
        assert len(meta.output_checks["result"]) == 1

    def test_no_checks_means_no_entry(self):
        @task
        class T(BaseModel):
            """Test."""

            query: Input(str)
            answer: Output(str)

        meta = extract_task_metadata(T)
        assert meta.input_checks == {}
        assert meta.output_checks == {}

    def test_multiple_checks_per_field(self):
        @task
        class T(BaseModel):
            """Test."""

            score: Annotated[Output(float), InRange(0.0, 1.0), NonEmpty()]

        meta = extract_task_metadata(T)
        assert "score" in meta.output_checks
        assert len(meta.output_checks["score"]) == 2

    def test_checks_dont_interfere_with_primary_markers(self):
        @task
        class T(BaseModel):
            """Test."""

            path: Annotated[Input(str), FileExists()]
            result: Annotated[Output(str), NonEmpty()]

        meta = extract_task_metadata(T)
        # Primary markers still extracted correctly
        assert "path" in meta.inputs
        assert meta.inputs["path"].marker_type == "input"
        assert "result" in meta.outputs
        assert meta.outputs["result"].marker_type == "output"

    def test_mixed_fields_with_and_without_checks(self):
        @task
        class T(BaseModel):
            """Test."""

            path: Annotated[Input(str), FileExists()]
            query: Input(str)
            result: Output(str)

        meta = extract_task_metadata(T)
        assert "path" in meta.input_checks
        assert "query" not in meta.input_checks
        assert meta.output_checks == {}
