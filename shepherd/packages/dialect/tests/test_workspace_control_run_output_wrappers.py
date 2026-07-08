from __future__ import annotations

import pytest
from shepherd2.schemas.run_outputs import (
    RunOutputOwner,
)
from support.workspace_control import FakeRuns, FakeWorkspace, RunRecordStub, run_output_ref

from shepherd_dialect.workspace_control.changesets import Changeset
from shepherd_dialect.workspace_control.errors import WorkspaceControlError
from shepherd_dialect.workspace_control.identities import RunRef
from shepherd_dialect.workspace_control.run_handles import WorkspaceRun
from shepherd_dialect.workspace_control.run_outputs import RunOutput


def test_run_output_wrapper_refresh_inspect_read_and_hydrate_delegate_through_workspace() -> None:
    stale_ref = run_output_ref()
    fresh_ref = run_output_ref(state="selected", settlement_ref="settlement-1")
    workspace = FakeWorkspace(runs=FakeRuns(output_refs=(fresh_ref,), shown={}))
    output = RunOutput(workspace, stale_ref)

    refreshed = output.refresh()
    inspected = output.inspect()
    read = output.read_file("candidate.txt")
    git_repo = output.as_readonly_git_repo()

    assert refreshed.state == "selected"
    assert inspected["state"] == "selected"
    assert read == (b"read:candidate.txt\n", 0o100644)
    assert git_repo.binding == "workspace"
    assert git_repo.basis.world_oid == "world-out"
    assert git_repo.basis.head == "candidate-head-1"
    assert git_repo.authority == frozenset({"read"})
    assert workspace.mg.file_reads == [("child", "candidate.txt")]
    assert len(workspace.mg.handoff_calls) == 3
    with pytest.raises(WorkspaceControlError, match="relative POSIX path"):
        output.read_file("../candidate.txt")


def test_changeset_wrapper_is_readonly_and_uses_custody_refreshed_output_state() -> None:
    stale_ref = run_output_ref()
    fresh_ref = run_output_ref(state="released", settlement_ref="settlement-1")
    workspace = FakeWorkspace(runs=FakeRuns(output_refs=(fresh_ref,), shown={}))
    output = RunOutput(workspace, stale_ref)
    changeset = output.changeset()

    assert isinstance(changeset, Changeset)
    assert changeset.output is output
    assert changeset.state == "unconsumed"
    assert changeset.changed_paths == ("candidate.txt",)
    assert changeset.read_file("candidate.txt") == (b"read:candidate.txt\n", 0o100644)
    assert changeset.stat().state == "released"
    assert changeset.inspect()["output"]["state"] == "released"
    assert changeset.refresh().state == "released"
    assert not hasattr(changeset, "select")
    assert not hasattr(changeset, "release")
    assert not hasattr(changeset, "discard")


def test_narrowed_changeset_inspect_stat_and_property_apply_same_path_filter() -> None:
    output_ref = run_output_ref(changed_paths=("backend/candidate.py", "docs/guide.md"))
    workspace = FakeWorkspace(runs=FakeRuns(output_refs=(output_ref,), shown={}))
    output = RunOutput(workspace, output_ref)

    whole = output.changeset()
    backend = whole.narrowed_to_binding(name="backend", root="backend/")
    docs = whole.narrowed_to_binding(name="docs", root="docs/")
    frontend = whole.narrowed_to_binding(name="frontend", root="frontend/")

    assert whole.changed_paths == ("backend/candidate.py", "docs/guide.md")
    assert backend.changed_paths == ("backend/candidate.py",)
    assert backend.stat().changed_paths == ("backend/candidate.py",)
    assert backend.inspect()["changed_paths"] == ["backend/candidate.py"]
    assert backend.inspect()["binding"] == "backend"
    assert docs.inspect()["changed_paths"] == ["docs/guide.md"]
    assert frontend.changed_paths == ()
    assert frontend.stat().changed_paths == ()
    assert frontend.inspect()["changed_paths"] == []


def test_run_output_wrapper_rejects_non_run_or_external_outputs_before_custody_reads() -> None:
    retained_query = RunOutput(
        FakeWorkspace(runs=FakeRuns(output_refs=(), shown={})),
        run_output_ref(owner=RunOutputOwner(kind="retained-query")),
    )
    external = RunOutput(
        FakeWorkspace(runs=FakeRuns(output_refs=(), shown={})),
        run_output_ref(materialization_kind="external"),
    )

    with pytest.raises(WorkspaceControlError, match="run-owned"):
        retained_query.inspect()
    with pytest.raises(WorkspaceControlError, match="run-owned"):
        retained_query.read_file("candidate.txt")
    with pytest.raises(WorkspaceControlError, match="run-owned"):
        retained_query.as_readonly_git_repo()
    with pytest.raises(WorkspaceControlError, match="tree materialization"):
        external.as_readonly_git_repo()


def test_run_output_settlement_methods_delegate_to_owning_workspace() -> None:
    workspace = FakeWorkspace(runs=FakeRuns(output_refs=(), shown={}))
    output = RunOutput(workspace, run_output_ref())

    assert output.select() == ("select", output.output_id)
    assert output.release() == ("release", output.output_id)
    assert output.discard() == ("discard", output.output_id)
    assert workspace.settlements == [
        ("select", output.output_id),
        ("release", output.output_id),
        ("discard", output.output_id),
    ]


def test_workspace_run_wrapper_delegates_outputs_refresh_and_json_projection() -> None:
    output_ref = run_output_ref()
    current_record = RunRecordStub(run_ref="run-1", status="retained")
    refreshed_record = RunRecordStub(run_ref="run-1", status="released")
    workspace = FakeWorkspace(
        runs=FakeRuns(
            output_refs=(output_ref,),
            shown={"run-1": refreshed_record},
        )
    )
    run = WorkspaceRun(workspace, current_record)

    assert run.run_ref == "run-1"
    assert run.ref == RunRef(id="run-1")
    assert run.status == "retained"
    assert run.output().output_id == output_ref.identity.output_id
    assert run.changeset().output_id == output_ref.identity.output_id
    outputs = run.outputs
    assert set(outputs) == {"workspace"}
    assert outputs["workspace"].output_id == output_ref.identity.output_id
    assert run.refresh().record == refreshed_record
    assert run.to_json()["record"] == current_record.to_json()
    assert "workspace" in run.to_json()["outputs"]

    with pytest.raises(WorkspaceControlError, match="no recorded authority context"):
        run.authority()
    with pytest.raises(WorkspaceControlError, match="no recorded authority context"):
        run.output().run_authority()
    with pytest.raises(WorkspaceControlError, match="no recorded authority context"):
        run.output().settlement_policy()
    with pytest.raises(WorkspaceControlError, match="no output named"):
        run.output("other")
