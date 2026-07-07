"""The P-030 fabrication fence, dialect stack + cross-stack parity (B1).

The dialect carries a deliberate no-core-imports port of the schema stack
(`_step_schema.py`), so the fence exists twice. A fence landed in only one
stack leaks through the other (the F7 lesson) — the parity tests here assert
the two stacks refuse the same input set with the same exception name and a
byte-identical message, and produce identical schemas for plain values.

Carve-out: task-registration *parameter* schemas are unaffected — handle-typed
parameters (`May[GitRepo, ...]`) are the shipped 0.2.0 surface and must keep
lowering; only provider-facing *return-slot* schemas refuse.
"""

from typing import Annotated, Optional

import pytest
from shepherd_runtime.nucleus import GitRepo
from shepherd_runtime.step.output import return_type_to_output_schema as runtime_rtos

from shepherd_dialect._step_schema import (
    HandleReturnSlotUnsupported as DialectHandleReturnSlotUnsupported,
)
from shepherd_dialect.steps import return_type_to_output_schema as dialect_rtos

HANDLE_RETURN_TYPES = [
    GitRepo,
    tuple[GitRepo, str],
    tuple[str, GitRepo],
    Annotated[GitRepo, "metadata"],
    list[GitRepo],
    # The legacy spelling is deliberate coverage: typing.Optional produces
    # typing.Union (not types.UnionType), and the fence must catch both forms.
    Optional[GitRepo],  # noqa: UP045
]

PLAIN_RETURN_TYPES = [str, int, list[str], dict[str, int], tuple[str, int], None]


class TestDialectFence:
    @pytest.mark.parametrize("return_type", HANDLE_RETURN_TYPES)
    def test_handle_return_slot_refuses(self, return_type):
        with pytest.raises(DialectHandleReturnSlotUnsupported) as excinfo:
            dialect_rtos(return_type)
        assert "GitRepo" in str(excinfo.value)

    @pytest.mark.parametrize("return_type", PLAIN_RETURN_TYPES)
    def test_plain_return_slot_unchanged(self, return_type):
        schema = dialect_rtos(return_type)
        assert schema["type"] == "json_schema"


class TestTwoStackParity:
    """Both stacks refuse identically — the fence's definition of done."""

    @pytest.mark.parametrize("return_type", HANDLE_RETURN_TYPES)
    def test_refusals_are_identical(self, return_type):
        with pytest.raises(Exception) as dialect_exc:
            dialect_rtos(return_type)
        with pytest.raises(Exception) as runtime_exc:
            runtime_rtos(return_type)
        assert type(dialect_exc.value).__name__ == "HandleReturnSlotUnsupported"
        assert type(runtime_exc.value).__name__ == "HandleReturnSlotUnsupported"
        assert str(dialect_exc.value) == str(runtime_exc.value)

    @pytest.mark.parametrize("return_type", PLAIN_RETURN_TYPES)
    def test_plain_value_schemas_are_identical(self, return_type):
        assert dialect_rtos(return_type) == runtime_rtos(return_type)


class TestRegistrationCarveOut:
    """Handle-typed *parameters* keep lowering — the shipped grant surface."""

    def test_task_input_model_accepts_handle_parameter(self):
        from shepherd_dialect.task_meta import task_input_model

        def fix_bug(repo: Annotated[GitRepo, "ReadWrite"], description: str) -> str:
            """Fix the bug in the repo."""

        model = task_input_model(fix_bug)
        assert set(model.model_fields) == {"repo", "description"}

    def test_task_prompt_refuses_handle_return(self):
        from shepherd_dialect.task_meta import task_prompt

        def produce_repo(description: str) -> tuple[GitRepo, str]:
            """Produce a repo (provider-facing prompt must refuse)."""

        with pytest.raises(DialectHandleReturnSlotUnsupported):
            task_prompt(produce_repo)

    def test_task_prompt_unaffected_for_plain_returns(self):
        from shepherd_dialect.task_meta import task_prompt

        def summarize(text: str) -> str:
            """Summarize the text."""

        prompt = task_prompt(summarize)
        assert "Respond with JSON" in prompt
