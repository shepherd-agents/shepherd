"""End-to-end non-degenerate `apply` through the production verb (T1 W2.4 xv / H6).

The vcs-core seal-lifecycle harness produces sequentially-accumulating siblings, so its "apply"
coverage exercises the D2 refusal path. This test drives the *disjoint* three-way apply the verb
exists for, using genuinely parallel candidates forked from one basis (the shape the W4 best-of-N
capstone also needs): select A (parent advances), then apply B onto the advanced parent.

`apply` is internal SPI today (no `ShepherdWorkspace.apply` facade until T1 task #10), so this
drives `mg.apply_retained_output(...)` through the validated settlement request the dialect facade
will later call.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow  # full-lifecycle suite: runs in the lifecycle-tests CI job

_EXAMPLES = Path(__file__).resolve().parents[4] / "examples" / "workspace-handles"
if str(_EXAMPLES) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES))

from _support import copy_git_repo, demo_workspace, seed_selected_workspace
from vcs_core.testing import read_world_workspace_file

from shepherd_dialect.workspace_control.workspace import (
    _validated_retained_run_output_settlement_request,
)


def _read_world_file(ws, world_oid: str, path: str) -> bytes | None:
    """Read a workspace file from a published world (the working dir is not materialized on disk)."""
    return read_world_workspace_file(ws.mg._world_storage(), world_oid, path)


# A path-parameterized candidate task: parallel candidates that write DIFFERENT files are disjoint,
# unlike the examples' fixed-`candidate.txt` propose task.
_PATH_TASK_ID = "tests.apply_e2e.write_path"
_PATH_TASK_SOURCE = """
from shepherd_runtime.nucleus import GitRepo


def write_path(repo: GitRepo, path: str, text: str):
    repo.write(path, text.encode())
    return {"path": path}
"""


def _register_path_task(workspace) -> None:
    workspace.tasks.register_source(
        task_id=_PATH_TASK_ID,
        module="tests_apply_e2e_tasks",
        source_text=_PATH_TASK_SOURCE,
        entrypoint="write_path",
        may_default="ReadWrite",
    )


def _apply(ws, output):
    request = _validated_retained_run_output_settlement_request(ws, output)
    return ws.mg.apply_retained_output(request.handle, parent=request.parent, binding=request.binding)


def test_disjoint_apply_after_select_merges_both_deltas() -> None:
    with demo_workspace(None, keep=False) as ws:
        _register_path_task(ws)
        repo = seed_selected_workspace(ws)
        task = ws.tasks.task(_PATH_TASK_ID)

        run_a = task.run(repo=copy_git_repo(repo), args={"path": "a.txt", "text": "A"}, placement="advisory")
        run_b = task.run(repo=copy_git_repo(repo), args={"path": "b.txt", "text": "B"}, placement="advisory")
        run_c = task.run(repo=copy_git_repo(repo), args={"path": "a.txt", "text": "C"}, placement="advisory")
        out_a, out_b, out_c = run_a.output(), run_b.output(), run_c.output()

        # Select A: the parent advances past B's and C's fork basis.
        ws.select(out_a)

        # Apply B: disjoint delta (b.txt) three-way-merges onto the advanced parent (a.txt).
        result = _apply(ws, out_b.refresh())
        assert result.settlement.action == "applied"
        # Non-degenerate: the merged head is a fresh revision, not the candidate head.
        assert result.settlement.applied_head != result.settlement.candidate_head

        # State reads back through the applied query branch.
        assert out_b.refresh().state == "applied"

        # The applied world now carries BOTH deltas (read from the world, not the unmaterialized dir).
        applied_world = result.parent_world_after
        assert _read_world_file(ws, applied_world, "a.txt") == b"A"
        assert _read_world_file(ws, applied_world, "b.txt") == b"B"

        # Consume-once: an applied output is no longer unconsumed, so neither a second apply nor a
        # select may proceed (refused at the settlement-request guard, before touching custody).
        with pytest.raises(Exception, match=r"unconsumed|already settled"):
            _apply(ws, out_b.refresh())
        with pytest.raises(Exception, match=r"unconsumed|already settled"):
            ws.select(out_b.refresh())

        # D2: applying C (which also wrote a.txt) overlaps the parent delta and fails closed.
        with pytest.raises(Exception, match="overlap"):
            _apply(ws, out_c.refresh())


def _application_plan_descriptor() -> dict[str, object]:
    return {
        "schema": "shepherd.permission-plan.v1",
        "fallback": "refuse",
        "assignments": [
            {
                "monitor": "carrier_check_at_commit",
                "timing": "commit",
                "completeness_basis": "exact_tree_diff",
                "tamper_basis": "content_addressed_store",
                "route": "retained_output_application",
                "evidence": {
                    "effective_match_digest": "m" * 8,
                    "authority_surface_plan_digest": "p" * 8,
                },
            }
        ],
    }


def _apply_with_authority(ws, output, decide):
    from vcs_core._permission_plan_evidence import permission_plan_digest

    descriptor = _application_plan_descriptor()
    request = _validated_retained_run_output_settlement_request(ws, output)
    return ws.mg.apply_retained_output(
        request.handle,
        parent=request.parent,
        binding=request.binding,
        decide=decide,
        effective_match_digest="m" * 8,
        authority_surface_plan_digest="p" * 8,
        permission_plan_digest=permission_plan_digest(descriptor),
        permission_plan_descriptor=descriptor,
    )


def test_authority_denied_apply_publishes_no_world_and_writes_no_receipt() -> None:
    """T1 W2.4(xiv): a denied decision fails the apply closed BEFORE publication."""
    with demo_workspace(None, keep=False) as ws:
        _register_path_task(ws)
        repo = seed_selected_workspace(ws)
        task = ws.tasks.task(_PATH_TASK_ID)

        run_a = task.run(repo=copy_git_repo(repo), args={"path": "a.txt", "text": "A"}, placement="advisory")
        run_b = task.run(repo=copy_git_repo(repo), args={"path": "b.txt", "text": "B"}, placement="advisory")
        out_b = run_b.output()
        ws.select(run_a.output())
        world_before = ws.git_repo().basis.world_oid

        with pytest.raises(Exception, match="application denied by authority"):
            _apply_with_authority(ws, out_b.refresh(), decide=lambda request: "denied")

        assert ws.git_repo().basis.world_oid == world_before  # no world published
        assert out_b.refresh().state == "unconsumed"  # no receipt written; output still settleable

        # The output remains genuinely settleable after the denial (fail-closed, not consumed).
        result = _apply_with_authority(ws, out_b.refresh(), decide=lambda request: "allowed")
        assert result.settlement.action == "applied"


def test_authority_allowed_apply_records_application_evidence() -> None:
    """T1 W2.4(xiv): an allowed decision records D7 authority evidence on receipt + result."""
    with demo_workspace(None, keep=False) as ws:
        _register_path_task(ws)
        repo = seed_selected_workspace(ws)
        task = ws.tasks.task(_PATH_TASK_ID)

        run_a = task.run(repo=copy_git_repo(repo), args={"path": "a.txt", "text": "A"}, placement="advisory")
        run_b = task.run(repo=copy_git_repo(repo), args={"path": "b.txt", "text": "B"}, placement="advisory")
        out_b = run_b.output()
        ws.select(run_a.output())

        result = _apply_with_authority(ws, out_b.refresh(), decide=lambda request: "allowed")
        assert result.settlement.action == "applied"
        assert result.authority_outcome == "allowed"
        assert result.authority_operation_id
        assert result.authority_settlement_operation_id
        assert result.settlement.authority_operation_id == result.authority_operation_id
        assert result.settlement.authority_outcome == "allowed"
        assert out_b.refresh().state == "applied"


def test_degenerate_apply_on_unmoved_parent_records_application_world() -> None:
    with demo_workspace(None, keep=False) as ws:
        _register_path_task(ws)
        repo = seed_selected_workspace(ws)
        task = ws.tasks.task(_PATH_TASK_ID)

        run = task.run(repo=copy_git_repo(repo), args={"path": "only.txt", "text": "ONE"}, placement="advisory")
        output = run.output()

        # Parent unmoved since fork basis: apply degenerates to the candidate head but still records
        # an application settlement (T1 D1a), never a selection.
        result = _apply(ws, output)
        assert result.settlement.action == "applied"
        assert result.settlement.applied_head == result.settlement.candidate_head
        assert output.refresh().state == "applied"
        assert _read_world_file(ws, result.parent_world_after, "only.txt") == b"ONE"
