"""Tests for Infer marker, extract_infer_fields, and build_inference_model."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field
from shepherd_core import Infer, _InferMarker
from shepherd_core._shared.schema import type_to_json_schema
from shepherd_core.autoconfig import build_inference_model, extract_infer_fields
from shepherd_runtime.task.markers import Input, InputMarker

# =============================================================================
# Fixtures
# =============================================================================


class VerifyConfig(BaseModel):
    test_command: str = Field(description="Test command, e.g. 'pytest tests/ -x'")
    build_command: str | None = Field(default=None, description="Optional build step")


class SampleConfig(BaseModel):
    """Config with a mix of Infer and non-Infer fields."""

    guidelines: Annotated[str, Infer] = Field(
        default="",
        description="Repo-specific review standards. Synthesize from CONTRIBUTING.md.",
    )
    focus_areas: Annotated[list[str], Infer] = Field(
        default_factory=lambda: ["correctness", "security"],
        description="Review focus areas derived from repository structure.",
    )
    verify: Annotated[VerifyConfig | None, Infer] = Field(
        default=None,
        description="Build/test verification config, or null to skip.",
    )
    max_comments: int = Field(default=5, ge=1)
    repo: str | None = None


class QGConfig(BaseModel):
    mode: Annotated[Literal["fast", "standard", "full"], Infer] = Field(default="full", description="Pipeline mode")
    test_paths: Annotated[list[str], Infer] = Field(default_factory=list, description="pytest paths")
    workspace_path: str = "."


# =============================================================================
# Infer sentinel and InputMarker
# =============================================================================


class TestInferSentinel:
    def test_repr(self) -> None:
        assert repr(Infer) == "Infer"

    def test_isinstance(self) -> None:
        assert isinstance(Infer, _InferMarker)

    def test_input_marker_default_no_infer(self) -> None:
        m = InputMarker()
        assert m.infer is False

    def test_input_marker_infer_true(self) -> None:
        m = InputMarker(infer=True)
        assert m.infer is True

    def test_input_function_infer_kwarg(self) -> None:
        hint = Input(str, infer=True)
        args = hint.__args__  # type: ignore[union-attr]
        metadata = hint.__metadata__  # type: ignore[union-attr]
        assert args[0] is str
        marker = metadata[0]
        assert isinstance(marker, InputMarker)
        assert marker.infer is True

    def test_input_function_backward_compat(self) -> None:
        hint = Input(str)
        metadata = hint.__metadata__  # type: ignore[union-attr]
        marker = metadata[0]
        assert isinstance(marker, InputMarker)
        assert marker.infer is False


# =============================================================================
# extract_infer_fields
# =============================================================================


class TestExtractInferFields:
    def test_returns_inferable_fields(self) -> None:
        fields = extract_infer_fields(SampleConfig)
        assert set(fields.keys()) == {"guidelines", "focus_areas", "verify"}

    def test_excludes_non_inferable(self) -> None:
        fields = extract_infer_fields(SampleConfig)
        assert "max_comments" not in fields
        assert "repo" not in fields

    def test_descriptions_preserved(self) -> None:
        fields = extract_infer_fields(SampleConfig)
        assert "CONTRIBUTING" in fields["guidelines"]["description"]

    def test_defaults_preserved(self) -> None:
        fields = extract_infer_fields(SampleConfig)
        assert fields["guidelines"]["default"] == ""
        assert fields["verify"]["default"] is None
        assert fields["focus_areas"]["has_default_factory"] is True
        assert fields["focus_areas"]["default"] == ["correctness", "security"]

    def test_empty_on_no_infer_fields(self) -> None:
        class Plain(BaseModel):
            x: int = 1

        assert extract_infer_fields(Plain) == {}

    def test_input_infer_true_on_task_class(self) -> None:
        """Input(str, infer=True) on a @task-like class is detected."""
        from shepherd_runtime.task.decorator import task

        @task
        class MyTask(BaseModel):
            query: Input(str, infer=True) = Field(default="", description="search query")
            result: str = ""

        fields = extract_infer_fields(MyTask)
        assert "query" in fields
        assert fields["query"]["description"] == "search query"


# =============================================================================
# build_inference_model
# =============================================================================


class TestBuildInferenceModel:
    def test_creates_valid_model(self) -> None:
        model = build_inference_model(SampleConfig)
        assert issubclass(model, BaseModel)

    def test_only_infer_fields(self) -> None:
        model = build_inference_model(SampleConfig)
        assert set(model.model_fields.keys()) == {"guidelines", "focus_areas", "verify"}

    def test_schema_valid(self) -> None:
        model = build_inference_model(SampleConfig)
        schema = type_to_json_schema(model)
        assert schema["type"] == "object"
        assert "properties" in schema

    def test_defs_for_nested_model(self) -> None:
        model = build_inference_model(SampleConfig)
        schema = type_to_json_schema(model)
        assert "$defs" in schema
        assert "VerifyConfig" in schema["$defs"]

    def test_no_infrastructure_in_schema(self) -> None:
        model = build_inference_model(SampleConfig)
        schema = type_to_json_schema(model)
        props = schema["properties"]
        assert "max_comments" not in props
        assert "repo" not in props

    def test_descriptions_in_schema(self) -> None:
        model = build_inference_model(SampleConfig)
        schema = type_to_json_schema(model)
        assert "description" in schema["properties"]["guidelines"]

    def test_literal_type(self) -> None:
        model = build_inference_model(QGConfig)
        schema = type_to_json_schema(model)
        assert "enum" in schema["properties"]["mode"]
        assert set(schema["properties"]["mode"]["enum"]) == {"fast", "standard", "full"}

    def test_no_defs_when_no_nested(self) -> None:
        model = build_inference_model(QGConfig)
        schema = type_to_json_schema(model)
        assert "$defs" not in schema

    def test_round_trip(self) -> None:
        model = build_inference_model(SampleConfig)
        instance = model.model_validate(
            {
                "guidelines": "Be strict",
                "focus_areas": ["correctness"],
                "verify": None,
            }
        )
        dumped = instance.model_dump()
        config = SampleConfig(**dumped)
        assert config.guidelines == "Be strict"
        assert config.max_comments == 5  # default preserved
