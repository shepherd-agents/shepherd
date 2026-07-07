"""The signature-directed placement refusal (B2 — dispatch side of the P-030 fence).

An ambient call of a handle-annotated bodyless task refuses loudly, keyed on
the annotation (never the passed value), before any handler/provider dispatch.
Covers every ambient spelling — ``task(...)``, ``task.run(...)``, and
``task.detailed(...)`` — including the with-provider case, which is the
standing regression gate any future ambient servicer must keep green: without
the refusal, a reachable provider converts grant erasure into a confidently
fabricated report of world work (banked probes
``260706-mdp-probe-ambient-grant-erasure.py`` and
``260706-probe-run-spelling-erasure.py``, flipped to regressions here).
"""

from __future__ import annotations

from typing import Annotated

import pytest
from shepherd_core.schema import SINGLE_OUTPUT_KEY
from shepherd_runtime.effects import handle
from shepherd_runtime.nucleus import (
    AmbientWorldAccessRefused,
    Finished,
    GitRepo,
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


def _capturing_responder(captured: list[ModelRequest]):
    def responder(request: ModelRequest) -> ModelResponse:
        captured.append(request)
        return ModelResponse(structured_output={SINGLE_OUTPUT_KEY: "FAKE-ANSWER"})

    return responder


@task
def implement(repo: Annotated[GitRepo, "ReadWrite"], feature: str) -> str:
    """Implement the feature in the repo and report what changed."""


@task
def summarize(text: str) -> str:
    """Summarize the text."""


class TestHandleAnnotatedBodylessRefuses:
    """The hero's exact shape refuses — with a working provider installed."""

    def test_direct_call_refuses_with_provider_installed(self) -> None:
        captured: list[ModelRequest] = []
        with (
            workspace(model="fake"),
            handle("model.call", _capturing_responder(captured)),
            pytest.raises(AmbientWorldAccessRefused) as excinfo,
        ):
            implement(repo=".", feature="login")
        assert "declares world access" in str(excinfo.value)
        assert "workspace.run(" in str(excinfo.value)
        assert captured == []  # refused BEFORE any handler/provider dispatch

    def test_run_spelling_refuses_with_provider_installed(self) -> None:
        # The banked .run()-spelling probe, flipped: previously fabricated
        # Finished(value='FAKE-ANSWER-VIA-RUN'); now refuses (V8).
        captured: list[ModelRequest] = []
        with (
            workspace(model="fake"),
            handle("model.call", _capturing_responder(captured)),
            pytest.raises(AmbientWorldAccessRefused),
        ):
            implement.run(repo=".", feature="login")
        assert captured == []

    def test_detailed_spelling_refuses(self) -> None:
        with workspace(model="fake"), pytest.raises(AmbientWorldAccessRefused):
            implement.detailed(repo=".", feature="login")

    def test_refuses_without_any_provider(self) -> None:
        # The message beats DeliveryFailed: the user learns the placement rule,
        # not just that no handler was installed.
        with workspace(model="fake"), pytest.raises(AmbientWorldAccessRefused):
            implement(repo=".", feature="login")

    def test_refusal_keys_on_annotation_not_value(self) -> None:
        # The hero passes repo="." — a plain string. The refusal names the
        # annotation's noun regardless of the passed value.
        with workspace(model="fake"), pytest.raises(AmbientWorldAccessRefused) as excinfo:
            implement(repo=".", feature="login")
        assert "'GitRepo'" in str(excinfo.value)
        assert "'repo'" in str(excinfo.value)

    def test_handle_typed_return_slot_also_refuses(self) -> None:
        @task
        def produce_repo(description: str) -> tuple[GitRepo, str]:
            """Produce a repo for the description."""

        with workspace(model="fake"), pytest.raises(AmbientWorldAccessRefused) as excinfo:
            produce_repo(description="x")
        assert "return slot" in str(excinfo.value)


class TestUnaffectedShapes:
    def test_pure_value_bodyless_still_delivers(self) -> None:
        captured: list[ModelRequest] = []
        with workspace(model="fake"), handle("model.call", _capturing_responder(captured)):
            result = summarize(text="hello world")
        assert result == "FAKE-ANSWER"
        assert len(captured) == 1

    def test_bodied_task_with_handle_params_untouched(self) -> None:
        @task
        def bodied_review(repo: Annotated[GitRepo, "ReadWrite"], note: str) -> str:
            """Review with a body — the sanctioned in-process dev column."""
            return f"reviewed:{note}"

        assert bodied_review.metadata.bodyless is False
        with workspace(model="fake"):
            run = bodied_review.detailed(repo=".", note="ok")
        assert isinstance(run.outcome, Finished)
        assert run.unwrap() == "reviewed:ok"
