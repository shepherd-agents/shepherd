from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated

import pytest
from pydantic import BaseModel
from shepherd_core.schema import SINGLE_OUTPUT_KEY
from shepherd_runtime.effects import Match, Plan, PlanNotExtractable, Tell, handle, sync_tell
from shepherd_runtime.nucleus import (
    DeliveryFailed,
    Failed,
    Finished,
    NoActiveTaskRun,
    Permissive,
    ReadOnly,
    Run,
    WorkspaceNotConfigured,
    deliver,
    extract_callable_task_metadata,
    install_task_execution_hook,
    reset_workspace_for_tests,
    task,
    workspace,
)
from shepherd_runtime.provider_boundary import ModelRequest, ModelResponse
from shepherd_runtime.task.markers import InputMarker
from shepherd_runtime.trace import Trace, active_trace_recorder


@pytest.fixture(autouse=True)
def reset_workspace() -> None:
    reset_workspace_for_tests()
    yield
    reset_workspace_for_tests()


@dataclass(frozen=True)
class DataclassSummary:
    bullets: list[str]


class PydanticSummary(BaseModel):
    bullets: list[str]


def _handled_model_call(*structured_outputs: dict[str, object]):
    responses = list(structured_outputs)

    def responder(request: ModelRequest) -> ModelResponse:
        del request
        if not responses:
            raise AssertionError("model.call handler received more requests than expected")
        return ModelResponse(structured_output=responses.pop(0))

    return handle("model.call", responder)


def test_extracts_sync_callable_metadata() -> None:
    def summarize(
        article: Annotated[str, InputMarker(description="Article text")],
        *,
        limit: int = 3,
    ) -> str:
        return article[:limit]

    metadata = extract_callable_task_metadata(summarize)

    assert metadata.qualname.endswith("summarize")
    assert metadata.module == __name__
    assert metadata.is_async is False
    assert metadata.return_annotation is str
    assert metadata.source is not None
    # No `name=` override → name is None; consumers fall back to qualname.
    assert metadata.name is None
    assert metadata.guidance is None
    article = metadata.parameters[0]
    assert article.name == "article"
    assert article.base_annotation is str
    assert article.metadata == (InputMarker(description="Article text"),)
    limit = metadata.parameters[1]
    assert limit.name == "limit"
    assert limit.has_default is True
    assert limit.default == 3


def test_extracts_async_callable_metadata() -> None:
    async def summarize(article: str) -> str:
        return article

    metadata = extract_callable_task_metadata(summarize)

    assert metadata.is_async is True
    assert [parameter.name for parameter in metadata.parameters] == ["article"]


def test_task_decorator_kwargs_flow_to_metadata() -> None:
    """D10: @task(guidance=..., name=...) stored opaquely on TaskMetadata."""

    @task(guidance="custom-guidance", name="custom-name")
    def labelled(text: str) -> str:
        return text

    assert labelled.metadata.guidance == "custom-guidance"
    assert labelled.metadata.name == "custom-name"
    assert labelled.metadata.qualname.endswith("labelled")


def test_task_decorator_accepts_minimal_may_profiles() -> None:
    @task(may=ReadOnly)
    def read_only_task(text: str) -> str:
        return text

    @task(may=Permissive)
    def permissive_task(text: str) -> str:
        return text

    assert read_only_task.metadata.may is ReadOnly
    assert read_only_task.may is ReadOnly
    assert permissive_task.metadata.may is Permissive
    assert permissive_task.may is Permissive
    assert read_only_task.metadata.structural_may is None
    assert permissive_task.metadata.structural_may is None


def test_task_decorator_accepts_structural_may_metadata() -> None:
    surface = Match.subtree("nucleus_task")

    @task(may=surface)
    def structural_task(text: str) -> str:
        return text

    assert structural_task.metadata.may is None
    assert structural_task.may is None
    assert structural_task.metadata.structural_may is not None
    assert structural_task.structural_may is structural_task.metadata.structural_may
    assert structural_task.metadata.structural_may.declaration == surface
    assert structural_task.metadata.structural_may.match == surface


def test_task_decorator_accepts_extractable_plan_may_metadata() -> None:
    plan = Plan().allow_only("nucleus_task.**")

    @task(may=plan)
    def structural_task(text: str) -> str:
        return text

    assert structural_task.metadata.may is None
    assert structural_task.metadata.structural_may is not None
    assert structural_task.metadata.structural_may.declaration == plan
    assert structural_task.metadata.structural_may.match == Match.subtree("nucleus_task")


def test_task_decorator_rejects_non_extractable_plan_may() -> None:
    with pytest.raises(PlanNotExtractable):

        @task(may=Plan().deny_kind("nucleus_task.**"))
        def invalid(text: str) -> str:
            return text


def test_structural_may_metadata_is_not_perform_site_enforcement() -> None:
    @dataclass(frozen=True)
    class Marker(Tell, kind="nucleus_task.marker"):
        message: str

    @task(may=Match.nothing())
    def structural_task() -> str:
        sync_tell(Marker(message="not-enforced-in-this-slice"))
        return "ok"

    with workspace(model="fake"):
        assert structural_task() == "ok"
    assert structural_task.metadata.may is None
    assert structural_task.metadata.structural_may is not None


def test_task_decorator_rejects_unsupported_may_values() -> None:
    with pytest.raises(TypeError, match="ReadOnly, Permissive, Match, or an extractable Plan"):

        @task(may="filesystem.read")
        def invalid(text: str) -> str:
            return text


def test_bodyless_sync_task_synthesizes_deliver_from_docstring_and_args() -> None:
    captured: list[ModelRequest] = []

    def responder(request: ModelRequest) -> ModelResponse:
        captured.append(request)
        return ModelResponse(structured_output={SINGLE_OUTPUT_KEY: "done"})

    @task
    def edit_repo(goal: str) -> str:
        """Edit the repository to accomplish the goal and return a summary."""

    assert edit_repo.metadata.bodyless is True
    with workspace(model="fake"), handle("model.call", responder):
        run = edit_repo.detailed("add setup instructions")

    assert isinstance(run.outcome, Finished)
    assert run.unwrap() == "done"
    assert len(captured) == 1
    prompt = captured[0].messages[0].content
    assert "Edit the repository to accomplish the goal" in prompt
    assert "add setup instructions" in prompt


@pytest.mark.asyncio
async def test_bodyless_async_task_synthesizes_deliver() -> None:
    def responder(request: ModelRequest) -> ModelResponse:
        del request
        return ModelResponse(structured_output={SINGLE_OUTPUT_KEY: "ok"})

    @task
    async def summarize(text: str) -> str:
        """Summarize the text."""

    assert summarize.metadata.bodyless is True
    with workspace(model="fake"), handle("model.call", responder):
        run = await summarize.detailed("hello")

    assert isinstance(run.outcome, Finished)
    assert run.unwrap() == "ok"


def test_bodyless_task_uses_guidance_when_no_docstring() -> None:
    captured: list[ModelRequest] = []

    def responder(request: ModelRequest) -> ModelResponse:
        captured.append(request)
        return ModelResponse(structured_output={SINGLE_OUTPUT_KEY: "x"})

    @task(guidance="Classify the input.")
    def classify(text: str) -> str: ...

    assert classify.metadata.bodyless is True
    with workspace(model="fake"), handle("model.call", responder):
        classify.detailed("payload")
    assert "Classify the input." in captured[0].messages[0].content


def test_bodyless_task_without_docstring_or_guidance_raises() -> None:
    with pytest.raises(TypeError, match="docstring or guidance"):

        @task
        def edit(goal: str) -> str: ...


def test_bodyless_task_without_handler_fails_with_unhandled_model_call() -> None:
    @task
    def edit(goal: str) -> str:
        """Do the thing."""

    with workspace(model="fake"):
        run = edit.detailed("go")

    assert isinstance(run.outcome, Failed)
    assert run.outcome.error_type == "UnhandledModelCall"


def test_task_with_real_body_is_not_bodyless() -> None:
    @task
    def labelled(text: str) -> str:
        """Has a real body."""
        return text

    assert labelled.metadata.bodyless is False
    with workspace(model="fake"):
        assert labelled.detailed("hi").unwrap() == "hi"


def test_bodyless_task_preserves_may_profile() -> None:
    @task(may=ReadOnly)
    def edit(goal: str) -> str:
        """Edit under a read-only surface."""

    assert edit.metadata.bodyless is True
    assert edit.metadata.may is ReadOnly


def test_rejects_class_form() -> None:
    class ClassTask:
        pass

    with pytest.raises(TypeError, match="does not accept classes"):
        extract_callable_task_metadata(ClassTask)


def test_rejects_missing_return_annotation() -> None:
    def missing_return(article: str):
        return article

    with pytest.raises(TypeError, match="return annotation"):
        extract_callable_task_metadata(missing_return)


def test_rejects_missing_parameter_annotation() -> None:
    def missing_parameter(article) -> str:
        return article

    with pytest.raises(TypeError, match="must be annotated"):
        extract_callable_task_metadata(missing_parameter)


def test_public_task_rejects_classes() -> None:
    class ClassTask:
        pass

    with pytest.raises(TypeError, match="does not accept classes"):
        task(ClassTask)


def test_deliver_outside_task_raises() -> None:
    with pytest.raises(NoActiveTaskRun) as exc_info:
        deliver(str, goal="Return text")
    assert "inside a function decorated with @task" in str(exc_info.value)
    assert "task.detailed(...)" in str(exc_info.value)


def test_sync_task_argument_errors_raise_type_error_before_run() -> None:
    workspace(model="fake")

    @task
    def label(text: str) -> str:
        return text

    with pytest.raises(TypeError, match="missing"):
        label()

    with pytest.raises(TypeError, match="unexpected"):
        label("hello", unknown=True)


def test_sync_detailed_argument_errors_raise_type_error_before_run() -> None:
    workspace(model="fake")

    @task
    def label(text: str) -> str:
        return text

    with pytest.raises(TypeError, match="missing"):
        label.detailed()


@pytest.mark.asyncio
async def test_async_task_argument_errors_raise_type_error_before_run() -> None:
    workspace(model="fake")

    @task
    async def label(text: str) -> str:
        return text

    with pytest.raises(TypeError, match="missing"):
        await label()

    with pytest.raises(TypeError, match="too many"):
        await label("hello", "extra")


@pytest.mark.asyncio
async def test_async_detailed_argument_errors_raise_type_error_before_run() -> None:
    workspace(model="fake")

    @task
    async def label(text: str) -> str:
        return text

    with pytest.raises(TypeError, match="missing"):
        await label.detailed()


@pytest.mark.asyncio
async def test_async_task_ordinary_and_detailed_calls_return_t_and_run() -> None:
    workspace(model="fake")

    @task
    async def summarize(article: str) -> DataclassSummary:
        return await deliver(DataclassSummary, goal="Summarize.", evidence=[article])

    with _handled_model_call(
        {SINGLE_OUTPUT_KEY: {"bullets": ["one", "two"]}},
        {SINGLE_OUTPUT_KEY: {"bullets": ["one", "two"]}},
    ):
        result = await summarize("article")
        run = await summarize.detailed("article")

    assert result == DataclassSummary(bullets=["one", "two"])
    assert isinstance(run, Run)
    assert run.unwrap() == result
    assert isinstance(run.outcome, Finished)


@pytest.mark.asyncio
async def test_deliver_parses_tuple_structured_output_shape() -> None:
    workspace(model="fake")

    @task
    async def classify(text: str) -> tuple[str, int]:
        return await deliver(tuple[str, int], goal="Classify.", evidence=[text])

    with _handled_model_call({"output_0": "bug", "output_1": 7}):
        run = await classify.detailed("crash on launch")

    assert run.unwrap() == ("bug", 7)
    assert run.trace is not None
    assert run.trace.surface[-1].status == "completed"


@pytest.mark.asyncio
async def test_deliver_tuple_reports_missing_output_key() -> None:
    workspace(model="fake")

    @task
    async def classify(text: str) -> tuple[str, int]:
        return await deliver(tuple[str, int], goal="Classify.", evidence=[text])

    with _handled_model_call({"output_0": "bug"}):
        run = await classify.detailed("crash on launch")

    assert isinstance(run.outcome, Failed)
    assert run.outcome.error_type == "StepOutputError"
    assert "output_1" in run.outcome.message
    assert run.trace is not None
    assert run.trace.surface[-1].status == "failed"


def test_sync_task_ordinary_and_detailed_calls_return_t_and_run() -> None:
    workspace(model="fake")

    @task
    def summarize(article: str) -> PydanticSummary:
        return deliver(PydanticSummary, goal="Summarize.", evidence=[article])

    with _handled_model_call(
        {SINGLE_OUTPUT_KEY: {"bullets": ["one"]}},
        {SINGLE_OUTPUT_KEY: {"bullets": ["one"]}},
    ):
        result = summarize("article")
        run = summarize.detailed("article")

    assert result == PydanticSummary(bullets=["one"])
    assert isinstance(run, Run)
    assert run.unwrap() == result


def test_task_execution_hook_wraps_sync_body_with_trace_context() -> None:
    workspace(model="fake")
    events: list[tuple[str, str, object | None, bool]] = []

    @contextmanager
    def hook(metadata, context):  # type: ignore[no-untyped-def]
        events.append(("enter", metadata.qualname, context.ref, active_trace_recorder() is context.trace_recorder))
        try:
            yield
        finally:
            events.append(("exit", metadata.qualname, context.ref, active_trace_recorder() is context.trace_recorder))

    @task(may=ReadOnly)
    def label(text: str) -> str:
        return text

    with install_task_execution_hook(hook):
        run = label.detailed("hello")

    assert run.unwrap() == "hello"
    assert events == [
        ("enter", label.metadata.qualname, run.ref, True),
        ("exit", label.metadata.qualname, run.ref, True),
    ]


@pytest.mark.asyncio
async def test_task_execution_hook_wraps_async_failure_with_trace_context() -> None:
    workspace(model="fake")
    events: list[tuple[str, object | None, bool]] = []

    @contextmanager
    def hook(_metadata, context):  # type: ignore[no-untyped-def]
        events.append(("enter", context.ref, active_trace_recorder() is context.trace_recorder))
        try:
            yield
        finally:
            events.append(("exit", context.ref, active_trace_recorder() is context.trace_recorder))

    @task(may=ReadOnly)
    async def fail() -> str:
        raise ValueError("boom")

    with install_task_execution_hook(hook):
        run = await fail.detailed()

    assert isinstance(run.outcome, Failed)
    assert events == [
        ("enter", run.ref, True),
        ("exit", run.ref, True),
    ]


@pytest.mark.asyncio
async def test_task_run_exposes_empty_trace_snapshot() -> None:
    workspace(model="fake")

    @task
    async def observe_trace() -> str:
        assert active_trace_recorder() is not None
        return "ok"

    run = await observe_trace.detailed()

    assert active_trace_recorder() is None
    assert run.unwrap() == "ok"
    assert isinstance(run.trace, Trace)
    assert run.trace.run_ref == run.ref
    assert run.trace.kernel == ()
    assert run.trace.surface == ()


@pytest.mark.asyncio
async def test_task_run_trace_recorder_is_popped_after_failure() -> None:
    workspace(model="fake")

    @task
    async def fail_after_observing_trace() -> str:
        assert active_trace_recorder() is not None
        raise ValueError("boom")

    run = await fail_after_observing_trace.detailed()

    assert active_trace_recorder() is None
    assert isinstance(run.outcome, Failed)
    assert isinstance(run.trace, Trace)
    assert run.trace.run_ref == run.ref


@pytest.mark.asyncio
async def test_sync_task_can_be_called_from_running_event_loop() -> None:
    workspace(model="fake")

    @task
    def label(text: str) -> str:
        return deliver(str, goal="Label.", evidence=[text])

    with _handled_model_call({SINGLE_OUTPUT_KEY: "label"}):
        assert label("hello") == "label"


@pytest.mark.asyncio
async def test_task_call_without_workspace_raises() -> None:
    @task
    async def summarize(article: str) -> str:
        return article

    with pytest.raises(WorkspaceNotConfigured) as exc_info:
        await summarize("article")
    assert "active workspace" in str(exc_info.value)
    assert "workspace(model=...)" in str(exc_info.value)


@pytest.mark.asyncio
async def test_model_call_handler_error_maps_to_failed_run_and_delivery_failed() -> None:
    workspace(model="fake")

    def failing_model_call(request: ModelRequest) -> ModelResponse:
        del request
        raise RuntimeError("nope")

    @task
    async def summarize(article: str) -> str:
        return await deliver(str, goal="Summarize.", evidence=[article])

    with handle("model.call", failing_model_call):
        run = await summarize.detailed("article")
    assert isinstance(run.outcome, Failed)
    assert "nope" in run.outcome.message

    with pytest.raises(DeliveryFailed) as exc_info, handle("model.call", failing_model_call):
        await summarize("article")
    assert isinstance(exc_info.value.run, Run)


@pytest.mark.asyncio
async def test_missing_structured_result_key_maps_to_failed() -> None:
    workspace(model="fake")

    @task
    async def summarize(article: str) -> str:
        return await deliver(str, goal="Summarize.", evidence=[article])

    with _handled_model_call({"other": "value"}):
        run = await summarize.detailed("article")

    assert isinstance(run.outcome, Failed)
    assert "structured output" in run.outcome.message
    assert "result" in run.outcome.message
    assert run.trace is not None
    detail = run.trace.surface[-1].payload["detail_summary"]
    assert detail["reason"] == "missing_single_output_key"
    assert detail["response_shape"] == "structured_output"
    assert detail["structured_key_count"] == 1
