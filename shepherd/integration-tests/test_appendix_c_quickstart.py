"""Plan 00 nucleus integration smoke — Appendix C quickstart shape.

This test exercises the day-1 nucleus surface against explicit
``handle("model.call", ...)`` responders. It anchors the integration-gate
scaffolding described in `docs/design/proposed/260505-plans/CONTRACTS.md`
"Integration Gate" (PR 16).

Today's coverage (Plan 00 nucleus only):

- A1  workspace(...) opener and Workspace handle
- A2  Run[T] shape and outcome variants (Finished)
- A3  RunRef
- A4  TaskCallable call surface (.detailed, sync vs async unwrap)
- A7  Nucleus exception hierarchy (.unwrap raises on non-Finished)

Out of scope:

- Full provider SDK interposition
- Proof-backed kernel trace coverage
- Multi-turn delivery-loop enforcement
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from shepherd_core.schema import SINGLE_OUTPUT_KEY
from shepherd_runtime.nucleus import (
    Exhausted,
    Failed,
    Finished,
    Stopped,
    WorkspaceAlreadyConfigured,
    WorkspaceNotConfigured,
)
from shepherd_runtime.nucleus.workspace import reset_workspace_for_tests
from shepherd_runtime.provider_boundary import ModelRequest, ModelResponse

import shepherd
from shepherd import (
    DeliveryFailed,
    Run,
    RunRef,
    Workspace,
    deliver,
    handle,
    task,
    workspace,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_workspace() -> Iterator[None]:
    """Each test starts and ends with a clean ambient workspace."""
    reset_workspace_for_tests()
    yield
    reset_workspace_for_tests()


@dataclass(frozen=True)
class Joke:
    """Small structured output used by Appendix C smoke tests."""

    text: str


@dataclass(frozen=True)
class QuickstartModel:
    """Small model identity for Appendix C offline model.call handlers."""

    name: str


def _handled_model_call(structured_output: dict[str, object]) -> object:
    def responder(request: ModelRequest) -> ModelResponse:
        del request
        return ModelResponse(structured_output=structured_output)

    return handle("model.call", responder)


# ---------------------------------------------------------------------------
# A1 — workspace(...) opener
# ---------------------------------------------------------------------------


def test_workspace_opens_returns_handle(tmp_path) -> None:
    """workspace(...) returns a Workspace handle rooted at the requested path."""
    ws = workspace(model=QuickstartModel("appendix-c"), root=str(tmp_path))
    assert isinstance(ws, Workspace)
    assert ws.root is not None
    assert ws.root == tmp_path.expanduser().resolve()


def test_workspace_scope_is_idempotent(tmp_path) -> None:
    """Repeated Workspace.scope reads return the same scope object."""
    ws = workspace(model=QuickstartModel("appendix-c"), root=str(tmp_path))
    # `ws.scope` reads are idempotent and never fork (CONTRACTS A1, D4).
    assert ws.scope is ws.scope


def test_workspace_already_configured_raises_on_conflict(tmp_path) -> None:
    """Opening a conflicting ambient workspace raises the configured error."""
    p1 = QuickstartModel("first")
    p2 = QuickstartModel("second")
    workspace(model=p1, root=str(tmp_path))
    with pytest.raises(WorkspaceAlreadyConfigured):
        workspace(model=p2, root=str(tmp_path))


def test_workspace_not_configured_raises_for_task_call(tmp_path) -> None:
    """Calling a task without an ambient workspace raises WorkspaceNotConfigured."""
    @task
    async def needs_workspace() -> str:
        return await deliver(str, goal="...")

    with pytest.raises(WorkspaceNotConfigured):
        asyncio.run(needs_workspace())


# ---------------------------------------------------------------------------
# A4 — Function-form @task: sync and async
# ---------------------------------------------------------------------------


def test_task_sync_unwraps_to_value(tmp_path) -> None:
    """A sync function-form task unwraps to the typed value."""
    workspace(model=QuickstartModel("appendix-c"), root=str(tmp_path))

    @task
    def tell_joke(topic: str) -> Joke:
        return deliver(Joke, goal="tell a joke", evidence=[topic])

    with _handled_model_call({SINGLE_OUTPUT_KEY: {"text": "sync joke"}}):
        result = tell_joke("recursion")
    assert isinstance(result, Joke)
    assert result.text == "sync joke"


def test_task_async_unwraps_to_value(tmp_path) -> None:
    """An async function-form task unwraps to the typed value."""
    workspace(model=QuickstartModel("appendix-c"), root=str(tmp_path))

    @task
    async def tell_joke(topic: str) -> Joke:
        return await deliver(Joke, goal="tell a joke", evidence=[topic])

    async def run() -> Joke:
        with _handled_model_call({SINGLE_OUTPUT_KEY: {"text": "async joke"}}):
            return await tell_joke("recursion")

    result = asyncio.run(run())
    assert isinstance(result, Joke)
    assert result.text == "async joke"


# ---------------------------------------------------------------------------
# A2 / A3 — Run[T] shape, outcome variants, RunRef
# ---------------------------------------------------------------------------


def test_detailed_returns_run_with_finished_outcome(tmp_path) -> None:
    """.detailed(...) returns a Run with a Finished outcome and RunRef."""
    workspace(model=QuickstartModel("appendix-c"), root=str(tmp_path))

    @task
    async def tell_joke(topic: str) -> Joke:
        return await deliver(Joke, goal="tell a joke", evidence=[topic])

    async def execute() -> Run[Joke]:
        with _handled_model_call({SINGLE_OUTPUT_KEY: {"text": "detailed"}}):
            return await tell_joke.detailed("recursion")

    run = asyncio.run(execute())

    # Run[T] shape per CONTRACTS A2.
    assert isinstance(run, Run)
    assert isinstance(run.outcome, Finished)
    assert run.outcome.value == Joke(text="detailed")
    # RunRef per CONTRACTS A3.
    assert isinstance(run.ref, RunRef)
    assert run.ref.id.startswith("run-")
    # Duration is a non-negative float.
    assert run.duration >= 0.0


def test_run_unwrap_returns_finished_value(tmp_path) -> None:
    """Run.unwrap() returns the value for a Finished outcome."""
    workspace(model=QuickstartModel("appendix-c"), root=str(tmp_path))

    @task
    async def tell_joke() -> Joke:
        return await deliver(Joke, goal="tell")

    async def execute() -> Run[Joke]:
        with _handled_model_call({SINGLE_OUTPUT_KEY: {"text": "unwrap"}}):
            return await tell_joke.detailed()

    run = asyncio.run(execute())
    assert run.unwrap() == Joke(text="unwrap")


# ---------------------------------------------------------------------------
# A7 — .unwrap() raises on non-Finished outcomes
# ---------------------------------------------------------------------------


def test_unwrap_raises_delivery_failed(tmp_path) -> None:
    """Plain task calls raise DeliveryFailed for non-Finished outcomes."""
    # Missing required 'result' key triggers a Failed outcome.
    workspace(model=QuickstartModel("appendix-c"), root=str(tmp_path))

    @task
    async def will_fail() -> Joke:
        return await deliver(Joke, goal="...")

    async def execute() -> Joke:
        with _handled_model_call({"text": "no result key"}):
            return await will_fail()

    with pytest.raises(DeliveryFailed) as excinfo:
        asyncio.run(execute())

    # The DeliveryFailed exception carries the Run for diagnostics (A7).
    assert excinfo.value.run is not None
    assert isinstance(excinfo.value.run.outcome, Failed)


def test_detailed_does_not_raise_on_failure(tmp_path) -> None:
    """.detailed(...) returns a failed Run instead of raising."""
    workspace(model=QuickstartModel("appendix-c"), root=str(tmp_path))

    @task
    async def will_fail() -> Joke:
        return await deliver(Joke, goal="...")

    async def execute() -> Run[Joke]:
        with _handled_model_call({"text": "no result key"}):
            return await will_fail.detailed()

    # `.detailed(...)` returns the Run regardless of outcome; only the
    # plain call form raises (CONTRACTS A4 + A7).
    run = asyncio.run(execute())
    assert isinstance(run, Run)
    assert isinstance(run.outcome, Failed)


# ---------------------------------------------------------------------------
# Outcome variants — frozen, hashable shapes
# ---------------------------------------------------------------------------


def test_outcome_variants_are_frozen_dataclasses() -> None:
    """Outcome variants expose frozen dataclass shapes."""
    finished = Finished(value=Joke(text="hi"))
    exhausted = Exhausted(reason="budget")
    stopped = Stopped(reason="cancel")
    failed = Failed(error_type="X", message="boom")
    # Each variant is the canonical type per CONTRACTS A2 / A6.
    assert isinstance(finished, Finished)
    assert isinstance(exhausted, Exhausted)
    assert isinstance(stopped, Stopped)
    assert isinstance(failed, Failed)
    # Failed.retryable defaults to None (per CONTRACTS A2 outcome variants).
    assert failed.retryable is None


# ---------------------------------------------------------------------------
# Public facade sanity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "workspace",
        "Workspace",
        "task",
        "deliver",
        "Run",
        "RunRef",
        "DeliveryFailed",
        "emit_artifact",
        "Artifact",
        "handle",
        "ask",
        "tell",
        "current_binding",
    ],
)
def test_public_reexport_present(name: str) -> None:
    """The top-level facade exports only the callable-spine symbols."""
    assert hasattr(shepherd, name), f"shepherd.{name} missing from callable-spine facade"


@pytest.mark.parametrize(
    "name",
    [
        "Finished",
        "Exhausted",
        "Stopped",
        "Failed",
        "DeliveryExhausted",
        "DeliveryStopped",
        "WorkspaceNotConfigured",
        "WorkspaceAlreadyConfigured",
        "NoActiveTaskRun",
    ],
)
def test_owner_path_nucleus_symbols_are_not_top_level(name: str) -> None:
    """Advanced nucleus symbols stay under ``shepherd_runtime.nucleus``."""
    assert not hasattr(shepherd, name)
