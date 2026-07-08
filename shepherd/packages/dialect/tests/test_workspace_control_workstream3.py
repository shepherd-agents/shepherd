"""Workstream 3 public-facade enforcement bridge coverage."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from vcs_core import FilesystemSubstrate, MarkerSubstrate, Store, VcsCore, build_builtin_substrate_context
from vcs_core.runtime_api import native_jail_available
from vcs_core.runtime_substrate import TaskTraceSubstrateDriver

from shepherd_dialect.run_driver import ShepherdRunDriver
from shepherd_dialect.workspace_control import (
    RunStartError,
    ShepherdRunLedgerDriver,
    ShepherdTaskArtifactDriver,
    ShepherdTaskLedgerDriver,
    ShepherdWorkspace,
)

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


def _write_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    source_text: str,
    entrypoint: str,
) -> str:
    module_path = tmp_path / f"{module_name}.py"
    module_path.write_text(_source_with_gitrepo_import(source_text), encoding="utf-8")
    sys.modules.pop(module_name, None)
    monkeypatch.syspath_prepend(str(tmp_path))
    return f"{module_name}:{entrypoint}"


def _source_with_gitrepo_import(source_text: str) -> str:
    if "GitRepo" not in source_text:
        return source_text
    stripped = source_text.lstrip("\n")
    leading = source_text[: len(source_text) - len(stripped)]
    gitrepo_import = "from shepherd_runtime.nucleus import GitRepo\n"
    if stripped.startswith(gitrepo_import):
        return source_text
    return f"{leading}{gitrepo_import}{stripped}"


def _seed_selected_workspace(workspace: ShepherdWorkspace) -> Any:
    workspace.mg.exec("filesystem", "write", scope=workspace.mg.ground, path="base.txt", content=b"base\n")
    return workspace.git_repo()


def test_public_workspace_run_records_advisory_for_in_process_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_module(
        tmp_path,
        monkeypatch,
        "ws3_advisory_tasks",
        """
def fix_bug(repo: GitRepo):
    return repo
""",
        "fix_bug",
    )
    root = tmp_path / "ws"
    workspace = _make_workspace(root)
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        run = workspace.run("ws3_advisory_tasks.fix_bug", repo=repo, placement="advisory")
        record = workspace.runs.show(run.run_ref)

        assert record is not None
        assert record.enforcement == "advisory"
        assert record.to_json()["enforcement"] == "advisory"
        assert record.execution_evidence.requested_placement == "advisory"
        assert record.execution_evidence.resolved_placement == "advisory"
        assert record.execution_evidence.enforcement_basis == "explicit_advisory"
        assert record.task_executions[0].executor_kind == "in_process"
    finally:
        workspace.close()

    from click.testing import CliRunner

    from shepherd_dialect import cli

    monkeypatch.chdir(root)
    result = CliRunner().invoke(cli.main, ["run", "show", run.run_ref, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["enforcement"] == "advisory"


@pytest.mark.workspace_native_jail
def test_public_workspace_run_required_jail_fails_closed_without_containment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vcs_core._vcscore_runtime as vcscore_runtime

    source = _write_module(
        tmp_path,
        monkeypatch,
        "ws3_no_jail_tasks",
        """
def fix_bug(repo: GitRepo):
    raise AssertionError("task artifact should not execute without a jail")
""",
        "fix_bug",
    )
    monkeypatch.setattr(vcscore_runtime, "detect_containment_backend", lambda: None)
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        with pytest.raises(RunStartError, match="no jail-capable containment"):
            workspace.run("ws3_no_jail_tasks.fix_bug", repo=repo, placement="jail")

        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        assert record.enforcement == "jail"
        assert record.execution_evidence.requested_placement == "jail"
        assert record.execution_evidence.resolved_placement == "jail"
        assert record.execution_evidence.enforcement_basis == "launch_confined_attempted"
        assert record.task_executions[0].executor_kind == "confined_process"
        assert record.task_executions[0].metadata["launch_confined_attempted"] is True
        assert record.task_executions[0].status == "failed"
        assert record.task_executions[0].error is not None
        assert record.task_executions[0].error["type"] == "JailNotEstablished"
        assert "no jail-capable containment" in record.task_executions[0].error["message"]
        launch_policy = record.launch_context.settlement_policy
        assert launch_policy is not None
        enforcement = launch_policy["execution_enforcement"]
        assert isinstance(enforcement, dict)
        monitor_refusal = enforcement["monitor_refusal"]
        assert isinstance(monitor_refusal, dict)
        assert monitor_refusal["type"] == "JailNotEstablished"
        assert workspace.runs.outputs() == ()
    finally:
        workspace.close()


@pytest.mark.workspace_native_jail
@pytest.mark.skipif(not native_jail_available(), reason="native jail backend is not available on this host")
def test_public_workspace_run_confined_permissive_publishes_retained_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_module(
        tmp_path,
        monkeypatch,
        "ws3_confined_write_tasks",
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
        "fix_bug",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        run = workspace.run("ws3_confined_write_tasks.fix_bug", repo=repo, args={"issue": "w3"}, placement="jail")
        record = workspace.runs.show(run.run_ref)

        assert record is not None
        assert record.status == "retained"
        assert record.enforcement == "jail"
        assert record.execution_evidence.requested_placement == "jail"
        assert record.execution_evidence.resolved_placement == "jail"
        assert record.execution_evidence.enforcement_basis == "launch_confined_attempted"
        assert record.task_executions[0].executor_kind == "confined_process"
        assert record.task_executions[0].metadata["placement"] == "jail"
        assert record.task_executions[0].metadata["launch_confined_attempted"] is True
        assert run.output().read_file("candidate.txt") == (b"selected candidate: w3\n", 0o100644)
        assert run.changeset().read_file("candidate.txt") == (b"selected candidate: w3\n", 0o100644)
        selected = workspace.select(run.output())
        assert selected.settlement.action == "selected"
    finally:
        workspace.close()


@pytest.mark.workspace_native_jail
@pytest.mark.skipif(not native_jail_available(), reason="native jail backend is not available on this host")
def test_public_workspace_run_readonly_confined_read_only_task_publishes_empty_changeset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_module(
        tmp_path,
        monkeypatch,
        "ws3_readonly_success_tasks",
        """
def inspect(repo: GitRepo):
    return {"binding": repo.binding, "authority": repo.authority}
""",
        "inspect",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        run = workspace.run(
            "ws3_readonly_success_tasks.inspect",
            repo=repo,
            may="ReadOnly",
            placement="jail",
        )
        record = workspace.runs.show(run.run_ref)

        assert record is not None
        assert record.status == "retained"
        assert record.may_profile == "ReadOnly"
        assert record.enforcement == "jail"
        assert record.execution_evidence.requested_placement == "jail"
        assert record.execution_evidence.resolved_placement == "jail"
        assert record.execution_evidence.enforcement_basis == "launch_confined_attempted"
        assert record.execution_evidence.execution_descriptor == {
            "mode": "confined_process",
            "enforcement": "syscall_jail",
            "profile": "ReadOnly",
            "provider": "workspace-control-confined-task",
        }
        assert record.task_executions[0].executor_kind == "confined_process"
        assert record.task_executions[0].status == "completed"
        assert run.output().changed_paths == ()
        assert run.changeset().stat().changed_paths == ()
    finally:
        workspace.close()


@pytest.mark.workspace_native_jail
@pytest.mark.skipif(not native_jail_available(), reason="native jail backend is not available on this host")
def test_public_workspace_run_readonly_denies_direct_filesystem_write_at_jail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_module(
        tmp_path,
        monkeypatch,
        "ws3_direct_write_tasks",
        """
from pathlib import Path


def fix_bug(repo: GitRepo):
    Path(repo.root, "bypass.txt").write_text("bypass\\n", encoding="utf-8")
    return {"wrote": True}
""",
        "fix_bug",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        with pytest.raises(RunStartError, match=r"PermissionError|Operation not permitted|Read-only file system"):
            workspace.run("ws3_direct_write_tasks.fix_bug", repo=repo, may="ReadOnly", placement="jail")

        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        assert record.enforcement == "jail"
        assert record.execution_evidence.requested_placement == "jail"
        assert record.execution_evidence.resolved_placement == "jail"
        assert record.execution_evidence.enforcement_basis == "launch_confined_attempted"
        assert record.task_executions[0].executor_kind == "confined_process"
        assert record.task_executions[0].status == "failed"
        assert record.task_executions[0].error is not None
        assert "PermissionError" in record.task_executions[0].error["message"]
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "bypass.txt") is None
        assert workspace.runs.outputs() == ()
    finally:
        workspace.close()


@pytest.mark.workspace_native_jail
@pytest.mark.skipif(not native_jail_available(), reason="native jail backend is not available on this host")
def test_public_workspace_run_confined_linked_task_calls_refuse_before_advisory_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_source = _write_module(
        tmp_path,
        monkeypatch,
        "ws3_child_tasks",
        """
def repair(repo: GitRepo):
    return "child result"
""",
        "repair",
    )
    parent_source = _write_module(
        tmp_path,
        monkeypatch,
        "ws3_parent_tasks",
        """
from shepherd_dialect.workspace_control import current_task_context


def fix_bug(repo: GitRepo):
    return current_task_context().call_task("repair")
""",
        "fix_bug",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(child_source, may_default="ReadWrite")
        workspace.tasks.register(
            parent_source,
            may_default="ReadWrite",
            declared_dependencies={"repair": {"task_id": "ws3_child_tasks.repair", "selector": "active"}},
        )
        repo = _seed_selected_workspace(workspace)

        with pytest.raises(RunStartError, match="linked task dependencies"):
            workspace.run("ws3_parent_tasks.fix_bug", repo=repo, placement="jail")

        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        assert record.enforcement == "advisory"
        assert record.execution_evidence.requested_placement == "jail"
        assert record.execution_evidence.resolved_placement == "jail"
        assert record.execution_evidence.enforcement_basis == "prelaunch_advisory"
        assert record.task_executions == ()
    finally:
        workspace.close()


def test_public_workspace_run_prelaunch_serialization_failure_stays_advisory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_module(
        tmp_path,
        monkeypatch,
        "ws3_prelaunch_tasks",
        """
def fix_bug(repo: GitRepo, payload):
    return {"payload": str(payload)}
""",
        "fix_bug",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        with pytest.raises(RunStartError, match="not JSON serializable"):
            workspace.run("ws3_prelaunch_tasks.fix_bug", repo=repo, args={"payload": object()}, placement="jail")

        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        assert record.enforcement == "advisory"
        assert record.execution_evidence.requested_placement == "jail"
        assert record.execution_evidence.resolved_placement == "jail"
        assert record.execution_evidence.enforcement_basis == "prelaunch_advisory"
        assert record.task_executions[0].executor_kind == "confined_process"
        assert record.task_executions[0].metadata["placement"] == "jail"
        assert record.task_executions[0].metadata["launch_confined_attempted"] is False
    finally:
        workspace.close()


@pytest.mark.workspace_native_jail
@pytest.mark.skipif(not native_jail_available(), reason="native jail backend is not available on this host")
def test_public_workspace_run_confined_runner_entrypoint_is_not_shadowed_by_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_module(
        tmp_path,
        monkeypatch,
        "ws3_shadow_tasks",
        """
def fix_bug(repo: GitRepo):
    return {"trusted_worker": True}
""",
        "fix_bug",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        workspace.mg.exec(
            "filesystem",
            "write",
            scope=workspace.mg.ground,
            path="shepherd_dialect/workspace_control/_confined_task_runner.py",
            content=b"raise SystemExit(42)\n",
        )
        repo = workspace.git_repo()

        run = workspace.run("ws3_shadow_tasks.fix_bug", repo=repo, placement="jail")
        record = workspace.runs.show(run.run_ref)

        assert record is not None
        assert record.status == "retained"
        assert record.enforcement == "jail"
        assert record.execution_evidence.requested_placement == "jail"
        assert record.execution_evidence.resolved_placement == "jail"
        assert record.execution_evidence.enforcement_basis == "launch_confined_attempted"
        assert record.task_executions[0].metadata["launch_confined_attempted"] is True
    finally:
        workspace.close()


@pytest.mark.workspace_native_jail
@pytest.mark.skipif(not native_jail_available(), reason="native jail backend is not available on this host")
def test_public_workspace_run_auto_resolves_to_jail_on_jail_capable_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_module(
        tmp_path,
        monkeypatch,
        "ws3_auto_jail_tasks",
        """
def fix_bug(repo: GitRepo):
    return {"auto": True}
""",
        "fix_bug",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        run = workspace.run("ws3_auto_jail_tasks.fix_bug", repo=repo)
        record = workspace.runs.show(run.run_ref)

        assert record is not None
        assert record.enforcement == "jail"
        assert record.execution_evidence.requested_placement == "auto"
        assert record.execution_evidence.resolved_placement == "jail"
        assert record.execution_evidence.enforcement_basis == "launch_confined_attempted"
        assert record.task_executions[0].metadata["requested_placement"] == "auto"
        assert record.task_executions[0].metadata["resolved_placement"] == "jail"
    finally:
        workspace.close()


def test_public_workspace_run_auto_resolves_to_advisory_when_no_native_jail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shepherd_dialect.workspace_control.workspace as workspace_module

    monkeypatch.setattr(workspace_module, "native_jail_available", lambda: False)
    child_source = _write_module(
        tmp_path,
        monkeypatch,
        "ws3_auto_advisory_child_tasks",
        """
def repair(repo: GitRepo):
    return "child result"
""",
        "repair",
    )
    parent_source = _write_module(
        tmp_path,
        monkeypatch,
        "ws3_auto_advisory_parent_tasks",
        """
from shepherd_dialect.workspace_control import current_task_context


def fix_bug(repo: GitRepo):
    return {"child": current_task_context().call_task("repair")}
""",
        "fix_bug",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(child_source, may_default="ReadWrite")
        workspace.tasks.register(
            parent_source,
            may_default="ReadWrite",
            declared_dependencies={"repair": {"task_id": "ws3_auto_advisory_child_tasks.repair", "selector": "active"}},
        )
        repo = _seed_selected_workspace(workspace)

        run = workspace.run("ws3_auto_advisory_parent_tasks.fix_bug", repo=repo)
        record = workspace.runs.show(run.run_ref)

        assert record is not None
        assert record.status == "retained"
        assert record.enforcement == "advisory"
        assert record.execution_evidence.requested_placement == "auto"
        assert record.execution_evidence.resolved_placement == "advisory"
        assert record.execution_evidence.enforcement_basis == "auto_advisory"
        assert [execution.call_kind for execution in record.task_executions] == ["linked_call", "root_run"]
    finally:
        workspace.close()


def test_public_workspace_run_advisory_preserves_same_process_linked_task_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_source = _write_module(
        tmp_path,
        monkeypatch,
        "ws3_advisory_child_tasks",
        """
def repair(repo: GitRepo):
    return "child result"
""",
        "repair",
    )
    parent_source = _write_module(
        tmp_path,
        monkeypatch,
        "ws3_advisory_parent_tasks",
        """
from shepherd_dialect.workspace_control import current_task_context


def fix_bug(repo: GitRepo):
    return {"child": current_task_context().call_task("repair")}
""",
        "fix_bug",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(child_source, may_default="ReadWrite")
        workspace.tasks.register(
            parent_source,
            may_default="ReadWrite",
            declared_dependencies={"repair": {"task_id": "ws3_advisory_child_tasks.repair", "selector": "active"}},
        )
        repo = _seed_selected_workspace(workspace)

        run = workspace.run("ws3_advisory_parent_tasks.fix_bug", repo=repo, placement="advisory")
        record = workspace.runs.show(run.run_ref)

        assert record is not None
        assert record.status == "retained"
        assert record.enforcement == "advisory"
        assert record.execution_evidence.requested_placement == "advisory"
        assert record.execution_evidence.resolved_placement == "advisory"
        assert [execution.call_kind for execution in record.task_executions] == ["linked_call", "root_run"]
        assert {execution.metadata["placement"] for execution in record.task_executions} == {"advisory"}
    finally:
        workspace.close()


def test_shepherd_dialect_production_code_does_not_import_private_vcs_core_modules() -> None:
    source_root = Path("shepherd/packages/dialect/src/shepherd_dialect")
    offenders: list[str] = []
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("vcs_core._"):
                offenders.append(f"{path}:{node.lineno}:{node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("vcs_core._"):
                        offenders.append(f"{path}:{node.lineno}:{alias.name}")

    assert offenders == []
