"""Tests for the function-form ``emit_artifact`` verb (Tranche 7 PR 21).

Function-form replacement for class-form ``Artifact()`` field markers.
Per CONTRACTS A1, the verb produces a durable artifact during task
execution; the artifact is collected on ``Run[T].artifacts`` after
the task completes.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError, dataclass

import pytest
from shepherd_core.schema import SINGLE_OUTPUT_KEY
from shepherd_runtime.nucleus import Finished, NoActiveTaskRun
from shepherd_runtime.nucleus.workspace import reset_workspace_for_tests
from shepherd_runtime.provider_boundary import ModelRequest, ModelResponse
from shepherd_runtime.trace import ArtifactEmitted
from shepherd_tests import MockProvider

import shepherd
from shepherd import (
    Artifact,
    Run,
    deliver,
    emit_artifact,
    handle,
    task,
    workspace,
)


@pytest.fixture(autouse=True)
def _reset_workspace():
    reset_workspace_for_tests()
    yield
    reset_workspace_for_tests()


@dataclass(frozen=True)
class _Result:
    summary: str


def _handled_model_calls(*structured_outputs: dict[str, object]):
    responses = list(structured_outputs)

    def responder(request: ModelRequest) -> ModelResponse:
        del request
        if not responses:
            raise AssertionError("model.call handler received more requests than expected")
        return ModelResponse(structured_output=responses.pop(0))

    return handle("model.call", responder)


def test_artifact_re_exports_through_top_level() -> None:
    assert hasattr(shepherd, "Artifact")
    assert hasattr(shepherd, "emit_artifact")


def test_emit_artifact_outside_task_raises(tmp_path) -> None:
    workspace(model=MockProvider(), root=str(tmp_path))

    with pytest.raises(NoActiveTaskRun):
        asyncio.run(emit_artifact(content="hi", kind="report", name="r.txt"))


def test_emit_artifact_appears_on_run_artifacts(tmp_path) -> None:
    provider = MockProvider(structured_output={"result": {"summary": "ok"}})
    workspace(model=provider, root=str(tmp_path))

    @task
    async def with_artifact(arg: str) -> _Result:
        result = await deliver(_Result, goal="...", evidence=[arg])
        await emit_artifact(content="report body", kind="report", name="lint.txt")
        return result

    async def execute() -> Run[_Result]:
        with _handled_model_calls({SINGLE_OUTPUT_KEY: {"summary": "ok"}}):
            return await with_artifact.detailed("input")

    run = asyncio.run(execute())
    assert isinstance(run, Run)
    assert isinstance(run.outcome, Finished)
    assert len(run.artifacts) == 1
    assert run.trace is not None
    assert isinstance(run.trace.surface[-1], ArtifactEmitted)
    assert run.trace.surface[-1].payload == {
        "artifact_kind": "report",
        "name": "lint.txt",
        "metadata_summary": {
            "content_type": "str",
            "content_length": len("report body"),
            "metadata_keys": [],
        },
    }
    artifact = run.artifacts[0]
    assert isinstance(artifact, Artifact)
    assert artifact.kind == "report"
    assert artifact.name == "lint.txt"
    assert artifact.content == "report body"


def test_multiple_artifacts_accumulate_in_emission_order(tmp_path) -> None:
    provider = MockProvider(structured_output={"result": {"summary": "ok"}})
    workspace(model=provider, root=str(tmp_path))

    @task
    async def with_two_artifacts() -> _Result:
        await emit_artifact(content="first", kind="log", name="1.txt")
        result = await deliver(_Result, goal="...")
        await emit_artifact(content="second", kind="log", name="2.txt")
        return result

    async def execute() -> Run[_Result]:
        with _handled_model_calls({SINGLE_OUTPUT_KEY: {"summary": "ok"}}):
            return await with_two_artifacts.detailed()

    run = asyncio.run(execute())
    assert len(run.artifacts) == 2
    assert run.artifacts[0].name == "1.txt"
    assert run.artifacts[1].name == "2.txt"
    assert run.trace is not None
    assert run.trace.surface[0].kind == "artifact_emitted"
    assert run.trace.surface[-1].kind == "artifact_emitted"


def test_artifact_supports_bytes_content(tmp_path) -> None:
    provider = MockProvider(structured_output={"result": {"summary": "ok"}})
    workspace(model=provider, root=str(tmp_path))

    @task
    async def with_bytes() -> _Result:
        await emit_artifact(content=b"\x00\x01\x02", kind="binary", name="x.bin")
        return await deliver(_Result, goal="...")

    async def execute() -> Run[_Result]:
        with _handled_model_calls({SINGLE_OUTPUT_KEY: {"summary": "ok"}}):
            return await with_bytes.detailed()

    run = asyncio.run(execute())
    assert len(run.artifacts) == 1
    assert run.artifacts[0].content == b"\x00\x01\x02"


def test_artifact_metadata_round_trips(tmp_path) -> None:
    provider = MockProvider(structured_output={"result": {"summary": "ok"}})
    workspace(model=provider, root=str(tmp_path))

    @task
    async def with_metadata() -> _Result:
        await emit_artifact(
            content="x",
            kind="report",
            name="r.txt",
            metadata={"source": "lint", "exit_code": 0},
        )
        return await deliver(_Result, goal="...")

    async def execute() -> Run[_Result]:
        with _handled_model_calls({SINGLE_OUTPUT_KEY: {"summary": "ok"}}):
            return await with_metadata.detailed()

    run = asyncio.run(execute())
    assert run.artifacts[0].metadata == {"source": "lint", "exit_code": 0}
    assert run.trace is not None
    artifact_events = [event for event in run.trace.surface if isinstance(event, ArtifactEmitted)]
    assert len(artifact_events) == 1
    assert artifact_events[0].payload["metadata_summary"]["metadata_keys"] == [
        "exit_code",
        "source",
    ]


def test_emit_artifact_trace_omits_content_and_metadata_values(tmp_path) -> None:
    workspace(model="fake", root=str(tmp_path))

    @task
    async def with_secret_artifact() -> str:
        await emit_artifact(
            content="secret report body",
            kind="report",
            name="secret.txt",
            metadata={
                "claim_level": "metadata value, not trace claim",
                "token": "secret metadata token",
            },
        )
        return "ok"

    run = asyncio.run(with_secret_artifact.detailed())

    assert run.unwrap() == "ok"
    assert run.trace is not None
    assert len(run.artifacts) == 1
    artifact_event = run.trace.surface[0]
    assert isinstance(artifact_event, ArtifactEmitted)
    assert artifact_event.payload == {
        "artifact_kind": "report",
        "name": "secret.txt",
        "metadata_summary": {
            "content_type": "str",
            "content_length": len("secret report body"),
            "metadata_keys": ["claim_level", "token"],
        },
    }

    serialized = json.dumps(run.trace.to_json())
    assert "secret report body" not in serialized
    assert "secret metadata token" not in serialized
    assert "metadata value, not trace claim" not in serialized
    assert type(run.trace).from_json(run.trace.to_json()) == run.trace


def test_artifact_is_frozen_dataclass() -> None:
    artifact = Artifact(kind="report", name="x.txt", content="hi", metadata={"k": "v"})
    with pytest.raises(FrozenInstanceError):
        artifact.kind = "other"  # type: ignore[misc]


def test_emit_artifact_inside_sync_task_returns_artifact_directly(tmp_path) -> None:
    """CONTRACTS A8 sync dispatch: inside a sync ``@task`` body,
    ``emit_artifact(...)`` blocks via the same ``run_sync`` helper as
    ``deliver()`` and returns ``Artifact`` directly — no ``await``.
    """
    provider = MockProvider(structured_output={"result": {"summary": "ok"}})
    workspace(model=provider, root=str(tmp_path))

    @task
    def with_sync_artifact() -> _Result:
        # Note: no `await` — inside a sync task, emit_artifact returns
        # the Artifact synchronously.
        artifact = emit_artifact(content="sync body", kind="report", name="s.txt")
        assert isinstance(artifact, Artifact)
        assert artifact.name == "s.txt"
        return deliver(_Result, goal="...")

    with _handled_model_calls({SINGLE_OUTPUT_KEY: {"summary": "ok"}}):
        run = with_sync_artifact.detailed()
    assert isinstance(run.outcome, Finished)
    assert len(run.artifacts) == 1
    assert run.artifacts[0].name == "s.txt"
    assert run.artifacts[0].content == "sync body"
    assert run.trace is not None
    assert isinstance(run.trace.surface[0], ArtifactEmitted)
    assert run.trace.surface[0].payload["metadata_summary"]["content_length"] == len("sync body")


def test_failed_run_still_carries_emitted_artifacts(tmp_path) -> None:
    """Artifacts emitted before the failure should still appear on
    ``Run.artifacts`` so debugging can inspect partial work."""
    provider = MockProvider(structured_output={"text": "missing-result"})
    workspace(model=provider, root=str(tmp_path))

    @task
    async def with_partial_artifact() -> _Result:
        await emit_artifact(content="partial", kind="log", name="partial.txt")
        return await deliver(_Result, goal="...")

    async def execute() -> Run[_Result]:
        with _handled_model_calls({"text": "missing-result"}):
            return await with_partial_artifact.detailed()

    run = asyncio.run(execute())
    # MockProvider's missing-result triggers a Failed outcome.
    assert not isinstance(run.outcome, Finished)
    # Artifact emitted before the failure should still be visible.
    assert len(run.artifacts) == 1
    assert run.artifacts[0].name == "partial.txt"


# ----------------------------------------------------------------------
# Nested-task artifact-ownership tests
#
# CONTRACTS A8 specifies that artifacts attach to the active
# ``TaskRunContext``, not to the Workspace's root Scope. The
# ``_active_task_runs`` contextvar holds an immutable tuple stack and
# ``active_task_run()`` returns ``stack[-1]``, so each nested @task
# pushes/pops its own context. These tests pin the per-task isolation
# semantics before Plan 02 attaches durable storage so future surface
# records know which Run to attribute artifacts to.
# ----------------------------------------------------------------------


def test_nested_tasks_isolate_artifact_buffers(tmp_path) -> None:
    """Outer emits A; inner emits B. Each Run sees only its own."""
    provider = MockProvider(
        mock_responses=[
            {"text": "outer", "structured": {"result": {"summary": "outer"}}},
            {"text": "inner", "structured": {"result": {"summary": "inner"}}},
        ]
    )
    workspace(model=provider, root=str(tmp_path))

    @task
    async def inner() -> _Result:
        await emit_artifact(content="B", kind="log", name="inner.txt")
        return await deliver(_Result, goal="inner")

    @task
    async def outer() -> _Result:
        await emit_artifact(content="A", kind="log", name="outer.txt")
        inner_run = await inner.detailed()
        # Inner's run carries only its own artifact.
        assert len(inner_run.artifacts) == 1
        assert inner_run.artifacts[0].name == "inner.txt"
        return await deliver(_Result, goal="outer")

    async def execute() -> Run[_Result]:
        with _handled_model_calls(
            {SINGLE_OUTPUT_KEY: {"summary": "inner"}},
            {SINGLE_OUTPUT_KEY: {"summary": "outer"}},
        ):
            return await outer.detailed()

    outer_run = asyncio.run(execute())
    # Outer's run carries only its own artifact, not inner's.
    assert isinstance(outer_run.outcome, Finished)
    assert len(outer_run.artifacts) == 1
    assert outer_run.artifacts[0].name == "outer.txt"


def test_nested_tasks_preserve_partial_artifacts_on_inner_failure(tmp_path) -> None:
    """If the inner task fails after emitting, the outer's buffer is
    unaffected and the inner's failed Run still carries its partial
    artifact."""
    provider = MockProvider(
        mock_responses=[
            # Inner: missing structured key triggers Failed.
            {"text": "inner-fail"},
            # Outer: succeeds with a structured result.
            {"text": "outer", "structured": {"result": {"summary": "outer"}}},
        ]
    )
    workspace(model=provider, root=str(tmp_path))

    @task
    async def inner() -> _Result:
        await emit_artifact(content="B-partial", kind="log", name="inner-partial.txt")
        return await deliver(_Result, goal="inner")

    @task
    async def outer() -> _Result:
        await emit_artifact(content="A", kind="log", name="outer.txt")
        inner_run = await inner.detailed()
        # Inner's failed Run still surfaces its partial artifact.
        assert not isinstance(inner_run.outcome, Finished)
        assert len(inner_run.artifacts) == 1
        assert inner_run.artifacts[0].name == "inner-partial.txt"
        return await deliver(_Result, goal="outer")

    async def execute() -> Run[_Result]:
        with _handled_model_calls(
            {"text": "inner-fail"},
            {SINGLE_OUTPUT_KEY: {"summary": "outer"}},
        ):
            return await outer.detailed()

    outer_run = asyncio.run(execute())
    # Outer is unaffected by the inner failure; only outer's own
    # artifact appears on its Run.
    assert isinstance(outer_run.outcome, Finished)
    assert len(outer_run.artifacts) == 1
    assert outer_run.artifacts[0].name == "outer.txt"


def test_inner_task_preserves_emission_order_within_its_buffer(tmp_path) -> None:
    """Multiple emissions inside an inner task accumulate in order on
    the inner Run, with no leak to the outer Run."""
    provider = MockProvider(
        mock_responses=[
            {"text": "outer", "structured": {"result": {"summary": "outer"}}},
            {"text": "inner", "structured": {"result": {"summary": "inner"}}},
        ]
    )
    workspace(model=provider, root=str(tmp_path))

    @task
    async def inner() -> _Result:
        await emit_artifact(content="B1", kind="log", name="inner-1.txt")
        await emit_artifact(content="B2", kind="log", name="inner-2.txt")
        return await deliver(_Result, goal="inner")

    @task
    async def outer() -> _Result:
        inner_run = await inner.detailed()
        assert [a.name for a in inner_run.artifacts] == ["inner-1.txt", "inner-2.txt"]
        return await deliver(_Result, goal="outer")

    async def execute() -> Run[_Result]:
        with _handled_model_calls(
            {SINGLE_OUTPUT_KEY: {"summary": "inner"}},
            {SINGLE_OUTPUT_KEY: {"summary": "outer"}},
        ):
            return await outer.detailed()

    outer_run = asyncio.run(execute())
    # Outer never emitted; its buffer must be empty.
    assert isinstance(outer_run.outcome, Finished)
    assert outer_run.artifacts == ()
