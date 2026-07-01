"""Tests for step metadata extraction, schema generation, and type utilities."""

import warnings
from typing import Literal

from shepherd_core.schema import SINGLE_OUTPUT_KEY, type_to_json_schema
from shepherd_runtime.step.api import DEFAULT_STEP_TIMEOUT
from shepherd_runtime.step.metadata import extract_step_metadata
from shepherd_runtime.step.output import return_type_to_output_schema

from .conftest import Severity

# =============================================================================
# Metadata Extraction
# =============================================================================


class TestMetadataExtraction:
    """Test metadata extraction from @step decorated methods."""

    def test_extract_basic_metadata(self):
        """Basic metadata extraction works."""

        def my_step(self, text: str) -> str:
            """Process the text."""

        metadata = extract_step_metadata(my_step)
        assert metadata.name == "my_step"
        assert metadata.docstring == "Process the text."
        assert "text" in metadata.inputs
        assert metadata.return_type is str

    def test_extract_metadata_multiple_params(self):
        """Metadata extraction handles multiple parameters."""

        def multi_param(self, a: str, b: int, c: bool = True) -> str:
            """Multi-param step."""

        metadata = extract_step_metadata(multi_param)
        assert len(metadata.inputs) == 3
        assert metadata.inputs["a"].type_annotation is str
        assert metadata.inputs["b"].type_annotation is int
        assert metadata.inputs["c"].type_annotation is bool
        assert metadata.inputs["a"].is_required is True
        assert metadata.inputs["c"].is_required is False

    def test_extract_metadata_shepherd_default(self):
        """Shepherd defaults to True."""

        def step_fn(self) -> str:
            """Step."""

        metadata = extract_step_metadata(step_fn)
        assert metadata.shepherd is True

    def test_extract_metadata_shepherd_false(self):
        """Shepherd can be set to False."""

        def step_fn(self) -> str:
            """Step."""

        metadata = extract_step_metadata(step_fn, shepherd=False)
        assert metadata.shepherd is False

    def test_extract_metadata_timeout_default(self):
        """Timeout defaults to DEFAULT_STEP_TIMEOUT."""

        def step_fn(self) -> str:
            """Step."""

        metadata = extract_step_metadata(step_fn)
        assert metadata.timeout == DEFAULT_STEP_TIMEOUT

    def test_extract_metadata_custom_timeout(self):
        """Custom timeout is stored."""

        def step_fn(self) -> str:
            """Step."""

        metadata = extract_step_metadata(step_fn, timeout=300)
        assert metadata.timeout == 300

    def test_extract_metadata_warns_no_docstring(self):
        """Warning is emitted when step has no docstring."""

        def no_doc_step(self, x: str) -> str: ...

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            extract_step_metadata(no_doc_step)
            assert len(w) >= 1
            assert "no docstring" in str(w[0].message).lower()

    def test_step_id_property(self):
        """step_id property returns correct format."""

        def my_step(self) -> str:
            """Step."""

        metadata = extract_step_metadata(my_step)
        assert metadata.step_id == "step:my_step"


# =============================================================================
# Schema Generation
# =============================================================================


class TestSchemaGeneration:
    """Test JSON schema generation for step return types."""

    def test_schema_string(self):
        """String type generates correct schema."""
        schema = return_type_to_output_schema(str)
        assert schema["schema"]["properties"][SINGLE_OUTPUT_KEY]["type"] == "string"

    def test_schema_int(self):
        """Int type generates correct schema."""
        schema = return_type_to_output_schema(int)
        assert schema["schema"]["properties"][SINGLE_OUTPUT_KEY]["type"] == "integer"

    def test_schema_bool(self):
        """Bool type generates correct schema."""
        schema = return_type_to_output_schema(bool)
        assert schema["schema"]["properties"][SINGLE_OUTPUT_KEY]["type"] == "boolean"

    def test_schema_literal(self):
        """Literal type generates enum schema."""
        schema = return_type_to_output_schema(Literal["a", "b", "c"])
        prop = schema["schema"]["properties"][SINGLE_OUTPUT_KEY]
        assert prop["type"] == "string"
        assert prop["enum"] == ["a", "b", "c"]

    def test_schema_enum(self):
        """Enum type generates enum schema."""
        schema = return_type_to_output_schema(Severity)
        prop = schema["schema"]["properties"][SINGLE_OUTPUT_KEY]
        assert prop["type"] == "string"
        assert set(prop["enum"]) == {"low", "medium", "high", "critical"}

    def test_schema_tuple(self):
        """Tuple type generates multiple output fields."""
        schema = return_type_to_output_schema(tuple[str, int, bool])
        props = schema["schema"]["properties"]
        assert "output_0" in props
        assert "output_1" in props
        assert "output_2" in props
        assert props["output_0"]["type"] == "string"
        assert props["output_1"]["type"] == "integer"
        assert props["output_2"]["type"] == "boolean"

    def test_schema_list(self):
        """List type generates array schema."""
        schema = return_type_to_output_schema(list[str])
        prop = schema["schema"]["properties"][SINGLE_OUTPUT_KEY]
        assert prop["type"] == "array"
        assert prop["items"]["type"] == "string"


class TestPythonTypeToJsonSchema:
    """Test type_to_json_schema utility."""

    def test_primitives(self):
        """Primitive types map correctly."""
        assert type_to_json_schema(str) == {"type": "string"}
        assert type_to_json_schema(int) == {"type": "integer"}
        assert type_to_json_schema(float) == {"type": "number"}
        assert type_to_json_schema(bool) == {"type": "boolean"}

    def test_none_type(self):
        """NoneType maps to null."""
        assert type_to_json_schema(type(None)) == {"type": "null"}
        assert type_to_json_schema(None) == {"type": "null"}

    def test_optional_produces_anyof(self):
        """Optional[T] produces anyOf schema with null (TypeAdapter behavior).

        Note: This differs from @task Output() handling, where None is stripped
        to enforce required outputs. Here we test raw type_to_json_schema behavior.
        """
        schema = type_to_json_schema(str | None)
        assert "anyOf" in schema
        types = {s.get("type") for s in schema["anyOf"]}
        assert types == {"string", "null"}

    def test_literal_string(self):
        """Literal with strings generates enum."""
        schema = type_to_json_schema(Literal["a", "b"])
        assert schema["type"] == "string"
        assert schema["enum"] == ["a", "b"]

    def test_literal_int(self):
        """Literal with ints generates integer enum."""
        schema = type_to_json_schema(Literal[1, 2, 3])
        assert schema["type"] == "integer"
        assert schema["enum"] == [1, 2, 3]

    def test_list_generic(self):
        """list[T] generates array schema with items."""
        schema = type_to_json_schema(list[str])
        assert schema["type"] == "array"
        assert schema["items"]["type"] == "string"

    def test_dict_generic(self):
        """dict[K, V] generates object schema."""
        schema = type_to_json_schema(dict[str, int])
        assert schema["type"] == "object"
        assert schema["additionalProperties"]["type"] == "integer"
