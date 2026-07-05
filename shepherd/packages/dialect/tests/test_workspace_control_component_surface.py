from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

import pytest
from shepherd_runtime.nucleus import GitRepo

from shepherd_dialect import workspace_control
from shepherd_dialect.workspace_control import May, ReadOnly, ReadWrite, RunStartError, ShepherdWorkspace, TaskRef
from shepherd_dialect.workspace_control.changesets import Changeset
from shepherd_dialect.workspace_control.run_handles import WorkspaceRun
from shepherd_dialect.workspace_control.run_outputs import RunOutput
from shepherd_dialect.workspace_control.task_handles import WorkspaceTask


@dataclass(frozen=True)
class _DefinitionStub:
    payload: dict[str, object]

    def to_json(self) -> dict[str, object]:
        return dict(self.payload)


@dataclass
class _TasksStub:
    definition: object | None
    requested: list[TaskRef] = field(default_factory=list)

    def get(self, task_ref: TaskRef) -> object | None:
        self.requested.append(task_ref)
        return self.definition


@dataclass
class _WorkspaceStub:
    tasks: _TasksStub
    run_result: object
    run_calls: list[dict[str, object]] = field(default_factory=list)

    def run(self, task_ref: TaskRef, **kwargs: object) -> object:
        self.run_calls.append({"task_ref": task_ref, **kwargs})
        return self.run_result


def test_public_workspace_control_authority_symbols_are_importable() -> None:
    assert May is Annotated
    assert ReadOnly.label == "ReadOnly"
    assert ReadOnly.mutates is False
    assert ReadWrite.label == "ReadWrite"
    assert ReadWrite.mutates is None


def test_public_floor_absence_guards_are_component_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not hasattr(workspace_control, "best_of_n")
    assert not hasattr(workspace_control, "gather")
    assert not hasattr(ShepherdWorkspace, "best_of_n")
    assert not hasattr(ShepherdWorkspace, "gather")
    assert not hasattr(GitRepo, "write")
    assert not hasattr(GitRepo, "apply")
    assert not hasattr(GitRepo, "run")
    assert not hasattr(WorkspaceTask, "best_of_n")
    assert not hasattr(WorkspaceTask, "gather")
    assert not hasattr(WorkspaceRun, "select")
    assert not hasattr(WorkspaceRun, "release")
    assert not hasattr(WorkspaceRun, "discard")
    assert not hasattr(RunOutput, "apply")
    assert not hasattr(Changeset, "apply")
    assert not hasattr(Changeset, "select")
    assert not hasattr(Changeset, "release")
    assert not hasattr(Changeset, "discard")

    # P-030 v0.2 fence: path-scoped GitRepo grants are not part of the claim. GitRepoPath is not a
    # public facade symbol, and a public May[...] carrying a path-scoped clause is refused at the
    # runtime seam regardless of how it was spelled (guilty-until-cleared, like best_of_n/apply).
    assert not hasattr(workspace_control, "GitRepoPath")
    from shepherd_dialect.workspace_control.authority import (
        GitRepoGrant,
        GitRepoGrantClause,
        GitRepoGrantDescriptor,
        gitrepo_grant_descriptor_from_may_annotation,
    )

    for path_grant in (
        GitRepoGrant(path_prefix="src/app"),
        GitRepoGrantDescriptor(
            grant_ref="signature:repo",
            clauses=(GitRepoGrantClause(binding_ref="workspace", path_prefix="src/app"),),
        ),
    ):
        with pytest.raises(ValueError, match=r"not part of the P-030 v0\.2 claim"):
            gitrepo_grant_descriptor_from_may_annotation(May[GitRepo, path_grant], grant_ref="signature:repo")

    workspace = ShepherdWorkspace(object())
    assert not hasattr(workspace.runs, "start_authority_workspace_run")
    monkeypatch.delenv("SHEPHERD_ENABLE_FENCED_RUN_START", raising=False)
    with pytest.raises(RunStartError, match=r"runs\.start is fenced"):
        workspace.runs.start("sample_tasks.fix_bug")


def test_workspace_task_handle_delegates_definition_json_and_run() -> None:
    definition = _DefinitionStub({"task_id": "sample_tasks.fix_bug", "version": "v1"})
    run_result = object()
    workspace = _WorkspaceStub(tasks=_TasksStub(definition), run_result=run_result)
    task = WorkspaceTask(workspace, "sample_tasks.fix_bug")  # type: ignore[arg-type]
    repo = object()

    assert task.task_ref == "sample_tasks.fix_bug"
    assert task.ref == TaskRef("sample_tasks.fix_bug")
    assert task.definition is definition
    assert task.to_json() == {
        "task_ref": "sample_tasks.fix_bug",
        "definition": {"task_id": "sample_tasks.fix_bug", "version": "v1"},
    }
    assert task.run(repo=repo, args={"issue": "parser"}, may="ReadWrite", placement="advisory") is run_result
    assert workspace.tasks.requested == [TaskRef("sample_tasks.fix_bug"), TaskRef("sample_tasks.fix_bug")]
    assert workspace.run_calls == [
        {
            "task_ref": TaskRef("sample_tasks.fix_bug"),
            "repo": repo,
            "bindings": None,  # LC-2: WorkspaceTask.run delegates bindings= for multi-binding parity
            "args": {"issue": "parser"},
            "may": "ReadWrite",
            "placement": "advisory",
            "runtime": None,
        }
    ]


def test_workspace_task_missing_definition_json_is_component_coverage() -> None:
    workspace = _WorkspaceStub(tasks=_TasksStub(None), run_result=object())
    task = WorkspaceTask(workspace, "sample_tasks.missing")  # type: ignore[arg-type]

    assert task.definition is None
    assert task.to_json() == {"task_ref": "sample_tasks.missing", "definition": None}
