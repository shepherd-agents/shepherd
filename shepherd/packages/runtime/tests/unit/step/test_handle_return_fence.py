"""The P-030 fabrication fence, runtime stack (B1).

Handle-typed return slots must refuse fail-closed instead of emitting a
fabricatable object schema: handle slots are custody-resolved, never
provider-authored. Fenced surfaces on this stack:

- ``shepherd_runtime.step.output.return_type_to_output_schema`` (step returns)
- ``shepherd_runtime.task.output.generate_output_schema`` (task output fields)
- ``@step`` decoration (computes the output schema at decoration time)

The generic ``shepherd_core.schema.type_to_json_schema`` helper is deliberately
NOT fenced — it has non-return consumers; the fence's semantic target is
provider-facing output schemas.
"""

from types import SimpleNamespace
from typing import Annotated, Optional

import pytest
from shepherd_core.schema import HandleReturnSlotUnsupported, find_handle_annotation
from shepherd_runtime.nucleus import GitRepo
from shepherd_runtime.step.output import return_type_to_output_schema
from shepherd_runtime.task.output import generate_output_schema

HANDLE_RETURN_TYPES = [
    GitRepo,
    tuple[GitRepo, str],
    tuple[str, GitRepo],
    Annotated[GitRepo, "metadata"],
    tuple[Annotated[GitRepo, "metadata"], str],
    list[GitRepo],
    # The legacy spelling is deliberate coverage: typing.Optional produces
    # typing.Union (not types.UnionType), and the fence must catch both forms.
    Optional[GitRepo],  # noqa: UP045
]

PLAIN_RETURN_TYPES = [str, int, list[str], dict[str, int], tuple[str, int], None]


class TestReturnTypeToOutputSchemaFence:
    @pytest.mark.parametrize("return_type", HANDLE_RETURN_TYPES)
    def test_handle_return_slot_refuses(self, return_type):
        with pytest.raises(HandleReturnSlotUnsupported) as excinfo:
            return_type_to_output_schema(return_type)
        message = str(excinfo.value)
        assert "GitRepo" in message
        assert "custody" in message
        assert "projector" in message

    @pytest.mark.parametrize("return_type", PLAIN_RETURN_TYPES)
    def test_plain_return_slot_unchanged(self, return_type):
        schema = return_type_to_output_schema(return_type)
        assert schema["type"] == "json_schema"
        assert "schema" in schema

    def test_str_schema_byte_identical_to_prefence_shape(self):
        # The pre-fence shape for a plain-value return, asserted literally so a
        # fence regression that alters plain schemas trips loudly.
        assert return_type_to_output_schema(str) == {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "properties": {"result": {"type": "string"}},
                "required": ["result"],
            },
        }


class TestGenerateOutputSchemaFence:
    def test_handle_output_field_refuses(self):
        meta = SimpleNamespace(outputs={"repo": SimpleNamespace(inner_type=GitRepo, description=None)})
        with pytest.raises(HandleReturnSlotUnsupported):
            generate_output_schema(meta)

    def test_plain_output_field_unaffected(self):
        meta = SimpleNamespace(outputs={"report": SimpleNamespace(inner_type=str, description=None)})
        schema = generate_output_schema(meta)
        assert schema is not None


class TestStepExecutionRoutesThroughFence:
    def test_decorator_schema_alias_is_the_fenced_generator(self):
        # Step execution computes `output_schema = _return_type_to_output_schema(...)`
        # before provider dispatch (decorator.py); asserting the alias identity
        # proves the execution path cannot bypass the fence.
        from shepherd_runtime.step import decorator

        assert decorator._return_type_to_output_schema is return_type_to_output_schema

    def test_mock_value_generation_refuses_handle_returns(self):
        # `_generate_mock_value` (the mock-provider leg) also routes through the
        # fenced generator.
        from shepherd_runtime.step.decorator import _generate_mock_value

        with pytest.raises(HandleReturnSlotUnsupported):
            _generate_mock_value(GitRepo)


class TestFindHandleAnnotation:
    def test_marker_detected_directly_and_nested(self):
        assert find_handle_annotation(GitRepo) is GitRepo
        assert find_handle_annotation(tuple[GitRepo, str]) is GitRepo
        assert find_handle_annotation(Annotated[GitRepo, "x"]) is GitRepo

    def test_plain_types_are_handle_free(self):
        for annotation in PLAIN_RETURN_TYPES:
            assert find_handle_annotation(annotation) is None
