"""User-program composition over the current public handle floor."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

import pytest
from shepherd_runtime.nucleus import GitRepo, GitRepoBasis
from vcs_core import FilesystemSubstrate, MarkerSubstrate, Store, VcsCore, build_builtin_substrate_context
from vcs_core.runtime_substrate import TaskTraceSubstrateDriver
from vcs_core.testing import read_world_workspace_file

from shepherd_dialect.run_driver import ShepherdRunDriver
from shepherd_dialect.workspace_control import (
    RunOutput,
    ShepherdRunLedgerDriver,
    ShepherdTaskArtifactDriver,
    ShepherdTaskLedgerDriver,
    ShepherdWorkspace,
    WorkspaceControlError,
)
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
from shepherd_runtime.nucleus import GitRepo


def propose(repo: GitRepo, label: str, score: int, accepted: bool = False):
    status = "accepted" if accepted else "rejected"
    repo.write("candidate.txt", f"{score}:{label}:{status}\\n".encode())
    return {"label": label, "score": score, "accepted": accepted}
""",
        encoding="utf-8",
    )
    sys.modules.pop("sample_tasks", None)
    monkeypatch.syspath_prepend(str(tmp_path))
    return "sample_tasks:propose"


def _write_path_candidate_task_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A candidate task that writes a per-label path, so parallel candidates are path-disjoint.

    The fixed-``candidate.txt`` ``propose`` task above produces candidates that all touch the same
    path (fine for select/release/discard); ``apply`` needs genuinely disjoint deltas to exercise
    the three-way merge rather than the D2 refusal.
    """
    module_path = tmp_path / "path_tasks.py"
    module_path.write_text(
        """
from shepherd_runtime.nucleus import GitRepo


def propose_path(repo: GitRepo, label: str, score: int):
    repo.write(f"{label}.txt", f"{score}:{label}\\n".encode())
    return {"label": label, "score": score}
""",
        encoding="utf-8",
    )
    sys.modules.pop("path_tasks", None)
    monkeypatch.syspath_prepend(str(tmp_path))
    return "path_tasks:propose_path"


def _seed_selected_workspace(workspace: ShepherdWorkspace) -> GitRepo:
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


def _read_world_file(workspace: ShepherdWorkspace, world_oid: str, path: str) -> bytes | None:
    """Read a workspace file from a published world (settlement does not materialize the dir)."""
    return read_world_workspace_file(workspace.mg._world_storage(), world_oid, path)


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
            workspace.runs.outputs(run_ref=losers[0].owner.run_id, state="released")[0].output_id == losers[0].output_id
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


@pytest.mark.slow
def test_best_of_n_applies_a_disjoint_loser_onto_the_selected_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-of-N in the settlement spelling, but keeping a runner-up by *applying* it.

    Where the select-based best-of-N discards the losers, this user program selects the winner
    and then *applies* a path-disjoint runner-up onto the (now advanced) parent — the
    three-way settlement that ``select`` (fast-forward-only) cannot do. Whole-output, consume-once,
    reviewed by changeset. This is the apply-shaped companion to
    ``test_best_of_n_is_plain_user_code_over_run_outputs``.
    """
    source = _write_path_candidate_task_module(tmp_path, monkeypatch)
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)
        task = workspace.tasks.task("path_tasks.propose_path")

        # Parallel candidates forked from one basis, each writing a disjoint path.
        runs = [
            task.run(repo=_copy_git_repo(repo), args={"label": label, "score": score}, placement="advisory")
            for label, score in (("alpha", 10), ("winner", 99), ("keep", 42))
        ]
        outputs = [run.output() for run in runs]

        # Review by changeset: each candidate's whole-delta is exactly its own path.
        for output, label in zip(outputs, ("alpha", "winner", "keep"), strict=True):
            assert tuple(output.changeset().stat().changed_paths) == (f"{label}.txt",)

        by_label = dict(zip(("alpha", "winner", "keep"), outputs, strict=True))
        winner, keep, drop = by_label["winner"], by_label["keep"], by_label["alpha"]

        # Select the winner: the parent advances past every candidate's fork basis.
        selection = workspace.select(winner)
        assert selection.settlement.action == "selected"
        advanced = _assert_selected_git_repo(workspace)

        # Apply a disjoint runner-up onto the advanced parent. `select` would fail closed on the
        # drifted basis; `apply` three-way-merges because keep.txt and winner.txt are disjoint.
        application = workspace.apply(keep.refresh())
        assert application.settlement.action == "applied"
        # Non-degenerate: the merged head is a fresh revision, not the candidate head.
        assert application.settlement.applied_head != application.settlement.candidate_head
        assert keep.refresh().state == "applied"

        # The workspace now carries BOTH the selected winner and the applied runner-up.
        merged = _assert_selected_git_repo(workspace)
        assert merged.basis.world_oid != advanced.basis.world_oid
        assert _read_world_file(workspace, merged.basis.world_oid, "winner.txt") == b"99:winner\n"
        assert _read_world_file(workspace, merged.basis.world_oid, "keep.txt") == b"42:keep\n"

        # Consume-once: an applied output cannot be re-settled.
        with pytest.raises(WorkspaceControlError, match=r"unconsumed|already settled"):
            workspace.apply(keep.refresh())
        with pytest.raises(WorkspaceControlError, match=r"unconsumed|already settled"):
            workspace.select(keep.refresh())

        # The still-unsettled loser remains settleable by the ordinary verbs.
        assert workspace.runs.outputs(run_ref=drop.owner.run_id, state="unconsumed")[0].output_id == drop.output_id
        assert workspace.discard(drop.refresh()).settlement.action == "discarded"
    finally:
        workspace.close()
