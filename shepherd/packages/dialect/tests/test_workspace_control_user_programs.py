"""User-program composition over the current public handle floor."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

import pytest
from shepherd_runtime.nucleus import GitRepo, GitRepoBasis
from vcs_core import FilesystemSubstrate, MarkerSubstrate, Store, VcsCore, build_builtin_substrate_context
from vcs_core.runtime_substrate import TaskTraceSubstrateDriver

from shepherd_dialect.run_driver import ShepherdRunDriver
from shepherd_dialect.workspace_control import (
    RunOutput,
    ShepherdRunLedgerDriver,
    ShepherdTaskArtifactDriver,
    ShepherdTaskLedgerDriver,
    ShepherdWorkspace,
    WorkspaceControlError,
)
from shepherd_dialect.workspace_control.feature_flags import _seal_and_select_enabled
from shepherd_dialect.workspace_control.gitrepo_handles import same_git_binding_state

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.workspace_scenario


def _make_workspace(root: Path) -> ShepherdWorkspace:
    root.mkdir(parents=True, exist_ok=True)
    store = Store(str(root / ".vcscore"))
    context = build_builtin_substrate_context(store=store, workspace=root, config={"backend": "clonefile"})
    mg = VcsCore(
        str(root),
        substrates=[
            MarkerSubstrate(context),
            FilesystemSubstrate(context),
            TaskTraceSubstrateDriver(),
            ShepherdTaskLedgerDriver(),
            ShepherdTaskArtifactDriver(),
            ShepherdRunLedgerDriver(),
            ShepherdRunDriver(),
        ],
        store=store,
    )
    with _seal_and_select_enabled():
        mg.activate()
    return ShepherdWorkspace(
        mg,
        trace_store_path=root / ".vcscore" / "shepherd" / "trace.sqlite",
        workspace_path=root,
    )


def _write_candidate_task_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    module_path = tmp_path / "sample_tasks.py"
    module_path.write_text(
        """
def propose(repo, label: str, score: int, accepted: bool = False):
    status = "accepted" if accepted else "rejected"
    repo.write("candidate.txt", f"{score}:{label}:{status}\\n".encode())
    return {"label": label, "score": score, "accepted": accepted}
""",
        encoding="utf-8",
    )
    sys.modules.pop("sample_tasks", None)
    monkeypatch.syspath_prepend(str(tmp_path))
    return "sample_tasks:propose"


def _seed_selected_workspace(workspace: ShepherdWorkspace) -> GitRepo:
    with _seal_and_select_enabled():
        workspace.mg.exec("filesystem", "write", scope=workspace.mg.ground, path="base.txt", content=b"base\n")
    return _assert_selected_git_repo(workspace)


def _assert_selected_git_repo(
    workspace: ShepherdWorkspace,
    *,
    expected_head_basis: GitRepoBasis | None = None,
) -> GitRepo:
    git_repo = workspace.git_repo()
    selected = workspace.mg.read_selected_binding_revision_with_head("workspace")
    assert selected is not None
    assert git_repo.binding == "workspace"
    assert git_repo.basis.world_oid == workspace.mg.world_oid()
    assert git_repo.basis.store_id == selected.store_id
    assert git_repo.basis.resource_id == selected.resource_id
    assert git_repo.basis.head == selected.head
    assert git_repo.authority == frozenset({"read", "write"})
    if expected_head_basis is not None:
        assert same_git_binding_state(git_repo.basis, expected_head_basis)
    return git_repo


def _assert_readonly_git_repo(output: RunOutput) -> GitRepo:
    git_repo = output.as_readonly_git_repo()
    assert git_repo.binding == "workspace"
    assert git_repo.basis.world_oid == output.output_world_oid
    assert git_repo.basis.store_id == output.store_id
    assert git_repo.basis.resource_id == output.resource_id
    assert git_repo.basis.head == output.identity.candidate_head
    assert git_repo.authority == frozenset({"read"})
    return git_repo


def _copy_git_repo(repo: GitRepo) -> GitRepo:
    return GitRepo.from_payload(json.loads(json.dumps(repo.to_payload())))


def _candidate_text(output: RunOutput) -> str:
    value = output.changeset().read_file("candidate.txt")
    assert value is not None
    return value[0].decode("utf-8")


@pytest.mark.slow
def test_best_of_n_is_plain_user_code_over_run_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_candidate_task_module(tmp_path, monkeypatch)
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)
        task = workspace.tasks.task("sample_tasks.propose")

        runs = [
            task.run(repo=_copy_git_repo(repo), args={"label": label, "score": score}, placement="advisory")
            for label, score in (("alpha", 10), ("winner", 99), ("omega", 20))
        ]
        outputs = [run.output() for run in runs]
        rendered = {output.output_id: _candidate_text(output) for output in outputs}
        winner = max(outputs, key=lambda output: int(rendered[output.output_id].split(":", maxsplit=1)[0]))
        losers = [output for output in outputs if output.output_id != winner.output_id]
        winner_repo = _assert_readonly_git_repo(winner)

        selection = workspace.select(winner)

        assert selection.settlement.action == "selected"
        selected_repo = _assert_selected_git_repo(workspace, expected_head_basis=winner_repo.basis)
        assert same_git_binding_state(selected_repo.basis, winner_repo.basis)
        assert selected_repo.basis.world_oid != winner_repo.basis.world_oid

        for loser in losers:
            (still_unconsumed,) = workspace.runs.outputs(run_ref=loser.owner.run_id, state="unconsumed")
            assert still_unconsumed.output_id == loser.output_id
            assert _candidate_text(still_unconsumed) == rendered[loser.output_id]

        release = workspace.release(losers[0].refresh())
        discard = workspace.discard(losers[1].refresh())

        assert release.settlement.action == "released"
        assert discard.settlement.action == "discarded"
        assert (
            workspace.runs.outputs(run_ref=losers[0].owner.run_id, state="released")[0].output_id
            == losers[0].output_id
        )
        assert (
            workspace.runs.outputs(run_ref=losers[1].owner.run_id, state="discarded")[0].output_id
            == losers[1].output_id
        )
        with pytest.raises(WorkspaceControlError, match="unconsumed"):
            workspace.select(winner.refresh())
    finally:
        workspace.close()


@pytest.mark.slow
def test_retry_until_acceptable_is_plain_user_code_over_explicit_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_candidate_task_module(tmp_path, monkeypatch)
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)
        task = workspace.tasks.task("sample_tasks.propose")

        rejected = task.run(
            repo=_copy_git_repo(repo),
            args={"label": "first", "score": 10, "accepted": False},
            placement="advisory",
        )
        rejected_output = rejected.output()
        assert _candidate_text(rejected_output) == "10:first:rejected\n"
        release = workspace.release(rejected_output)

        assert release.settlement.action == "released"
        assert same_git_binding_state(workspace.git_repo().basis, repo.basis)
        assert workspace.runs.outputs(run_ref=rejected.run_ref, state="released")[0].read_file("candidate.txt") == (
            b"10:first:rejected\n",
            0o100644,
        )

        accepted = task.run(
            repo=_copy_git_repo(repo),
            args={"label": "second", "score": 90, "accepted": True},
            placement="advisory",
        )
        accepted_output = accepted.output()
        accepted_repo = _assert_readonly_git_repo(accepted_output)
        assert _candidate_text(accepted_output) == "90:second:accepted\n"

        selection = workspace.select(accepted_output)

        assert selection.settlement.action == "selected"
        fresh_repo = _assert_selected_git_repo(workspace, expected_head_basis=accepted_repo.basis)
        with pytest.raises(WorkspaceControlError, match="current selected workspace binding state"):
            task.run(repo=repo, args={"label": "stale", "score": 1, "accepted": True}, placement="advisory")

        followup = task.run(
            repo=_copy_git_repo(fresh_repo),
            args={"label": "after-select", "score": 100, "accepted": True},
            placement="advisory",
        )
        assert _candidate_text(followup.output()) == "100:after-select:accepted\n"
    finally:
        workspace.close()
