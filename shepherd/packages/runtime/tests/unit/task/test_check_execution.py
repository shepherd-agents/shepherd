"""Unit tests for runtime check execution (precondition/postcondition)."""

from typing import Annotated

import pytest
from pydantic import BaseModel
from shepherd_core.errors import CheckFailedError
from shepherd_runtime.scope import Scope
from shepherd_runtime.task.authoring import Check, Input, NonEmpty, Output, task
from shepherd_tests import MockProvider

# Module-level Check instances so get_type_hints() can resolve them
_always_fail = Check(predicate=lambda v: False, message="always fails")
_fail1 = Check(predicate=lambda v: False, message="first fails")
_fail2 = Check(predicate=lambda v: False, message="second fails")
_pass1 = Check(predicate=lambda v: True)
_pass2 = Check(predicate=lambda v: True)


# =============================================================================
# Helpers
# =============================================================================


def _scope_with_provider(
    mock_responses: list[dict] | None = None,
) -> tuple[Scope, MockProvider]:
    """Create a root scope with a mock provider registered."""
    provider = MockProvider(mock_responses=mock_responses or [])
    scope = Scope(root=True)
    scope.register_provider("default", provider, default=True)
    return scope, provider


# =============================================================================
# Precondition checks
# =============================================================================


class TestPreconditionChecks:
    def test_precondition_failure_raises_check_failed_error(self):
        @task
        class T(BaseModel):
            """Test."""

            query: Annotated[Input(str), NonEmpty()]
            answer: Output(str)

        scope, _provider = _scope_with_provider()
        with scope:
            with pytest.raises(CheckFailedError, match="precondition") as exc_info:
                T(query="")
            assert exc_info.value.phase == "precondition"
            assert exc_info.value.field_name == "query"

    def test_precondition_failure_does_not_call_provider(self):
        @task
        class T(BaseModel):
            """Test."""

            query: Annotated[Input(str), NonEmpty()]
            answer: Output(str)

        scope, provider = _scope_with_provider()
        with scope:
            with pytest.raises(CheckFailedError):
                T(query="")
            assert len(provider.calls) == 0

    def test_precondition_failure_discards_fork(self):
        @task
        class T(BaseModel):
            """Test."""

            query: Annotated[Input(str), NonEmpty()]
            answer: Output(str)

        scope, _provider = _scope_with_provider()
        with scope:
            initial_effects = len(scope.effects)
            with pytest.raises(CheckFailedError):
                T(query="")
            # Parent scope should have no new effects from the discarded fork
            assert len(scope.effects) == initial_effects

    def test_precondition_passes_with_valid_input(self):
        @task
        class T(BaseModel):
            """Test."""

            query: Annotated[Input(str), NonEmpty()]
            answer: Output(str)

        scope, _provider = _scope_with_provider([{"structured": {"answer": "hello"}}])
        with scope:
            result = T(query="valid input")
            assert result.answer is not None


# =============================================================================
# Postcondition checks
# =============================================================================


class TestPostconditionChecks:
    def test_postcondition_failure_raises_check_failed_error(self):
        @task
        class T(BaseModel):
            """Test."""

            query: Input(str)
            answer: Annotated[Output(str), _always_fail]

        scope, _provider = _scope_with_provider([{"structured": {"answer": "some output"}}])
        with scope:
            with pytest.raises(CheckFailedError, match="postcondition") as exc_info:
                T(query="test")
            assert exc_info.value.phase == "postcondition"
            assert exc_info.value.field_name == "answer"

    def test_postcondition_failure_discards_fork(self):
        @task
        class T(BaseModel):
            """Test."""

            query: Input(str)
            answer: Annotated[Output(str), _always_fail]

        scope, _provider = _scope_with_provider([{"structured": {"answer": "output"}}])
        with scope:
            initial_effects = len(scope.effects)
            with pytest.raises(CheckFailedError):
                T(query="test")
            assert len(scope.effects) == initial_effects

    def test_postcondition_passes_with_valid_output(self):
        @task
        class T(BaseModel):
            """Test."""

            query: Input(str)
            answer: Annotated[Output(str), NonEmpty()]

        scope, _provider = _scope_with_provider([{"structured": {"answer": "valid output"}}])
        with scope:
            result = T(query="test")
            assert result.answer == "valid output"


# =============================================================================
# Multiple checks
# =============================================================================


class TestMultipleChecks:
    def test_first_failing_check_raises(self):
        @task
        class T(BaseModel):
            """Test."""

            query: Annotated[Input(str), _fail1, _fail2]
            answer: Output(str)

        scope, _provider = _scope_with_provider()
        with scope, pytest.raises(CheckFailedError, match="first fails"):
            T(query="test")

    def test_all_checks_pass(self):
        @task
        class T(BaseModel):
            """Test."""

            query: Annotated[Input(str), _pass1, _pass2]
            answer: Output(str)

        scope, _provider = _scope_with_provider([{"structured": {"answer": "ok"}}])
        with scope:
            result = T(query="test")
            assert result.answer == "ok"


# =============================================================================
# No checks (regression)
# =============================================================================


class TestNoChecks:
    def test_task_without_checks_still_works(self):
        @task
        class T(BaseModel):
            """Test."""

            query: Input(str)
            answer: Output(str)

        scope, _provider = _scope_with_provider([{"structured": {"answer": "result"}}])
        with scope:
            result = T(query="hello")
            assert result.answer == "result"

    def test_effects_merged_on_success(self):
        @task
        class T(BaseModel):
            """Test."""

            query: Input(str)
            answer: Output(str)

        scope, _provider = _scope_with_provider([{"structured": {"answer": "result"}}])
        with scope:
            T(query="hello")
            # Effects should be present from the merged fork
            assert len(scope.effects) > 0


# =============================================================================
# Custom execute() path
# =============================================================================


class TestCustomExecute:
    def test_postcondition_checks_run_after_custom_execute(self):
        @task
        class T(BaseModel):
            """Test."""

            query: Input(str)
            result: Annotated[Output(str), NonEmpty()]

            def execute(self) -> None:
                self.result = ""  # Postcondition should catch this

        scope, _provider = _scope_with_provider()
        with scope, pytest.raises(CheckFailedError, match="postcondition"):
            T(query="test")

    def test_custom_execute_passes_with_valid_output(self):
        @task
        class T(BaseModel):
            """Test."""

            query: Input(str)
            result: Annotated[Output(str), NonEmpty()]

            def execute(self) -> None:
                self.result = "computed"

        scope, _provider = _scope_with_provider()
        with scope:
            result = T(query="test")
            assert result.result == "computed"


# =============================================================================
# arun() path
# =============================================================================


class TestArunPath:
    @pytest.mark.asyncio
    async def test_precondition_failure_in_arun(self):
        @task
        class T(BaseModel):
            """Test."""

            query: Annotated[Input(str), NonEmpty()]
            answer: Output(str)

        scope, provider = _scope_with_provider()
        async with scope:
            with pytest.raises(CheckFailedError, match="precondition"):
                await T.arun(scope=scope, query="")
            assert len(provider.calls) == 0

    @pytest.mark.asyncio
    async def test_postcondition_failure_in_arun(self):
        @task
        class T(BaseModel):
            """Test."""

            query: Input(str)
            answer: Annotated[Output(str), _always_fail]

        scope, _provider = _scope_with_provider([{"structured": {"answer": "output"}}])
        async with scope:
            with pytest.raises(CheckFailedError, match="postcondition"):
                await T.arun(scope=scope, query="test")

    @pytest.mark.asyncio
    async def test_arun_success_merges_effects(self):
        @task
        class T(BaseModel):
            """Test."""

            query: Annotated[Input(str), NonEmpty()]
            answer: Output(str)

        scope, _provider = _scope_with_provider([{"structured": {"answer": "ok"}}])
        async with scope:
            result = await T.arun(scope=scope, query="valid")
            assert result.answer == "ok"
            assert len(scope.effects) > 0

    @pytest.mark.asyncio
    async def test_arun_custom_execute_with_checks(self):
        @task
        class T(BaseModel):
            """Test."""

            query: Input(str)
            result: Annotated[Output(str), NonEmpty()]

            def execute(self) -> None:
                self.result = "done"

        scope, _provider = _scope_with_provider()
        async with scope:
            result = await T.arun(scope=scope, query="test")
            assert result.result == "done"
