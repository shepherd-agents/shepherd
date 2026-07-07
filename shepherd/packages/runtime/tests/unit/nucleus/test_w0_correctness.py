"""W0 correctness fixes (2132 §W0/§W3 via MDP tranche B3).

B3.1 — dual-key shim: the taught ``handle("model.call.requested", ...)`` idiom
resolves (Bug 1: the documented mock was silently ignored, so a reachable
provider would take the call — paid, non-deterministic). Legacy ``model.call``
stays accepted; recorded effect-kind vocabulary is unchanged (the kind-string
bump is a durable-vocabulary decision, deliberately not taken here).

B3.3 — tri-state body classification: a task defined via exec/REPL/notebook
(source unavailable) with an empty-shaped compiled body raises loud
``AmbiguousTaskBody`` at call time instead of running to a silent ``None``.
A bodied exec-defined task (non-trivial bytecode) still runs. A
handle-annotated ambiguous task gets the placement refusal, not the
introspection error (B2 ordering).

B3.4 — workspace re-entry: idle reconfiguration (the notebook cell-re-run
idiom, where a fresh model object fails the identity-based config match)
replaces the workspace instead of trapping the session; reconfiguring while a
task run is active still refuses.
"""

from __future__ import annotations

from typing import Annotated

import pytest
from shepherd_core.schema import SINGLE_OUTPUT_KEY
from shepherd_runtime.effects import handle
from shepherd_runtime.nucleus import (
    AmbientWorldAccessRefused,
    AmbiguousTaskBody,
    GitRepo,
    current_workspace,
    reset_workspace_for_tests,
    task,
    workspace,
)
from shepherd_runtime.provider_boundary import ModelRequest, ModelResponse


@pytest.fixture(autouse=True)
def reset_workspace() -> None:
    reset_workspace_for_tests()
    yield
    reset_workspace_for_tests()


def _responder(answer: str, captured: list[ModelRequest] | None = None):
    def respond(request: ModelRequest) -> ModelResponse:
        if captured is not None:
            captured.append(request)
        return ModelResponse(structured_output={SINGLE_OUTPUT_KEY: answer})

    return respond


@task
def summarize(text: str) -> str:
    """Summarize the text."""


class TestDualKeyShim:
    def test_taught_spelling_resolves(self) -> None:
        # Bug 1 regression: the documented mock idiom must intercept.
        captured: list[ModelRequest] = []
        with workspace(model="fake"), handle("model.call.requested", _responder("mocked", captured)):
            assert summarize(text="hello") == "mocked"
        assert len(captured) == 1

    def test_legacy_spelling_still_resolves(self) -> None:
        with workspace(model="fake"), handle("model.call", _responder("legacy")):
            assert summarize(text="hello") == "legacy"

    def test_innermost_wins_across_mixed_spellings(self) -> None:
        # The alias normalizes at installation, so LIFO ordering holds across
        # the two spellings rather than one key always shadowing the other.
        with workspace(model="fake"):
            with (
                handle("model.call", _responder("outer-legacy")),
                handle("model.call.requested", _responder("inner-taught")),
            ):
                assert summarize(text="hello") == "inner-taught"
            with (
                handle("model.call.requested", _responder("outer-taught")),
                handle("model.call", _responder("inner-legacy")),
            ):
                assert summarize(text="hello") == "inner-legacy"


def _exec_task(source: str, extra_globals: dict | None = None):
    """Define a function via exec (source unavailable to inspect.getsource)."""
    namespace: dict = {"Annotated": Annotated, "GitRepo": GitRepo}
    namespace.update(extra_globals or {})
    exec(source, namespace)
    return namespace


class TestTriStateBodyClassification:
    def test_exec_defined_docstring_only_task_raises_loud(self) -> None:
        namespace = _exec_task('def ghost(text: str) -> str:\n    """Summarize the text."""\n')
        ghost = task(namespace["ghost"])
        assert ghost.metadata.body_ambiguous is True
        assert ghost.metadata.bodyless is False
        with (
            workspace(model="fake"),
            handle("model.call", _responder("never")),
            pytest.raises(AmbiguousTaskBody) as excinfo,
        ):
            ghost(text="hello")
        assert "importable .py file" in str(excinfo.value)

    def test_exec_defined_bodied_task_runs(self) -> None:
        namespace = _exec_task('def real(text: str) -> str:\n    """Doc."""\n    return text.upper()\n')
        real = task(namespace["real"])
        assert real.metadata.body_ambiguous is False
        assert real.metadata.bodyless is False
        with workspace(model="fake"):
            assert real(text="ok") == "OK"

    def test_exec_defined_return_none_task_raises_loud_not_silent(self) -> None:
        # Docstring-only and `return None` compile byte-identically; the
        # deliberate no-op raising loud is the accepted cost of never running a
        # delegating body to a silent None (2132 W3 rationale).
        namespace = _exec_task('def noop(text: str) -> str:\n    """Doc."""\n    return None\n')
        noop = task(namespace["noop"])
        assert noop.metadata.body_ambiguous is True

    def test_ambiguous_handle_annotated_gets_placement_refusal(self) -> None:
        # B2 ordering: the refusal names workspace.run(...) — which works — rather
        # than the introspection error.
        namespace = _exec_task(
            "def ghost_repo(repo: Annotated[GitRepo, 'ReadWrite'], goal: str) -> str:\n"
            '    """Implement the goal in the repo."""\n'
        )
        ghost_repo = task(namespace["ghost_repo"])
        with workspace(model="fake"), pytest.raises(AmbientWorldAccessRefused):
            ghost_repo(repo=".", goal="login")

    def test_file_defined_bodyless_task_still_delivers(self) -> None:
        # The classifier change must not disturb the ordinary import path.
        assert summarize.metadata.bodyless is True
        assert summarize.metadata.body_ambiguous is False
        with workspace(model="fake"), handle("model.call", _responder("fine")):
            assert summarize(text="hello") == "fine"


class TestWorkspaceReentry:
    def test_idle_reconfiguration_replaces_instead_of_trapping(self) -> None:
        # The notebook idiom: re-running a cell constructs a fresh model object,
        # which fails the identity-based config match.
        workspace(model=object())
        second_model = object()
        ws = workspace(model=second_model)
        assert ws.model is second_model
        assert current_workspace() is not None
        assert current_workspace().model is second_model

    def test_same_config_reentry_shares_scope_unchanged(self) -> None:
        model = object()
        first = workspace(model=model)
        second = workspace(model=model)
        assert second._root_owner is first._root_owner

    def test_reconfiguration_during_active_run_refuses(self) -> None:
        @task
        def sneaky() -> str:
            """Reconfigure mid-run."""
            workspace(model=object())
            return "should-not-get-here"

        workspace(model=object())
        run = sneaky.detailed()
        assert run.outcome.__class__.__name__ == "Failed"
        assert "task run is active" in str(run.outcome.message)
