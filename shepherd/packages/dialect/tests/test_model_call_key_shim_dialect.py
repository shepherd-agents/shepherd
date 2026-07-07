"""B3.1 dual-key shim, dialect nucleus twin (Bug 1 — 2132 W0.1).

The dialect quickstart nucleus carries its own responder registry and a bare
``"model.call"`` dispatch (``nucleus.py`` ``deliver()``). The taught
``handle("model.call.requested", ...)`` spelling must resolve here too, or the
quickstart lane reintroduces the exact key drift the shim closes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from shepherd_dialect.nucleus import deliver, handle, reset_workspace_for_tests, task, workspace

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


@dataclass(frozen=True)
class Reply:
    text: str


def _run_delivering_task() -> str:
    @task
    def uses_deliver() -> Reply:
        """Deliver a typed value via the model seam."""
        return deliver(Reply, goal="say hi")

    outcome = uses_deliver.detailed().outcome
    assert outcome.__class__.__name__ == "Finished", f"unexpected outcome: {outcome}"
    return outcome.value.text


def test_taught_spelling_resolves_in_dialect_nucleus(tmp_path) -> None:
    workspace(model=Model("m"), root=str(tmp_path))

    def responder(request: object) -> object:
        del request
        return {"result": {"text": "mocked"}}

    with handle("model.call.requested", responder):
        assert _run_delivering_task() == "mocked"


def test_legacy_spelling_still_resolves_in_dialect_nucleus(tmp_path) -> None:
    workspace(model=Model("m"), root=str(tmp_path))

    def responder(request: object) -> object:
        del request
        return {"result": {"text": "legacy"}}

    with handle("model.call", responder):
        assert _run_delivering_task() == "legacy"


def test_innermost_wins_across_mixed_spellings(tmp_path) -> None:
    workspace(model=Model("m"), root=str(tmp_path))

    def make(answer: str):
        def responder(request: object) -> object:
            del request
            return {"result": {"text": answer}}

        return responder

    with handle("model.call", make("outer-legacy")), handle("model.call.requested", make("inner-taught")):
        assert _run_delivering_task() == "inner-taught"
