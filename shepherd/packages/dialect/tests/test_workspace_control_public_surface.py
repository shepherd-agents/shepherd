"""Acceptance coverage for the current public workspace-control floor."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner
from shepherd_runtime.nucleus import GitRepo, GitRepoBasis
from vcs_core import (
    FilesystemSubstrate,
    InvalidRepositoryStateError,
    MarkerSubstrate,
    Store,
    VcsCore,
    build_builtin_substrate_context,
)
from vcs_core.runtime_substrate import TaskTraceSubstrateDriver

from shepherd_dialect import cli
from shepherd_dialect.run_driver import ShepherdRunDriver
from shepherd_dialect.workspace_control import (
    RUN_LEDGER_BINDING,
    RunAuthority,
    RunOutput,
    RunOutputSettlementEvidence,
    RunOutputSettlementPolicy,
    RunRef,
    RunStartError,
    ShepherdRunLedgerDriver,
    ShepherdTaskArtifactDriver,
    ShepherdTaskLedgerDriver,
    ShepherdWorkspace,
    TaskNotFoundError,
    TaskRef,
    WorkspaceControlError,
    WorkspaceRef,
    WorkspaceRun,
    WorkspaceTask,
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


def _write_task_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    module_path = tmp_path / "sample_tasks.py"
    module_path.write_text(
        """
from shepherd_runtime.nucleus import GitRepo


def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
        encoding="utf-8",
    )
    sys.modules.pop("sample_tasks", None)
    monkeypatch.syspath_prepend(str(tmp_path))
    return "sample_tasks:fix_bug"


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
    assert isinstance(git_repo, GitRepo)
    assert git_repo.binding == "workspace"
    assert git_repo.basis.world_oid == workspace.mg.world_oid()
    assert git_repo.basis.store_id == selected.store_id
    assert git_repo.basis.resource_id == selected.resource_id
    assert git_repo.basis.head == selected.head
    assert git_repo.authority == frozenset({"read", "write"})
    if expected_head_basis is not None:
        assert same_git_binding_state(git_repo.basis, expected_head_basis)
    return git_repo


def _assert_readonly_git_repo(output: RunOutput, expected: GitRepo | None = None) -> GitRepo:
    git_repo = output.as_readonly_git_repo()
    assert isinstance(git_repo, GitRepo)
    assert git_repo.binding == "workspace"
    assert git_repo.basis.world_oid == output.output_world_oid
    assert git_repo.basis.store_id == output.store_id
    assert git_repo.basis.resource_id == output.resource_id
    assert git_repo.basis.head == output.identity.candidate_head
    assert git_repo.authority == frozenset({"read"})
    if expected is not None:
        assert git_repo == expected
    return git_repo


def _copy_git_repo(repo: GitRepo) -> GitRepo:
    return GitRepo.from_payload(json.loads(json.dumps(repo.to_payload())))


def _run_ledger_revision(workspace: ShepherdWorkspace) -> object:
    return json.loads(json.dumps(workspace.mg.read_selected_binding_revision(RUN_LEDGER_BINDING)))


@pytest.mark.slow
def test_public_workspace_run_keeps_run_ledger_keyed_and_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(tmp_path, monkeypatch)
    root = tmp_path / "ws"
    workspace = _make_workspace(root)
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        runs = tuple(
            workspace.run(
                "sample_tasks.fix_bug",
                repo=_copy_git_repo(repo),
                args={"issue": issue},
                placement="advisory",
            )
            for issue in ("parser", "renderer", "docs")
        )

        manifest = _run_ledger_revision(workspace)
        assert isinstance(manifest, dict)
        assert manifest["storage_shape"] == "keyed-json-tree"
        assert manifest["record_count"] == len(runs)
        assert manifest["latest_run_ref"] == runs[-1].run_ref
        assert "runs" not in manifest
        for run in runs:
            path = f"data/runs/by-ref/{run.run_ref[:2]}/{run.run_ref}.json"
            row = workspace.mg.read_selected_binding_json_entry(RUN_LEDGER_BINDING, path)
            assert row is not None
            assert row["run_ref"] == run.run_ref
            assert row["status"] == run.status
            assert "outputs" in row
    finally:
        workspace.close()


@pytest.mark.slow
def test_public_handle_surface_select_reacquire_run_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(tmp_path, monkeypatch)
    root = tmp_path / "ws"
    workspace = _make_workspace(root)
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo1 = _seed_selected_workspace(workspace)
        task_ref = TaskRef("sample_tasks.fix_bug")
        task = workspace.tasks.task(task_ref)

        assert isinstance(workspace.ref, WorkspaceRef)
        assert isinstance(task, WorkspaceTask)
        assert task.ref == task_ref
        same_ref_task = workspace.tasks.task(task_ref)
        assert same_ref_task.ref == task.ref
        assert same_ref_task is not task
        assert same_ref_task != task
        assert task.definition == workspace.tasks.get(task_ref)
        assert task.to_json()["task_ref"] == task_ref.id
        run1 = task.run(repo=_copy_git_repo(repo1), args={"issue": "first"}, placement="advisory")

        assert isinstance(run1, WorkspaceRun)
        assert run1.ref == RunRef(id=run1.run_ref)
        assert run1.status == run1.record.status
        refreshed_run1 = run1.refresh()
        assert refreshed_run1.ref == run1.ref
        assert refreshed_run1 is not run1
        assert refreshed_run1 != run1
        assert workspace.runs.show(run1.ref) == run1.record
        assert workspace.runs.show(run1.run_ref[:8]) == run1.record
        assert workspace.runs.show(RunRef(id=run1.run_ref[:8])) is None
        run_authority = run1.authority()
        assert isinstance(run_authority, RunAuthority)
        assert run_authority.run_ref == run1.run_ref
        assert run_authority.task_default_may == "ReadWrite"
        assert run_authority.requested_may is None
        assert run_authority.effective_may == run1.record.authority_context.effective_may
        assert run_authority.effective_grant_digest == run1.record.authority_context.effective_grant_digest
        assert run_authority.effective_match_digest == run1.record.authority_context.effective_match_digest
        assert (
            run_authority.authority_surface_plan_digest == run1.record.authority_context.authority_surface_plan_digest
        )
        assert run_authority.classifier_policy == run1.record.authority_context.classifier_policy
        assert run_authority.to_json()["effective_grant_digest"] == run_authority.effective_grant_digest

        output1 = run1.output()
        output_authority = output1.run_authority()
        output_policy = output1.settlement_policy()
        output_evidence = output1.settlement_evidence()
        assert run1.outputs["workspace"].output_id == output1.output_id
        assert output_authority == run_authority
        assert isinstance(output_policy, RunOutputSettlementPolicy)
        assert isinstance(output_evidence, RunOutputSettlementEvidence)
        assert output_policy.output_id == output1.output_id
        assert output_evidence.output_id == output1.output_id
        assert output_policy.run_ref == run1.run_ref
        assert output_evidence.run_ref == run1.run_ref
        assert output_policy.state == "unconsumed"
        assert output_evidence.state == "unconsumed"
        assert output_evidence.settlement_action is None
        assert output_policy.consume_once is True
        assert output_policy.custody_owner == "vcs-core.retained-output"
        assert output_policy.settlement_verbs == ("select", "apply", "release", "discard")
        assert output_policy.authority == run_authority
        assert output_policy.settlement_policy is not None
        execution_enforcement = output_policy.settlement_policy["execution_enforcement"]
        assert isinstance(execution_enforcement, dict)
        execution_descriptor = run1.record.execution_evidence.execution_descriptor
        assert execution_descriptor is not None
        assert execution_enforcement["mode"] == execution_descriptor["mode"]
        assert execution_enforcement["provider"] == execution_descriptor["provider"]
        if execution_descriptor["mode"] == "confined_process":
            assert execution_enforcement["established_monitor"] == execution_enforcement["requested_monitor"]
        else:
            assert execution_enforcement["established_monitor"] is None
        assert output_policy.to_json()["authority"]["effective_match_digest"] == run_authority.effective_match_digest
        assert output1.inspect()["state"] == "unconsumed"
        assert output1.read_file("candidate.txt") == (b"selected candidate: first\n", 0o100644)
        assert run1.changeset().read_file("candidate.txt") == (b"selected candidate: first\n", 0o100644)
        assert run1.changeset().stat().changed_paths == ("candidate.txt",)
        assert run1.to_json()["outputs"]["workspace"]["state"] == "unconsumed"
        retained_repo1 = _assert_readonly_git_repo(output1)
        ledger_before_select = _run_ledger_revision(workspace)

        selection = workspace.select(output1)

        assert selection.settlement.action == "selected"
        assert workspace.mg.read_selected_binding_revision(RUN_LEDGER_BINDING) == ledger_before_select
        assert output1.refresh().state == "selected"
        assert output1.settlement_policy().state == "selected"
        selected_evidence = output1.settlement_evidence()
        assert selected_evidence.state == "selected"
        assert selected_evidence.settlement_action == "selected"
        assert selected_evidence.authority_operation_id == selection.authority_operation_id
        assert selected_evidence.permission_plan_digest
        assert run1.to_json()["outputs"]["workspace"]["state"] == "selected"
        repo2 = _assert_selected_git_repo(workspace, expected_head_basis=retained_repo1.basis)
        assert repo2.basis.world_oid != retained_repo1.basis.world_oid
        assert not same_git_binding_state(repo1.basis, repo2.basis)
        with pytest.raises(WorkspaceControlError, match="current selected workspace binding state"):
            task.run(repo=repo1, args={"issue": "stale"}, placement="advisory")

        run2 = task.run(repo=_copy_git_repo(repo2), args={"issue": "second"}, placement="advisory")
        output2 = run2.output()
        retained_repo2 = _assert_readonly_git_repo(output2)
        assert output2.read_file("candidate.txt") == (b"selected candidate: second\n", 0o100644)
        run1_ref = run1.run_ref
        run2_ref = run2.run_ref
    finally:
        workspace.close()

    reopened = _make_workspace(root)
    try:
        (selected_output,) = reopened.runs.outputs(run_ref=run1_ref, state="selected")
        assert selected_output.read_file("candidate.txt") == (b"selected candidate: first\n", 0o100644)
        _assert_readonly_git_repo(selected_output, retained_repo1)
        (unconsumed_output,) = reopened.runs.outputs(run_ref=run2_ref, state="unconsumed")
        assert unconsumed_output.read_file("candidate.txt") == (b"selected candidate: second\n", 0o100644)
        _assert_readonly_git_repo(unconsumed_output, retained_repo2)
    finally:
        reopened.close()


def test_public_workspace_task_missing_and_draft_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(tmp_path, monkeypatch)
    workspace = _make_workspace(tmp_path / "ws")
    try:
        repo = _seed_selected_workspace(workspace)
        missing = workspace.tasks.task("sample_tasks.missing")
        assert isinstance(missing, WorkspaceTask)
        assert missing.definition is None
        assert missing.to_json() == {"task_ref": "sample_tasks.missing", "definition": None}
        with pytest.raises(TaskNotFoundError, match="no active task"):
            missing.run(repo=repo)

        draft = workspace.tasks.register(
            source,
            may_default="ReadWrite",
            declared_dependencies={"repair": {"task_id": "sample_tasks.repair", "selector": "active"}},
        )
        assert draft.status == "draft"
        draft_task = workspace.tasks.task(f"sample_tasks.fix_bug@{draft.version}")
        assert draft_task.definition == draft
        with pytest.raises(RunStartError, match="is draft"):
            draft_task.run(repo=repo, args={"issue": "draft"}, placement="advisory")
    finally:
        workspace.close()


@pytest.mark.slow
def test_public_cli_settlement_verbs_are_exact_and_stateful(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(tmp_path, monkeypatch)
    root = tmp_path / "ws"
    workspace = _make_workspace(root)
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        selected_before = _seed_selected_workspace(workspace)
        released = workspace.run(
            "sample_tasks.fix_bug",
            repo=selected_before,
            args={"issue": "release"},
            placement="advisory",
        )
        discarded = workspace.run(
            "sample_tasks.fix_bug",
            repo=selected_before,
            args={"issue": "discard"},
            placement="advisory",
        )
        selected = workspace.run(
            "sample_tasks.fix_bug",
            repo=selected_before,
            args={"issue": "select"},
            placement="advisory",
        )
        selected_output_repo = _assert_readonly_git_repo(selected.output())
        ledger_before_settlement = _run_ledger_revision(workspace)
    finally:
        workspace.close()

    monkeypatch.chdir(root)
    runner = CliRunner()

    latest_result = runner.invoke(cli.main, ["run", "release", "@latest"])
    assert latest_result.exit_code != 0
    assert "exact run identity" in latest_result.output
    prefix_result = runner.invoke(cli.main, ["run", "release", released.run_ref[:8]])
    assert prefix_result.exit_code != 0
    assert "exact run identity" in prefix_result.output
    missing_output_result = runner.invoke(cli.main, ["run", "release", released.run_ref, "--output-name", "missing"])
    assert missing_output_result.exit_code != 0
    assert "no output named 'missing'" in missing_output_result.output

    release_result = runner.invoke(cli.main, ["run", "release", released.run_ref, "--binding", "workspace"])
    discard_result = runner.invoke(cli.main, ["run", "discard", discarded.run_ref, "--binding", "workspace"])
    select_result = runner.invoke(cli.main, ["run", "select", selected.run_ref, "--binding", "workspace"])

    assert release_result.exit_code == 0, release_result.output
    assert discard_result.exit_code == 0, discard_result.output
    assert select_result.exit_code == 0, select_result.output
    assert json.loads(release_result.output)["settlement"]["action"] == "released"
    assert json.loads(discard_result.output)["settlement"]["action"] == "discarded"
    assert json.loads(select_result.output)["settlement"]["action"] == "selected"

    second_release = runner.invoke(cli.main, ["run", "release", released.run_ref])
    assert second_release.exit_code != 0
    assert "unconsumed" in second_release.output or "already settled" in second_release.output

    reader = _make_workspace(root)
    try:
        assert reader.mg.read_selected_binding_revision(RUN_LEDGER_BINDING) == ledger_before_settlement
        assert reader.runs.outputs(run_ref=released.run_ref, state="released")[0].output_name == "workspace"
        assert reader.runs.outputs(run_ref=discarded.run_ref, state="discarded")[0].output_name == "workspace"
        assert reader.runs.outputs(run_ref=selected.run_ref, state="selected")[0].output_name == "workspace"
        selected_after = _assert_selected_git_repo(reader, expected_head_basis=selected_output_repo.basis)
        assert not same_git_binding_state(selected_after.basis, selected_before.basis)
    finally:
        reader.close()


@pytest.mark.slow
def test_public_best_of_n_is_plain_user_code_over_handles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(tmp_path, monkeypatch)
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)
        task = workspace.tasks.task("sample_tasks.fix_bug")

        candidates = [
            task.run(repo=_copy_git_repo(repo), args={"issue": issue}, placement="advisory")
            for issue in ("alpha", "winner", "omega")
        ]
        outputs = [candidate.output() for candidate in candidates]
        rendered = {output.output_id: output.changeset().read_file("candidate.txt")[0].decode() for output in outputs}
        winner = next(output for output in outputs if "winner" in rendered[output.output_id])
        losers = [output for output in outputs if output.output_id != winner.output_id]
        winner_repo = _assert_readonly_git_repo(winner)

        released = workspace.release(losers[0])
        discarded = workspace.discard(losers[1])
        selected = workspace.select(winner)

        assert released.settlement.action == "released"
        assert discarded.settlement.action == "discarded"
        assert selected.settlement.action == "selected"
        released_output = workspace.runs.outputs(run_ref=candidates[0].run_ref, state="released")[0]
        discarded_output = workspace.runs.outputs(run_ref=candidates[2].run_ref, state="discarded")[0]
        assert released_output.output_id == losers[0].output_id
        assert discarded_output.output_id == losers[1].output_id
        assert workspace.runs.outputs(run_ref=candidates[1].run_ref, state="selected")[0].output_id == winner.output_id
        selected_repo = _assert_selected_git_repo(workspace, expected_head_basis=winner_repo.basis)
        assert same_git_binding_state(selected_repo.basis, winner_repo.basis)
    finally:
        workspace.close()


@pytest.mark.slow
def test_public_settlement_rejects_bare_forged_and_stale_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(tmp_path, monkeypatch)
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)
        run = workspace.run("sample_tasks.fix_bug", repo=repo, args={"issue": "guard"}, placement="advisory")
        output = run.output()

        with pytest.raises(WorkspaceControlError, match="RunOutput from this workspace"):
            workspace.release(output.ref)  # type: ignore[arg-type]

        foreign_workspace = _make_workspace(tmp_path / "foreign-ws")
        try:
            foreign_workspace.tasks.register(source, may_default="ReadWrite")
            foreign_repo = _seed_selected_workspace(foreign_workspace)
            foreign_run = foreign_workspace.run(
                "sample_tasks.fix_bug",
                repo=foreign_repo,
                args={"issue": "foreign"},
                placement="advisory",
            )
            with pytest.raises(WorkspaceControlError, match="this workspace"):
                workspace.release(foreign_run.output())
        finally:
            foreign_workspace.close()

        forged_descriptor = replace(output.descriptor, store_id="forged-store")
        forged = RunOutput(workspace, replace(output.ref, descriptor=forged_descriptor, store_id="forged-store"))
        with pytest.raises(InvalidRepositoryStateError, match="handle disagrees"):
            workspace.release(forged)
        with pytest.raises(InvalidRepositoryStateError, match="handle disagrees"):
            forged.changeset().inspect()
        with pytest.raises(InvalidRepositoryStateError, match="handle disagrees"):
            forged.run_authority()
        with pytest.raises(InvalidRepositoryStateError, match="handle disagrees"):
            forged.settlement_policy()

        selected = workspace.select(output)
        (selected_output,) = workspace.runs.outputs(run_ref=run.run_ref, state="selected")
        assert selected_output.settlement_ref == selected.settlement.settlement_ref
        with pytest.raises(WorkspaceControlError, match="unconsumed"):
            workspace.release(selected_output)
    finally:
        workspace.close()
