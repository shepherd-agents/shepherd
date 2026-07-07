"""B2 twin-site verification: the dialect nucleus ambient lane CANNOT reach the
bodyless fabrication shape (the recorded can't-reach verification).

The dialect nucleus (`shepherd_dialect.nucleus`) is a parallel delivery
implementation (plan V7): its own ``DeliveryFailed``, its own ``deliver()``
dispatching bare ``"model.call"``. The B2 refusal was therefore specified for
both nuclei OR a recorded, executed verification that the dialect ambient
task-call lane cannot reach a bodyless handle-annotated delivery.

This file is that verification, kept executed so it becomes a standing
tripwire: the dialect ``TaskCallable`` always executes the actual function
body — there is no bodyless-delivery branch — so a docstring-only task returns
``None`` without ever dispatching ``model.call``. The dialect ``deliver()`` is
an *in-body* verb the body must call explicitly. If the dialect nucleus ever
grows an ambient bodyless delivery lane, the assertions here fail and force
the twin refusal to be built (plan 260706-1210 §4 B2, r3 escape hatch).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

import pytest

# Runtime import by design: the task signature's annotation namespace needs the
# noun at definition/classification time, not only for type checking.
from shepherd_runtime.nucleus import GitRepo  # noqa: TC002

from shepherd_dialect.nucleus import handle, reset_workspace_for_tests, task, workspace

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


def test_dialect_ambient_lane_cannot_reach_bodyless_delivery(tmp_path) -> None:
    workspace(model=Model("m"), root=str(tmp_path))
    dispatched: list[object] = []

    def responder(request: object) -> object:
        dispatched.append(request)
        return {"result": "FAKE-ANSWER"}

    @task
    def implement(repo: Annotated[GitRepo, "ReadWrite"], feature: str) -> str:
        """Implement the feature in the repo and report what changed."""

    with handle("model.call", responder):
        run = implement.detailed(repo=".", feature="login")

    # The docstring-only body executed and returned None: no bodyless delivery
    # lane exists, so no model.call dispatch and no fabricated typed result.
    assert dispatched == []
    assert run.outcome.value is None


def test_dialect_deliver_is_in_body_only() -> None:
    # deliver() is the in-body verb: reachable only when a body explicitly
    # calls it. A bodyless (docstring-only) dialect task never invokes it.
    from shepherd_dialect import nucleus

    assert hasattr(nucleus, "deliver")
    import inspect

    signature = inspect.signature(nucleus.deliver)
    assert "goal" in signature.parameters  # in-body verb shape, not task lowering
