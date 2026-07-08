"""Workspace facade lifecycle ergonomics."""

from __future__ import annotations

import logging

import pytest

from shepherd_dialect.workspace_control import ShepherdWorkspace, WorkspaceControlError


class FakeMg:
    def __init__(self, *, close_error: Exception | None = None) -> None:
        self.close_error = close_error
        self.deactivate_calls = 0

    def deactivate(self) -> None:
        self.deactivate_calls += 1
        if self.close_error is not None:
            raise self.close_error


def test_workspace_context_manager_returns_self_and_closes() -> None:
    mg = FakeMg()
    ws = ShepherdWorkspace(mg)

    with ws as entered:
        assert entered is ws
        assert not ws.closed

    assert ws.closed
    assert mg.deactivate_calls == 1


def test_workspace_close_is_idempotent() -> None:
    mg = FakeMg()
    ws = ShepherdWorkspace(mg)

    ws.close()
    ws.close()

    assert ws.closed
    assert mg.deactivate_calls == 1


def test_workspace_context_closes_and_preserves_body_exception() -> None:
    mg = FakeMg()
    ws = ShepherdWorkspace(mg)

    with pytest.raises(ValueError, match="body failed"), ws:
        raise ValueError("body failed")

    assert ws.closed
    assert mg.deactivate_calls == 1


def test_workspace_context_close_failure_does_not_mask_body_exception(caplog: pytest.LogCaptureFixture) -> None:
    mg = FakeMg(close_error=RuntimeError("close failed"))
    ws = ShepherdWorkspace(mg)

    with (
        caplog.at_level(logging.WARNING, logger="shepherd_dialect.workspace_control.workspace"),
        pytest.raises(ValueError, match="body failed") as exc_info,
        ws,
    ):
        raise ValueError("body failed")

    assert ws.closed
    assert mg.deactivate_calls == 1
    assert "close() also failed" in "\n".join(getattr(exc_info.value, "__notes__", ()))
    assert "close() failed during exception cleanup" in caplog.text


def test_workspace_context_close_failure_without_body_exception_propagates() -> None:
    mg = FakeMg(close_error=RuntimeError("close failed"))
    ws = ShepherdWorkspace(mg)

    with pytest.raises(RuntimeError, match="close failed"), ws:
        pass

    assert ws.closed
    assert mg.deactivate_calls == 1


def test_closed_workspace_mutation_surfaces_reopen_remedy() -> None:
    ws = ShepherdWorkspace(FakeMg())
    ws.close()

    with pytest.raises(WorkspaceControlError, match=r"facade is closed.*reacquire"):
        ws.release(object())  # type: ignore[arg-type]
