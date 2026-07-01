"""W2/W3 of the quickstart re-pin: variant emitters, durable trace, artifacts.

Each variant test names its legacy ancestor; the trace tests lift slice-1
invariant 3 to the vocabulary level (every terminal path traces durably, and
``run.trace`` reads it back through the slice-3 public route).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from shepherd_dialect import ClaudeAgentProvider, deliver, emit_artifact, handle, task, workspace
from shepherd_dialect.nucleus import (
    BudgetExhausted,
    Exhausted,
    Failed,
    Finished,
    Stopped,
    reset_workspace_for_tests,
)
from shepherd_dialect.provider_boundary import ModelResponse
from shepherd_dialect.supervision import SupervisorDenied

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _reset_workspace() -> Iterator[None]:
    reset_workspace_for_tests()
    yield
    reset_workspace_for_tests()


@dataclass(frozen=True)
class Model:
    name: str


def test_finished_run_traces_merged_with_readback(tmp_path) -> None:
    """Ancestor: runtime trace tests — the durable trace, now vocabulary-level."""
    workspace(model=Model("m"), root=str(tmp_path))

    @task
    def ok() -> str:
        return "done"

    run = ok.detailed()
    assert isinstance(run.outcome, Finished)
    trace = run.trace
    assert trace is not None
    summary = trace.summary()
    assert summary["terminal_status"] == "merged"
    assert summary["invocation_digest"].startswith("sha256:")


def test_failed_run_still_traces_discarded(tmp_path) -> None:
    """Ancestor: failure_discards + invariant 3 — the trace outlives the discard."""
    workspace(model=Model("m"), root=str(tmp_path))

    @task
    def boom() -> str:
        raise RuntimeError("mid-body")

    run = boom.detailed()
    assert isinstance(run.outcome, Failed)
    assert run.outcome.error_type == "RuntimeError"
    assert run.trace is not None
    assert run.trace.summary()["terminal_status"] == "discarded"


def test_stopped_maps_supervisor_denied(tmp_path) -> None:
    """Ancestor: supervised-deny observables — D3: SupervisorDenied → Stopped."""
    workspace(model=Model("m"), root=str(tmp_path))

    @task
    def denied() -> str:
        raise SupervisorDenied(effect=object(), reason="outside ./drafts/")

    run = denied.detailed()
    assert isinstance(run.outcome, Stopped)
    assert "drafts" in run.outcome.reason


def test_exhausted_maps_budget_exhausted(tmp_path) -> None:
    """D3 (probe a): a positively identified budget stop maps to Exhausted."""
    workspace(model=Model("m"), root=str(tmp_path))

    @task
    def out_of_turns() -> str:
        raise BudgetExhausted("max turns reached (4)")

    run = out_of_turns.detailed()
    assert isinstance(run.outcome, Exhausted)
    assert "max turns" in run.outcome.reason


def _fake_cap_returning(tmp_path, *, returncode: int, signal: str):
    class _Proc:
        pass

    _Proc.returncode = returncode
    _Proc.stderr = signal
    _Proc.stdout = ""

    class _Cap:
        working_path = str(tmp_path)

        def launch_confined(self, command, confinement):
            return _Proc()

    return _Cap()


@pytest.mark.skipif(__import__("shutil").which("claude") is None, reason="needs the claude CLI on PATH")
@pytest.mark.parametrize(
    "signal",
    [
        # The two forms the real CLI actually emits on turn exhaustion.
        "Error: Reached maximum number of turns (4)",
        '{"type":"result","terminal_reason":"max_turns","errors":["Reached maximum number of turns (4)"]}',
    ],
)
def test_provider_max_turns_signal_raises_budget_exhausted(tmp_path, signal: str) -> None:
    """The CLI's real turn-limit signal becomes the typed budget stop.

    Regression: the earlier probe matched ``"Reached max turns"`` — a string the
    CLI never emits — so real turn exhaustion fell through to a hard refusal.
    """
    cap = _fake_cap_returning(tmp_path, returncode=1, signal=signal)
    provider = ClaudeAgentProvider(prompt="x")
    with pytest.raises(BudgetExhausted, match="max turns"):
        provider.execute(None, None, None, {}, execution=cap, confinement=object())


@pytest.mark.skipif(__import__("shutil").which("claude") is None, reason="needs the claude CLI on PATH")
def test_provider_ambiguous_stop_stays_a_refusal(tmp_path) -> None:
    """A nonzero exit that isn't a turn-limit signal stays a refusal, not Exhausted.

    ``BudgetExhausted`` is not a ``RuntimeError``, so this pins that ambiguous
    stops (e.g. alarm kills) do not get miscategorized as a budget outcome.
    """
    cap = _fake_cap_returning(tmp_path, returncode=1, signal="some other failure")
    provider = ClaudeAgentProvider(prompt="x")
    with pytest.raises(RuntimeError):
        provider.execute(None, None, None, {}, execution=cap, confinement=object())


def test_emit_artifact_lands_in_run_artifacts(tmp_path) -> None:
    """Ancestor: nucleus/test_emit_artifact (CONTRACTS A1) — sole legacy coverage."""
    workspace(model=Model("m"), root=str(tmp_path))

    @task
    def produces() -> str:
        emit_artifact("report.md", "# hi")
        return "ok"

    run = produces.detailed()
    assert isinstance(run.outcome, Finished)
    assert [a.name for a in run.artifacts] == ["report.md"]
    assert run.artifacts[0].content == b"# hi"


def test_async_body_keeps_run_context_across_worker_thread(tmp_path) -> None:
    """W1.A: contextvars must cross the _run_coro worker hop."""
    workspace(model=Model("m"), root=str(tmp_path))

    @task
    async def produces() -> str:
        emit_artifact("async.txt", "kept")
        return "ok"

    run = asyncio.run(produces.detailed())
    assert isinstance(run.outcome, Finished)
    assert [a.name for a in run.artifacts] == ["async.txt"]
    assert run.artifacts[0].content == b"kept"


def test_task_may_profile_reaches_vcscore_and_trace(tmp_path) -> None:
    """W1.A: declared coarse may= is no longer hardcoded to Permissive."""
    ws = workspace(model=Model("m"), root=str(tmp_path))
    calls: list[dict[str, object]] = []
    original = ws._mg.execute_recorded

    def spy_execute_recorded(*args, **kwargs):
        calls.append(dict(kwargs))
        return original(*args, **kwargs)

    ws._mg.execute_recorded = spy_execute_recorded

    @task(may="ReadOnly")
    def reads_only() -> str:
        return "ok"

    run = reads_only.detailed()
    assert isinstance(run.outcome, Finished)
    assert calls
    assert calls[0]["may"] == "ReadOnly"
    lifecycle = run.trace.filter("run.lifecycle")[0]
    assert lifecycle["may_profile"] == "ReadOnly"
    assert lifecycle["may_source"] == "declared"


def test_readonly_task_refuses_in_process_model_dispatch(tmp_path) -> None:
    workspace(model=Model("m"), root=str(tmp_path))
    calls: list[object] = []

    @task(may="ReadOnly")
    def blocked_model_call() -> str:
        return deliver(str, goal="not allowed")

    with handle(
        "model.call",
        lambda req: calls.append(req) or ModelResponse(structured_output={"result": "unused"}),
    ):
        run = blocked_model_call.detailed()

    assert isinstance(run.outcome, Failed)
    assert run.outcome.error_type == "EffectNotPermitted"
    assert calls == []
    lifecycle = run.trace.filter("run.lifecycle")[0]
    assert lifecycle["may_profile"] == "ReadOnly"
    assert lifecycle["terminal_status"] == "discarded"


def test_omitted_task_may_is_defaulted_permissive_in_trace(tmp_path) -> None:
    ws = workspace(model=Model("m"), root=str(tmp_path))
    calls: list[dict[str, object]] = []
    original = ws._mg.execute_recorded

    def spy_execute_recorded(*args, **kwargs):
        calls.append(dict(kwargs))
        return original(*args, **kwargs)

    ws._mg.execute_recorded = spy_execute_recorded

    @task
    def defaulted() -> str:
        return "ok"

    run = defaulted.detailed()
    assert calls
    assert "may" not in calls[0]
    lifecycle = run.trace.filter("run.lifecycle")[0]
    assert lifecycle["may_profile"] == "Permissive"
    assert lifecycle["may_source"] == "defaulted"
