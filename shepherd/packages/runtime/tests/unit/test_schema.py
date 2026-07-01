"""Tests for runtime output schema generation."""

import json
from datetime import datetime
from typing import Union, get_args, get_origin
from uuid import UUID

import pytest
from pydantic import BaseModel
from shepherd_core.errors import SchemaGenerationError
from shepherd_core.schema import type_to_json_schema
from shepherd_runtime.step.output import return_type_to_output_schema


def _normalize_schema(schema: dict) -> str:
    """Normalize schema for comparison (remove noise, sort keys)."""

    def _strip_titles(value):
        if isinstance(value, dict):
            return {k: _strip_titles(v) for k, v in value.items() if k != "title"}
        if isinstance(value, list):
            return [_strip_titles(item) for item in value]
        return value

    return json.dumps(_strip_titles(schema), sort_keys=True)


class TestStepTypeCoverage:
    """Tests for @step return type schema generation."""

    def test_step_with_datetime_return(self):
        schema = return_type_to_output_schema(datetime)
        result_schema = schema["schema"]["properties"]["result"]
        assert result_schema.get("type") == "string"
        assert result_schema.get("format") == "date-time"

    def test_step_with_uuid_return(self):
        schema = return_type_to_output_schema(UUID)
        result_schema = schema["schema"]["properties"]["result"]
        assert result_schema.get("type") == "string"
        assert result_schema.get("format") == "uuid"

    def test_step_with_set_return(self):
        schema = return_type_to_output_schema(set[str])
        result_schema = schema["schema"]["properties"]["result"]
        assert result_schema.get("type") == "array"
        assert result_schema.get("uniqueItems") is True

    def test_step_with_tuple_return(self):
        schema = return_type_to_output_schema(tuple[str, int])
        props = schema["schema"]["properties"]
        assert "output_0" in props
        assert "output_1" in props
        assert props["output_0"].get("type") == "string"
        assert props["output_1"].get("type") == "integer"


class TestDefsHandling:
    """Tests for $defs hoisting and conflict detection."""

    def test_step_with_pydantic_model_return(self):
        class Result(BaseModel):
            value: str

        schema = return_type_to_output_schema(Result)
        result_schema = schema["schema"]["properties"]["result"]
        assert "properties" in result_schema or "$ref" in result_schema

    def test_step_with_list_of_models_has_defs(self):
        class Item(BaseModel):
            name: str

        schema = return_type_to_output_schema(list[Item])
        result_schema = schema["schema"]["properties"]["result"]

        has_defs_at_root = "$defs" in schema["schema"]
        has_items_key = "items" in result_schema

        assert has_items_key, "list schema should have 'items' key"
        if "$ref" in result_schema.get("items", {}):
            assert has_defs_at_root, "$defs should be hoisted to schema root"

    def test_step_with_tuple_of_models_merges_defs(self):
        class A(BaseModel):
            x: str

        class B(BaseModel):
            y: int

        schema = return_type_to_output_schema(tuple[A, B])
        props = schema["schema"]["properties"]
        defs = schema["schema"].get("$defs", {})

        assert "output_0" in props
        assert "output_1" in props

        for key in ["output_0", "output_1"]:
            if "$ref" in props[key]:
                ref_name = props[key]["$ref"].split("/")[-1]
                assert ref_name in defs, f"$defs should contain {ref_name}"

    def test_step_with_tuple_conflicting_defs_raises(self):
        class ModelA(BaseModel):
            class Item(BaseModel):
                text: str

            items: list[Item]

        class ModelB(BaseModel):
            class Item(BaseModel):
                score: int

            items: list[Item]

        with pytest.raises(SchemaGenerationError, match=r"Conflicting.*Item"):
            return_type_to_output_schema(tuple[ModelA, ModelB])


class TestConsistency:
    """Tests for consistency between @task and @step schema generation."""

    def test_task_and_step_produce_consistent_model_schema(self):
        class Result(BaseModel):
            value: str
            count: int

        step_schema = return_type_to_output_schema(Result)
        step_result = step_schema["schema"]["properties"]["result"]

        task_schema = type_to_json_schema(Result)

        assert _normalize_schema(step_result) == _normalize_schema(task_schema)

    def test_task_and_step_produce_consistent_list_schema(self):
        class Item(BaseModel):
            id: int
            name: str

        step_schema = return_type_to_output_schema(list[Item])
        step_result = step_schema["schema"]["properties"]["result"]
        step_defs = step_schema["schema"].get("$defs", {})

        task_schema = type_to_json_schema(list[Item])
        task_defs = task_schema.pop("$defs", {})

        assert _normalize_schema(step_result) == _normalize_schema(task_schema)
        assert _normalize_schema(step_defs) == _normalize_schema(task_defs)


class TestOptionalHandling:
    """Tests for Optional handling differences between @task and @step."""

    def test_step_optional_return_allows_null(self):
        schema = return_type_to_output_schema(str | None)
        result_schema = schema["schema"]["properties"]["result"]

        allows_null = "anyOf" in result_schema and any(s.get("type") == "null" for s in result_schema["anyOf"])

        assert allows_null, "Optional[T] return should allow null in @step schema"

    def test_task_output_does_not_allow_null(self):
        from shepherd_runtime.task.authoring import Output, task
        from shepherd_runtime.task.metadata import extract_task_metadata
        from shepherd_runtime.task.output import generate_output_schema

        @task
        class TestTask(BaseModel):
            """Test task."""

            result: Output(str)

        meta = extract_task_metadata(TestTask)
        schema = generate_output_schema(meta)
        result_schema = schema["schema"]["properties"]["result"]

        has_null = "anyOf" in result_schema and any(s.get("type") == "null" for s in result_schema.get("anyOf", []))
        assert not has_null, "Output(T) should not allow null in @task schema"

    def test_task_output_multi_type_union_no_null_in_schema(self):
        from shepherd_runtime.task.authoring import Output, task
        from shepherd_runtime.task.metadata import extract_task_metadata
        from shepherd_runtime.task.output import generate_output_schema

        @task
        class TaskMultiUnion(BaseModel):
            """Task with multi-type union output."""

            result: Output(str | int | None)

        meta = extract_task_metadata(TaskMultiUnion)
        schema = generate_output_schema(meta)
        result_schema = schema["schema"]["properties"]["result"]

        if "anyOf" in result_schema:
            type_names = [s.get("type") for s in result_schema["anyOf"]]
            assert "null" not in type_names, "Multi-type union output should not allow null"
        else:
            assert result_schema.get("type") != "null"


class TestComplexTypes:
    """Tests for deeply nested and complex type structures."""

    def test_nested_generics(self):
        class Item(BaseModel):
            value: str

        schema = return_type_to_output_schema(dict[str, list[Item]])
        result_schema = schema["schema"]["properties"]["result"]

        assert result_schema.get("type") == "object"
        if "$defs" in schema["schema"]:
            assert "Item" in schema["schema"]["$defs"]

    def test_self_referential_model(self):
        class TreeNode(BaseModel):
            value: str
            children: list["TreeNode"] = []

        TreeNode.model_rebuild()

        schema = return_type_to_output_schema(TreeNode)
        assert "$defs" in schema["schema"] or "properties" in schema["schema"]["properties"]["result"]


class TestTaskDefsConflictDetection:
    """Tests for $defs conflict detection in @task output schema generation."""

    def test_task_with_conflicting_output_defs_raises(self):
        from shepherd_runtime.task.authoring import Output, task
        from shepherd_runtime.task.metadata import extract_task_metadata
        from shepherd_runtime.task.output import generate_output_schema

        class ModelA(BaseModel):
            class Nested(BaseModel):
                text: str

            items: list[Nested]

        class ModelB(BaseModel):
            class Nested(BaseModel):
                score: int
                value: float

            items: list[Nested]

        @task
        class TaskWithConflict(BaseModel):
            """Task with conflicting nested class names."""

            output_a: Output(ModelA)
            output_b: Output(ModelB)

        meta = extract_task_metadata(TaskWithConflict)

        with pytest.raises(SchemaGenerationError, match=r"Conflicting.*Nested"):
            generate_output_schema(meta)

    def test_task_with_same_nested_class_no_conflict(self):
        from shepherd_runtime.task.authoring import Output, task
        from shepherd_runtime.task.metadata import extract_task_metadata
        from shepherd_runtime.task.output import generate_output_schema

        class SharedNested(BaseModel):
            value: str

        class ModelA(BaseModel):
            items: list[SharedNested]

        class ModelB(BaseModel):
            data: list[SharedNested]

        @task
        class TaskNoConflict(BaseModel):
            """Task with shared nested class."""

            output_a: Output(ModelA)
            output_b: Output(ModelB)

        meta = extract_task_metadata(TaskNoConflict)
        schema = generate_output_schema(meta)
        assert schema is not None


class TestStripNoneFromType:
    """Tests for _strip_none_from_type() handling of complex unions."""

    def test_strip_none_from_binary_union(self):
        from shepherd_runtime.task.metadata import _strip_none_from_type

        result = _strip_none_from_type(str | None)
        assert result is str

    def test_strip_none_from_multi_type_union(self):
        from shepherd_runtime.task.metadata import _strip_none_from_type

        result = _strip_none_from_type(str | int | None)

        origin = get_origin(result)
        assert origin is Union

        args = set(get_args(result))
        assert args == {str, int}
        assert type(None) not in args

    def test_strip_none_from_optional_list(self):
        from shepherd_runtime.task.metadata import _strip_none_from_type

        result = _strip_none_from_type(list[str] | None)

        assert get_origin(result) is list
        assert get_args(result) == (str,)

    def test_strip_none_preserves_non_union(self):
        from shepherd_runtime.task.metadata import _strip_none_from_type

        assert _strip_none_from_type(str) is str
        assert _strip_none_from_type(int) is int
        assert _strip_none_from_type(list[str]) == list[str]

    def test_strip_none_from_optional_none(self):
        from shepherd_runtime.task.metadata import _strip_none_from_type

        result = _strip_none_from_type(type(None) | None)
        assert result is type(None)
