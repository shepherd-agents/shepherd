"""Workspace-control core-loop coverage.

Tests that execute ``runs.start`` opt into its fenced compatibility entry point
explicitly. Enabled ``runs.start`` execution routes through the retained nucleus
spine; it is not evidence that the historical skeleton bridge is a normal
v1 launch spine.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
import vcs_core._vcscore_lifecycle as lifecycle
import vcs_core._vcscore_runtime as vcscore_runtime
from shepherd_runtime.nucleus import GitRepo, GitRepoBasis
from vcs_core import (
    FilesystemSubstrate,
    InvalidRepositoryStateError,
    MarkerSubstrate,
    Store,
    VcsCore,
    build_builtin_substrate_context,
)
from vcs_core._execution_capability import detect_containment_backend
from vcs_core.runtime_substrate import TaskTraceSubstrateDriver
from vcs_core.spi import DriverAuthorityRequiredError

from shepherd_dialect.run_driver import ShepherdRunDriver
from shepherd_dialect.workspace_control import (
    RUN_LEDGER_BINDING,
    TASK_LEDGER_BINDING,
    TASK_LEDGER_SCHEMA,
    DeclaredTaskDependency,
    May,
    ReadOnly,
    RunOutput,
    RunRecord,
    RunRef,
    RunRetainedCustody,
    RunStartError,
    RunTerminalization,
    ShepherdRunLedgerDriver,
    ShepherdTaskArtifactDriver,
    ShepherdTaskLedgerDriver,
    ShepherdWorkspace,
    TaskRef,
    TaskRegistrationError,
    WorkspaceControlError,
    WorkspaceRef,
    run_workspace_output_world_oid,
)
from shepherd_dialect.workspace_control import workspace as workspace_module
from shepherd_dialect.workspace_control._confined_task_executor import (
    ConfinedRootTaskProvider,
    ConfinedTaskExecutionError,
)
from shepherd_dialect.workspace_control.authority import _allow_path_prefix_grants
from shepherd_dialect.workspace_control.drivers import mint_ledger_write_authority
from shepherd_dialect.workspace_control.gitrepo_handles import same_git_binding_state

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.workspace_scenario


def _make_workspace(
    root: Path,
    *,
    explicit_trace_path: bool = True,
    trace_store_path_override: Path | None = None,
) -> ShepherdWorkspace:
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
    if trace_store_path_override is not None:
        # Point at a different/empty trace store (custody persists in vcs-core; the descriptor does not).
        trace_path: Path | None = trace_store_path_override
    elif explicit_trace_path:
        trace_path = root / ".vcscore" / "shepherd" / "trace.sqlite"
    else:
        # explicit_trace_path=False exercises the DEFAULT resolution (.vcscore/shepherd/trace.sqlite).
        trace_path = None
    return ShepherdWorkspace(mg, trace_store_path=trace_path, workspace_path=root)


def _authority_effects(history: Any) -> list[dict[str, object]]:
    return [
        commit.metadata
        for commit in history.commits
        if str(commit.metadata.get("type", "")).startswith(("Authority", "RetainedOutput", "Prepared"))
    ]


def _assert_execution_enforcement(record: RunRecord, **expected: object) -> dict[str, object]:
    assert record.launch_context.settlement_policy is not None
    enforcement = record.launch_context.settlement_policy["execution_enforcement"]
    assert isinstance(enforcement, dict)
    for key, value in expected.items():
        assert enforcement[key] == value
    return enforcement


def _write_task_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> str:
    return _write_module(tmp_path, monkeypatch, "sample_tasks", body, "fix_bug")


def _write_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    body: str,
    attr_name: str,
) -> str:
    module_path = tmp_path / f"{module_name}.py"
    module_path.write_text(_body_with_gitrepo_import(body), encoding="utf-8")
    sys.modules.pop(module_name, None)
    monkeypatch.syspath_prepend(str(tmp_path))
    return f"{module_name}:{attr_name}"


def _body_with_gitrepo_import(body: str) -> str:
    if "GitRepo" not in body:
        return body
    stripped = body.lstrip("\n")
    leading = body[: len(body) - len(stripped)]
    future = "from __future__ import annotations\n"
    gitrepo_import = "from shepherd_runtime.nucleus import GitRepo\n"
    if stripped.startswith(gitrepo_import):
        return body
    if stripped.startswith(future):
        rest = stripped[len(future) :]
        if rest.startswith(gitrepo_import):
            return body
        return f"{leading}{future}{gitrepo_import}{rest}"
    return f"{leading}{gitrepo_import}{stripped}"


# Legacy core-loop tests below still exercise task resolution and run-ledger behavior through
# the fenced compatibility start path. Product floor tests use ``workspace.run(..., repo=...)``.
def _start_fenced_run(
    workspace: ShepherdWorkspace,
    task_ref: str | TaskRef,
    **kwargs: Any,
) -> RunRecord:
    old_value = os.environ.get("SHEPHERD_ENABLE_FENCED_RUN_START")
    os.environ["SHEPHERD_ENABLE_FENCED_RUN_START"] = "1"
    try:
        kwargs.setdefault("placement", "advisory")
        return workspace.runs.start(task_ref, **kwargs)
    finally:
        if old_value is None:
            os.environ.pop("SHEPHERD_ENABLE_FENCED_RUN_START", None)
        else:
            os.environ["SHEPHERD_ENABLE_FENCED_RUN_START"] = old_value


def _assert_readonly_git_repo_for_output(output: RunOutput, expected: GitRepo | None = None) -> GitRepo:
    git_repo = output.as_readonly_git_repo()
    assert isinstance(git_repo, GitRepo)
    assert isinstance(git_repo.basis, GitRepoBasis)
    assert git_repo.binding == "workspace"
    assert git_repo.binding == output.binding
    assert git_repo.basis.world_oid == output.output_world_oid
    assert git_repo.basis.store_id == output.store_id
    assert git_repo.basis.resource_id == output.resource_id
    assert git_repo.basis.head == output.identity.candidate_head
    assert git_repo.authority == frozenset({"read"})
    assert git_repo.readonly() == git_repo
    if expected is not None:
        assert git_repo == expected
    return git_repo


def _assert_selected_git_repo_for_workspace(
    workspace: ShepherdWorkspace,
    expected_basis: GitRepoBasis | None = None,
    expected_head_basis: GitRepoBasis | None = None,
) -> GitRepo:
    git_repo = workspace.git_repo()
    selected = workspace.mg.read_selected_binding_revision_with_head("workspace")
    assert selected is not None
    assert isinstance(git_repo, GitRepo)
    assert isinstance(git_repo.basis, GitRepoBasis)
    assert git_repo.binding == "workspace"
    assert git_repo.basis.world_oid == workspace.mg.world_oid()
    assert git_repo.basis.store_id == selected.store_id
    assert git_repo.basis.resource_id == selected.resource_id
    assert git_repo.basis.head == selected.head
    assert git_repo.authority == frozenset({"read", "write"})
    assert git_repo.readonly().authority == frozenset({"read"})
    if expected_basis is not None:
        assert git_repo.basis == expected_basis
    if expected_head_basis is not None:
        assert git_repo.basis.store_id == expected_head_basis.store_id
        assert git_repo.basis.resource_id == expected_head_basis.resource_id
        assert git_repo.basis.head == expected_head_basis.head
    return git_repo


def _copy_git_repo(repo: GitRepo) -> GitRepo:
    return GitRepo.from_payload(json.loads(json.dumps(repo.to_payload())))


def _seed_selected_workspace(
    workspace: ShepherdWorkspace,
    *,
    path: str = "base.txt",
    content: bytes = b"base\n",
) -> GitRepo:
    workspace.mg.exec("filesystem", "write", scope=workspace.mg.ground, path=path, content=content)
    return _assert_selected_git_repo_for_workspace(workspace)


def test_task_register_publishes_versioned_selected_task_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"fixed: {issue}\\n".encode())
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        v1 = workspace.tasks.register(source, may_default="ReadOnly", metadata={"owner": "tests"})
        v2 = workspace.tasks.register(source, may_default="ReadWrite")

        assert v1.task_id == "sample_tasks.fix_bug"
        assert v1.version == "v1"
        assert v1.artifact_ref is not None
        assert v1.artifact_digest == v1.artifact_ref.artifact_digest
        assert v2.version == "v2"
        assert v2.status == "active"
        assert workspace.tasks.get("sample_tasks.fix_bug") == v2
        assert [item.version for item in workspace.tasks.list()] == ["v1", "v2"]
        assert [item.status for item in workspace.tasks.list()] == ["superseded", "active"]
        payload = workspace.mg.read_selected_binding_revision(TASK_LEDGER_BINDING)
        assert payload is not None
        assert payload["schema"] == TASK_LEDGER_SCHEMA
    finally:
        workspace.close()


def test_task_register_rejects_unsupported_may_default_before_ledger_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo):
    return repo
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        with pytest.raises(TaskRegistrationError, match="may='WriteOnly'"):
            workspace.tasks.register(source, may_default="WriteOnly")

        assert workspace.tasks.list() == ()
    finally:
        workspace.close()


def test_artifact_backed_task_versions_do_not_execute_live_import_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    raise RuntimeError(f"artifact-v1: {issue}")
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        v1 = workspace.tasks.register(source, may_default="ReadWrite")
        _write_task_module(
            tmp_path,
            monkeypatch,
            """
def fix_bug(repo: GitRepo, issue: str):
    raise RuntimeError(f"live-v2: {issue}")
""",
        )
        workspace.tasks.register(source, may_default="ReadWrite")

        with pytest.raises(RunStartError, match="artifact-v1: parser"):
            _start_fenced_run(workspace, f"sample_tasks.fix_bug@{v1.version}", args={"issue": "parser"})
    finally:
        workspace.close()


def test_sigterm_during_authority_run_is_caught_and_discards_not_orphans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a SIGTERM (`kill`/`docker stop`) mid-run on the real workspace-control run
    path is routed through the clean-discard path by the ``@terminate_as_interrupt()`` decorator
    on ``_execute_nucleus_runtime_run``, so the run leaves no orphaned operation to wedge the next.

    The body first asserts a SIGTERM handler is actually installed on this path, so if the
    decorator did not cover it the body fails loudly *instead of* the default SIGTERM disposition
    killing the test runner.
    """
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
import os
import signal


def fix_bug(repo: GitRepo, issue: str):
    handler = signal.getsignal(signal.SIGTERM)
    assert handler not in (signal.SIG_DFL, signal.SIG_IGN, None), "no SIGTERM handler on the run path"
    os.kill(os.getpid(), signal.SIGTERM)  # `kill` / `docker stop`, mid-run
    return "unreachable"
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        with pytest.raises(KeyboardInterrupt):
            workspace.runs._start_authority_workspace_run("sample_tasks.fix_bug", args={"issue": "parser"})
        assert workspace.mg.list_orphaned_operations() == ()  # no wedge for the next run
    finally:
        workspace.close()


def test_private_authority_workspace_run_merges_allowed_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    repo.write("candidate.txt", f"fixed: {issue}\\n".encode())
    return "body-completed"
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")

        record = workspace.runs._start_authority_workspace_run("sample_tasks.fix_bug", args={"issue": "parser"})

        assert record.status == "merged"
        assert record.provider == "shepherd.workspace_control.nucleus-authority.v0"
        assert record.terminalization == RunTerminalization(
            body_status="completed",
            world_disposition="merged",
            output_publication_status="not_applicable",
        )
        assert record.outputs == {}
        assert record.terminal_workspace_world_oid is not None
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "candidate.txt") == b"fixed: parser\n"
        assert record.operation_refs.runtime_operation is not None
        assert record.operation_refs.authority_operation is not None
        assert record.operation_refs.authority_settlement_operation is not None
        assert record.operation_refs.trace_head is not None
        assert record.operation_refs.runtime_value_ref is None

        history = workspace.mg.resolve_operation_history(
            record.operation_refs.authority_operation,
            scope=workspace.mg.ground,
        )
        decision = next(effect for effect in _authority_effects(history) if effect["type"] == "AuthorityDecision")
        assert decision["outcome"] == "allowed"
        assert decision["authority_context"]["shepherd"]["run_ref"] == record.run_ref
        assert decision["authority_context"]["shepherd"]["task_id"] == "sample_tasks.fix_bug"
        assert decision["authority_context"]["shepherd"]["may_profile"] == "ReadWrite"
        assert decision["authority_context"]["runtime_operation_id"] == record.operation_refs.runtime_operation

        settlement_history = workspace.mg.resolve_operation_history(
            record.operation_refs.authority_settlement_operation,
            scope=workspace.mg.ground,
        )
        settlement = next(
            effect for effect in _authority_effects(settlement_history) if effect["type"] == "AuthoritySettlement"
        )
        assert isinstance(settlement.get("parent_world_before"), str)
        assert settlement["parent_world_before"] != record.terminal_workspace_world_oid
        assert settlement["parent_world_after"] == record.terminal_workspace_world_oid
        assert workspace.mg.world_oid(workspace.mg.ground) != record.terminal_workspace_world_oid
        vcscore_projection = workspace.runs.vcscore(record.run_ref)
        assert vcscore_projection is not None
        assert vcscore_projection["authority_operation"] == record.operation_refs.authority_operation
        assert vcscore_projection["authority_settlement_operation"] == (
            record.operation_refs.authority_settlement_operation
        )
    finally:
        workspace.close()


def test_private_authority_workspace_run_denies_bypassed_readonly_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
from pathlib import Path


def fix_bug(repo: GitRepo, issue: str):
    Path(repo.root, "candidate.txt").write_text(f"bypassed: {issue}\\n", encoding="utf-8")
    return "body-completed"
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadOnly")

        with pytest.raises(RunStartError, match="authority denied"):
            workspace.runs._start_authority_workspace_run("sample_tasks.fix_bug", args={"issue": "parser"})

        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        assert record.provider == "shepherd.workspace_control.nucleus-authority.v0"
        assert record.error is not None
        assert record.error["type"] == "AuthorityDenied"
        assert record.error["stage"] == "authority_terminalization"
        assert record.error["outcome"] == "denied"
        assert record.terminalization == RunTerminalization(
            body_status="completed",
            world_disposition="discarded",
            output_publication_status="not_applicable",
        )
        assert record.terminal_workspace_world_oid is None
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "candidate.txt") is None
        assert record.operation_refs.runtime_operation is not None
        assert record.operation_refs.authority_operation is not None
        assert record.operation_refs.authority_settlement_operation is not None
        assert record.operation_refs.trace_head is not None

        settlement_history = workspace.mg.resolve_operation_history(
            record.operation_refs.authority_settlement_operation,
            scope=workspace.mg.ground,
        )
        settlement = next(
            effect for effect in _authority_effects(settlement_history) if effect["type"] == "AuthoritySettlement"
        )
        assert settlement["outcome"] == "denied"
        assert settlement["settlement"] == "discarded"
        assert settlement["authority_context"]["shepherd"]["run_ref"] == record.run_ref
        assert settlement["authority_context"]["shepherd"]["may_profile"] == "ReadOnly"
    finally:
        workspace.close()


def test_private_authority_workspace_run_recovers_settlement_failure_before_terminal_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    repo.write("candidate.txt", f"fixed after recovery: {issue}\\n".encode())
    return "body-completed"
""",
    )
    original_record_settlement = lifecycle._record_authority_final_settlement
    fail_next = True

    def fail_first_settlement(*args: object, **kwargs: object) -> None:
        nonlocal fail_next
        if fail_next:
            fail_next = False
            raise RuntimeError("simulated workspace authority settlement failure")
        original_record_settlement(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_record_authority_final_settlement", fail_first_settlement)
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")

        record = workspace.runs._start_authority_workspace_run("sample_tasks.fix_bug", args={"issue": "parser"})

        assert record.status == "merged"
        assert record.error is None
        assert record.terminalization == RunTerminalization(
            body_status="completed",
            world_disposition="merged",
            output_publication_status="not_applicable",
        )
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "candidate.txt") == (
            b"fixed after recovery: parser\n"
        )
        assert workspace.mg.list_authority_settlement_pending() == ()
        assert record.operation_refs.runtime_operation is not None
        assert record.operation_refs.authority_operation is not None
        assert record.operation_refs.authority_settlement_operation is not None
        settlement_history = workspace.mg.resolve_operation_history(
            record.operation_refs.authority_settlement_operation,
            scope=workspace.mg.ground,
        )
        settlement = next(
            effect for effect in _authority_effects(settlement_history) if effect["type"] == "AuthoritySettlement"
        )
        assert settlement["outcome"] == "allowed"
        assert settlement["settlement"] == "merged"
        assert settlement["authority_context"]["shepherd"]["run_ref"] == record.run_ref
        assert settlement["authority_context"]["runtime_operation_id"] == record.operation_refs.runtime_operation
        assert settlement["parent_world_after"] == record.terminal_workspace_world_oid
    finally:
        workspace.close()


def test_private_authority_workspace_run_recovers_pre_adoption_failure_from_final_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    repo.write("candidate.txt", f"fixed after pre-adoption recovery: {issue}\\n".encode())
    return "body-completed"
""",
    )
    original_begin = lifecycle._begin_lifecycle_run
    fail_next = True

    def fail_first_lifecycle_begin(*args: object, **kwargs: object) -> None:
        nonlocal fail_next
        if fail_next:
            fail_next = False
            raise RuntimeError("simulated authority lifecycle handoff failure")
        original_begin(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_begin_lifecycle_run", fail_first_lifecycle_begin)
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")

        record = workspace.runs._start_authority_workspace_run("sample_tasks.fix_bug", args={"issue": "parser"})

        assert record.status == "merged"
        assert record.error is None
        assert record.terminal_workspace_world_oid is not None
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "candidate.txt") == (
            b"fixed after pre-adoption recovery: parser\n"
        )
        assert workspace.mg.list_authority_settlement_pending() == ()
        assert record.operation_refs.runtime_operation is not None
        assert record.operation_refs.authority_operation is not None
        assert record.operation_refs.authority_settlement_operation is not None

        settlement_history = workspace.mg.resolve_operation_history(
            record.operation_refs.authority_settlement_operation,
            scope=workspace.mg.ground,
        )
        settlement = next(
            effect for effect in _authority_effects(settlement_history) if effect["type"] == "AuthoritySettlement"
        )
        assert settlement["outcome"] == "allowed"
        assert settlement["settlement"] == "merged"
        assert settlement["authority_context"]["runtime_operation_id"] == record.operation_refs.runtime_operation
        assert settlement["parent_world_after"] == record.terminal_workspace_world_oid
    finally:
        workspace.close()


def test_parent_task_can_register_before_child_then_activate_and_call_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_source = _write_module(
        tmp_path,
        monkeypatch,
        "parent_tasks",
        """
from shepherd_dialect.workspace_control import current_task_context


def fix_bug(repo: GitRepo, issue: str):
    child_result = current_task_context().call_task("repair", issue=issue)
    return repo.write("candidate.txt", child_result.encode())
""",
        "fix_bug",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        parent = workspace.tasks.register(
            parent_source,
            may_default="ReadWrite",
            declared_dependencies={"repair": {"task_id": "child_tasks.repair", "selector": "active"}},
        )

        assert parent.status == "draft"
        with pytest.raises(RunStartError, match="is draft"):
            _start_fenced_run(workspace, f"parent_tasks.fix_bug@{parent.version}", args={"issue": "parser"})

        child_source = _write_module(
            tmp_path,
            monkeypatch,
            "child_tasks",
            """
def repair(repo: GitRepo, issue: str):
    return f"child-v1: {issue}\\n"
""",
            "repair",
        )
        child = workspace.tasks.register(child_source, may_default="ReadWrite")
        activated_parent = workspace.tasks.activate(f"parent_tasks.fix_bug@{parent.version}")

        assert child.status == "active"
        assert activated_parent.status == "active"
        record = _start_fenced_run(
            workspace,
            "parent_tasks.fix_bug",
            args={"issue": "parser"},
            placement="advisory",
        )

        assert record.resolved_task_graph is not None
        assert record.resolved_task_graph.dependencies["repair"].task_id == "child_tasks.repair"
        assert [resolution.reason for resolution in record.task_resolutions] == ["run_start", "declared_alias"]
        assert record.task_resolutions[1].declared_alias == "repair"
        assert record.task_resolutions[1].task_lock.task_id == "child_tasks.repair"
        output_refs = workspace.runs.outputs(run_ref=record.run_ref)
        assert output_refs[0].identity.output_name == "workspace"
        assert output_refs[0].state == "unconsumed"
    finally:
        workspace.close()


def test_nested_declared_task_aliases_resolve_in_child_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grand_source = _write_module(
        tmp_path,
        monkeypatch,
        "grand_tasks",
        """
def finish(repo: GitRepo):
    return "grand-ok\\n"
""",
        "finish",
    )
    child_source = _write_module(
        tmp_path,
        monkeypatch,
        "nested_child_tasks",
        """
from shepherd_dialect.workspace_control import current_task_context


def repair(repo: GitRepo):
    return current_task_context().call_task("finish")
""",
        "repair",
    )
    parent_source = _write_module(
        tmp_path,
        monkeypatch,
        "nested_parent_tasks",
        """
from shepherd_dialect.workspace_control import current_task_context


def fix_bug(repo: GitRepo):
    result = current_task_context().call_task("repair")
    return repo.write("candidate.txt", result.encode())
""",
        "fix_bug",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(grand_source, may_default="ReadWrite")
        workspace.tasks.register(
            child_source,
            may_default="ReadWrite",
            declared_dependencies={"finish": {"task_id": "grand_tasks.finish", "selector": "active"}},
        )
        workspace.tasks.register(
            parent_source,
            may_default="ReadWrite",
            declared_dependencies={"repair": {"task_id": "nested_child_tasks.repair", "selector": "active"}},
        )

        record = _start_fenced_run(workspace, "nested_parent_tasks.fix_bug", placement="advisory")

        assert record.status == "retained"
        assert [resolution.declared_alias for resolution in record.task_resolutions] == [None, "repair", "finish"]
        assert [resolution.task_lock.task_id for resolution in record.task_resolutions] == [
            "nested_parent_tasks.fix_bug",
            "nested_child_tasks.repair",
            "grand_tasks.finish",
        ]
        assert record.task_resolutions[2].requester_task_id == "nested_child_tasks.repair"
    finally:
        workspace.close()


def test_task_context_can_dynamically_resolve_and_run_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_source = _write_module(
        tmp_path,
        monkeypatch,
        "dynamic_child_tasks",
        """
def repair(repo: GitRepo, issue: str):
    return f"dynamic-child: {issue}\\n"
""",
        "repair",
    )
    parent_source = _write_module(
        tmp_path,
        monkeypatch,
        "dynamic_parent_tasks",
        """
from shepherd_dialect.workspace_control import current_task_context


def fix_bug(repo: GitRepo, issue: str):
    context = current_task_context()
    resolution = context.resolve_task("dynamic_child_tasks.repair", reason="dynamic_lookup")
    result = context.run_task(resolution, issue=issue)
    return repo.write("candidate.txt", result.encode())
""",
        "fix_bug",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(child_source, may_default="ReadWrite")
        workspace.tasks.register(parent_source, may_default="ReadWrite")

        record = _start_fenced_run(
            workspace,
            "dynamic_parent_tasks.fix_bug",
            args={"issue": "parser"},
            placement="advisory",
        )

        assert record.status == "retained"
        assert [resolution.reason for resolution in record.task_resolutions] == ["run_start", "dynamic_lookup"]
        assert record.task_resolutions[1].requested_ref == "dynamic_child_tasks.repair"
        assert record.task_resolutions[1].requester_task_id == "dynamic_parent_tasks.fix_bug"
        assert record.task_resolutions[1].task_lock.task_id == "dynamic_child_tasks.repair"
    finally:
        workspace.close()


def test_declared_task_handle_fails_closed_on_in_run_task_library_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_source = _write_module(
        tmp_path,
        monkeypatch,
        "declared_child_tasks",
        """
def repair(repo: GitRepo, issue: str):
    return f"child-v1: {issue}"
""",
        "repair",
    )
    parent_source = _write_module(
        tmp_path,
        monkeypatch,
        "declared_parent_tasks",
        """
from shepherd_dialect.workspace_control import current_task_context


def fix_bug(repo: GitRepo, issue: str):
    ctx = current_task_context()
    first = ctx.call_task("repair", issue=issue)
    ctx.tasks.update_source(
        task_id="declared_child_tasks.repair",
        base_version="v1",
        module="declared_child_generated_v2",
        entrypoint="repair",
        source_text='def repair(repo: GitRepo, issue: str):\\n return f"child-v2: {issue}"\\n',
        may_default="ReadWrite",
    )
    second = ctx.call_task("repair", issue=issue)
    return repo.write("candidate.txt", f"{first}|{second}\\n".encode())
""",
        "fix_bug",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(child_source, may_default="ReadWrite")
        workspace.tasks.register(
            parent_source,
            may_default="ReadWrite",
            declared_dependencies={"repair": {"task_id": "declared_child_tasks.repair", "selector": "active"}},
        )

        with pytest.raises(RunStartError, match="task-library mutation during a retained nucleus run"):
            _start_fenced_run(
                workspace,
                "declared_parent_tasks.fix_bug",
                args={"issue": "parser"},
                placement="advisory",
            )

        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        assert [resolution.reason for resolution in record.task_resolutions] == ["run_start", "declared_alias"]
        assert record.task_resolutions[1].task_lock.version == "v1"
        assert record.task_resolutions[1].metadata["binding_policy"] == "once_per_run"
        assert record.task_resolutions[1].metadata["alias_path"] == "repair"
        assert workspace.tasks.get("declared_child_tasks.repair").version == "v1"
    finally:
        workspace.close()


def test_live_task_handle_fails_closed_on_in_run_generated_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_source = _write_module(
        tmp_path,
        monkeypatch,
        "live_parent_tasks",
        """
from shepherd_dialect.workspace_control import current_task_context


def fix_bug(repo: GitRepo):
    ctx = current_task_context()
    handle = ctx.tasks.handle("generated_repair.repair", policy="live")
    seen = []
    for index in range(1, 5):
        kwargs = dict(
            task_id="generated_repair.repair",
            module=f"generated_repair_v{index}",
            entrypoint="repair",
            source_text=f'def repair(repo: GitRepo):\\n return "v{index}"\\n',
            may_default="ReadWrite",
        )
        if index == 1:
            ctx.tasks.register_source(**kwargs)
        else:
            ctx.tasks.update_source(base_version=f"v{index - 1}", **kwargs)
        seen.append(handle())
    return repo.write("candidate.txt", "|".join(seen).encode())
""",
        "fix_bug",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(parent_source, may_default="ReadWrite")

        with pytest.raises(RunStartError, match="task-library mutation during a retained nucleus run"):
            _start_fenced_run(workspace, "live_parent_tasks.fix_bug", placement="advisory")

        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        assert workspace.tasks.get("generated_repair.repair") is None
    finally:
        workspace.close()


def test_pinned_task_handle_fails_closed_on_in_run_task_library_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_source = _write_module(
        tmp_path,
        monkeypatch,
        "pinned_child_tasks",
        """
def repair(repo: GitRepo, issue: str):
    return f"pinned-v1: {issue}"
""",
        "repair",
    )
    parent_source = _write_module(
        tmp_path,
        monkeypatch,
        "pinned_parent_tasks",
        """
from shepherd_dialect.workspace_control import current_task_context


def fix_bug(repo: GitRepo, issue: str):
    ctx = current_task_context()
    resolution = ctx.resolve_task("pinned_child_tasks.repair", reason="dynamic_lookup")
    handle = ctx.tasks.pinned(resolution)
    ctx.tasks.update_source(
        task_id="pinned_child_tasks.repair",
        base_version="v1",
        module="pinned_child_generated_v2",
        entrypoint="repair",
        source_text='def repair(repo: GitRepo, issue: str):\\n return f"pinned-v2: {issue}"\\n',
        may_default="ReadWrite",
    )
    result = handle(issue=issue)
    return repo.write("candidate.txt", f"{result}\\n".encode())
""",
        "fix_bug",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(child_source, may_default="ReadWrite")
        workspace.tasks.register(parent_source, may_default="ReadWrite")

        with pytest.raises(RunStartError, match="task-library mutation during a retained nucleus run"):
            _start_fenced_run(
                workspace,
                "pinned_parent_tasks.fix_bug",
                args={"issue": "parser"},
                placement="advisory",
            )

        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        original_resolution = record.task_resolutions[1]
        assert [resolution.reason for resolution in record.task_resolutions] == ["run_start", "dynamic_lookup"]
        assert original_resolution.task_lock.version == "v1"
        assert workspace.tasks.get("pinned_child_tasks.repair").version == "v1"
    finally:
        workspace.close()


def test_in_run_register_source_fails_closed_without_executing_generated_top_level_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "generated-registration-marker.txt"
    generated_source = (
        "from pathlib import Path\n"
        "from shepherd_runtime.nucleus import GitRepo\n"
        f"Path({str(marker)!r}).write_text('registered', encoding='utf-8')\n"
        "def repair(repo: GitRepo):\n"
        " return 'executed'\n"
    )
    parent_source = _write_module(
        tmp_path,
        monkeypatch,
        "generated_register_parent_tasks",
        f"""
from pathlib import Path
from shepherd_dialect.workspace_control import current_task_context


MARKER = {str(marker)!r}


def fix_bug(repo: GitRepo):
    ctx = current_task_context()
    ctx.tasks.register_source(
        task_id="generated_no_exec.repair",
        module="generated_no_exec_v1",
        entrypoint="repair",
        source_text={generated_source!r},
        may_default="ReadWrite",
    )
    existed_after_registration = Path(MARKER).exists()
    if existed_after_registration:
        raise RuntimeError("generated source executed during registration")
    result = ctx.tasks.handle("generated_no_exec.repair", policy="live")()
    existed_after_call = Path(MARKER).exists()
    return repo.write(
        "candidate.txt",
        f"after_registration={{existed_after_registration}};after_call={{existed_after_call}};result={{result}}".encode(),
    )
""",
        "fix_bug",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(parent_source, may_default="ReadWrite")

        with pytest.raises(RunStartError, match="task-library mutation during a retained nucleus run"):
            _start_fenced_run(workspace, "generated_register_parent_tasks.fix_bug", placement="advisory")

        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        assert not marker.exists()
        assert workspace.tasks.get("generated_no_exec.repair") is None
    finally:
        workspace.close()


def test_update_source_derives_from_active_base_and_rejects_stale_base(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path / "ws")
    try:
        v1 = workspace.tasks.register_source(
            task_id="source_update.repair",
            module="source_update_v1",
            entrypoint="repair",
            source_text='from shepherd_runtime.nucleus import GitRepo\n\ndef repair(repo: GitRepo):\n return "v1"\n',
            may_default="ReadWrite",
        )
        v2 = workspace.tasks.update_source(
            "source_update.repair",
            base_version=v1.version,
            module="source_update_v2",
            entrypoint="repair",
            source_text='from shepherd_runtime.nucleus import GitRepo\n\ndef repair(repo: GitRepo):\n return "v2"\n',
            may_default="ReadWrite",
        )

        assert v2.version == "v2"
        assert v2.base_version == "v1"
        assert workspace.tasks.get("source_update.repair") == v2

        with pytest.raises(TaskRegistrationError, match="stale base_version"):
            workspace.tasks.update_source(
                "source_update.repair",
                base_version=v1.version,
                module="source_update_v3",
                entrypoint="repair",
                source_text="from shepherd_runtime.nucleus import GitRepo\n\n"
                'def repair(repo: GitRepo):\n return "v3"\n',
                may_default="ReadWrite",
            )
    finally:
        workspace.close()


def test_lock_only_pinned_handle_records_exact_lock_without_task_ledger_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_source = _write_module(
        tmp_path,
        monkeypatch,
        "lock_only_child_tasks",
        """
def repair(repo: GitRepo, issue: str):
    return f"lock-only-v1: {issue}"
""",
        "repair",
    )
    parent_source = _write_module(
        tmp_path,
        monkeypatch,
        "lock_only_parent_tasks",
        """
from shepherd_dialect.workspace_control import current_task_context


def fix_bug(repo: GitRepo, issue: str):
    ctx = current_task_context()
    resolution = ctx.resolve_task("lock_only_child_tasks.repair", reason="dynamic_lookup")
    handle = ctx.tasks.pinned(resolution.task_lock)
    result = handle(issue=issue)
    return repo.write("candidate.txt", f"{result}\\n".encode())
""",
        "fix_bug",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(child_source, may_default="ReadWrite")
        workspace.tasks.register(parent_source, may_default="ReadWrite")

        record = _start_fenced_run(
            workspace,
            "lock_only_parent_tasks.fix_bug",
            args={"issue": "parser"},
            placement="advisory",
        )

        pinned_resolution = record.task_resolutions[-1]
        assert pinned_resolution.reason == "pinned"
        assert pinned_resolution.task_ledger_head is None
        assert pinned_resolution.metadata["binding_policy"] == "pinned"
        assert pinned_resolution.metadata["resolution_kind"] == "exact_lock"
        assert "source_resolution_id" not in pinned_resolution.metadata
        assert pinned_resolution.task_lock.task_id == "lock_only_child_tasks.repair"
    finally:
        workspace.close()


def test_single_file_task_registration_rejects_local_sibling_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "helper.py").write_text("VALUE = 'v1'\n", encoding="utf-8")
    source = _write_module(
        tmp_path,
        monkeypatch,
        "ambient_task",
        """
import helper


def fix_bug(repo: GitRepo):
    return helper.VALUE
""",
        "fix_bug",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        with pytest.raises(TaskRegistrationError, match="explicit task bundle"):
            workspace.tasks.register(source, may_default="ReadWrite")
    finally:
        workspace.close()


def test_single_file_task_registration_rejects_same_package_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_dir = tmp_path / "pkg_tasks"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "helper.py").write_text("VALUE = 'ambient'\n", encoding="utf-8")
    (package_dir / "task.py").write_text(
        """
import pkg_tasks.helper
from shepherd_runtime.nucleus import GitRepo


def fix_bug(repo: GitRepo):
    return pkg_tasks.helper.VALUE
""",
        encoding="utf-8",
    )
    sys.modules.pop("pkg_tasks", None)
    sys.modules.pop("pkg_tasks.task", None)
    monkeypatch.syspath_prepend(str(tmp_path))
    workspace = _make_workspace(tmp_path / "ws")
    try:
        with pytest.raises(TaskRegistrationError, match="explicit task bundle"):
            workspace.tasks.register("pkg_tasks.task:fix_bug", may_default="ReadWrite")
    finally:
        workspace.close()


def test_dependency_preflight_rejects_task_index_cache_drift(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path / "ws")
    try:
        child = workspace.tasks.register_source(
            task_id="drift_child.repair",
            module="drift_child",
            entrypoint="repair",
            source_text='from shepherd_runtime.nucleus import GitRepo\n\ndef repair(repo: GitRepo):\n return "child"\n',
            may_default="ReadWrite",
        )
        parent = workspace.tasks.register_source(
            task_id="drift_parent.run",
            module="drift_parent",
            entrypoint="run",
            source_text='from shepherd_runtime.nucleus import GitRepo\n\ndef run(repo: GitRepo):\n return "parent"\n',
            may_default="ReadWrite",
        )
        drifted_parent = replace(
            parent,
            declared_dependencies={
                "repair": DeclaredTaskDependency(task_id=child.task_id, selector="active"),
            },
        )
        selected = workspace.mg.read_selected_binding_revision_with_head(TASK_LEDGER_BINDING)
        assert selected is not None
        workspace.mg.exec(
            TASK_LEDGER_BINDING,
            "publish",
            scope=workspace.mg.ground,
            payload={
                "schema": TASK_LEDGER_SCHEMA,
                "tasks": {
                    child.task_id: [child.to_json()],
                    parent.task_id: [drifted_parent.to_json()],
                },
            },
            expected_head=selected.head,
            authority=mint_ledger_write_authority(),
        )

        with pytest.raises(RunStartError, match="dependency cache disagrees"):
            _start_fenced_run(workspace, "drift_parent.run")
    finally:
        workspace.close()


def test_run_start_rejects_cyclic_task_dependency_graph(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path / "ws")
    try:
        task_a = workspace.tasks.register_source(
            task_id="cycle.a",
            module="cycle_a",
            entrypoint="run",
            source_text="from shepherd_runtime.nucleus import GitRepo\n\ndef run(repo: GitRepo):\n return None\n",
            may_default="ReadWrite",
            declared_dependencies={"b": {"task_id": "cycle.b", "selector": "active"}},
        )
        task_b = workspace.tasks.register_source(
            task_id="cycle.b",
            module="cycle_b",
            entrypoint="run",
            source_text="from shepherd_runtime.nucleus import GitRepo\n\ndef run(repo: GitRepo):\n return None\n",
            may_default="ReadWrite",
            declared_dependencies={"a": {"task_id": "cycle.a", "selector": "active"}},
        )
        selected = workspace.mg.read_selected_binding_revision_with_head(TASK_LEDGER_BINDING)
        assert selected is not None
        workspace.mg.exec(
            TASK_LEDGER_BINDING,
            "publish",
            scope=workspace.mg.ground,
            payload={
                "schema": TASK_LEDGER_SCHEMA,
                "tasks": {
                    task_a.task_id: [replace(task_a, status="active").to_json()],
                    task_b.task_id: [replace(task_b, status="active").to_json()],
                },
            },
            expected_head=selected.head,
            authority=mint_ledger_write_authority(),
        )

        with pytest.raises(RunStartError, match="dependency cycle"):
            _start_fenced_run(workspace, "cycle.a")
    finally:
        workspace.close()


def test_task_update_rejects_missing_or_stale_base_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"fixed: {issue}\\n".encode())
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        v1 = workspace.tasks.register(source, may_default="ReadOnly")

        with pytest.raises(TaskRegistrationError, match="missing base_version"):
            workspace.tasks.update("sample_tasks.fix_bug", source, base_version="v404")

        v2 = workspace.tasks.update("sample_tasks.fix_bug", source, base_version=v1.version)
        assert v2.version == "v2"
        assert workspace.tasks.get("sample_tasks.fix_bug") == v2

        with pytest.raises(TaskRegistrationError, match="stale base_version"):
            workspace.tasks.update("sample_tasks.fix_bug", source, base_version=v1.version)
    finally:
        workspace.close()


def test_task_update_validates_run_produced_source_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"fixed: {issue}\\n".encode())
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        v1 = workspace.tasks.register(source, may_default="ReadWrite")
        record = _start_fenced_run(workspace, "sample_tasks.fix_bug", args={"issue": "parser"})
        assert record.status == "retained"
        assert record.terminalization.output_publication_status == "published"
        published_world_oid = run_workspace_output_world_oid(record)
        assert published_world_oid is not None
        valid_source_identity = f"world:{published_world_oid}:path:sample_tasks.py"

        with pytest.raises(TaskRegistrationError, match="missing produced_by_run"):
            workspace.tasks.update(
                "sample_tasks.fix_bug",
                source,
                base_version=v1.version,
                produced_by_run="run-missing",
                source_identity=valid_source_identity,
            )

        with pytest.raises(TaskRegistrationError, match="world does not match"):
            workspace.tasks.update(
                "sample_tasks.fix_bug",
                source,
                base_version=v1.version,
                produced_by_run=record.run_ref,
                source_identity="world:other:path:sample_tasks.py",
            )

        with pytest.raises(TaskRegistrationError, match="relative workspace path"):
            workspace.tasks.update(
                "sample_tasks.fix_bug",
                source,
                base_version=v1.version,
                produced_by_run=record.run_ref,
                source_identity=f"world:{published_world_oid}:path:/tmp/sample_tasks.py",
            )

        (retained_row,) = workspace.mg.list_retained_outputs(parent=workspace.mg.ground, binding="workspace")
        workspace.mg.release_retained_output(retained_row.scope_name, parent=workspace.mg.ground)

        v2 = workspace.tasks.update(
            "sample_tasks.fix_bug",
            source,
            base_version=v1.version,
            produced_by_run=record.run_ref,
            source_identity=valid_source_identity,
            derived_from=(record.run_ref,),
        )

        assert v2.version == "v2"
        assert v2.produced_by_run == record.run_ref
        assert v2.source_identity == valid_source_identity
    finally:
        workspace.close()


def test_task_update_rejects_run_without_published_workspace_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"fixed: {issue}\\n".encode())
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        v1 = workspace.tasks.register(source, may_default="ReadWrite")
        output_world_oid = workspace.mg.world_oid(workspace.mg.ground)
        assert output_world_oid is not None
        merged_without_output = RunRecord(
            run_ref="run-merged-without-output",
            task_id="sample_tasks.improve_task",
            task_version="v1",
            task_schema_digest="sha256:meta-task",
            args_digest="sha256:args",
            may_profile="ReadWrite",
            provider="test",
            status="merged",
            terminalization=RunTerminalization(
                body_status="completed",
                world_disposition="merged",
                output_publication_status="not_applicable",
            ),
            input_workspace_world_oid=output_world_oid,
            terminal_workspace_world_oid=output_world_oid,
        )
        unpublished_retained = RunRecord(
            run_ref="run-retained-unpublished",
            task_id="sample_tasks.improve_task",
            task_version="v1",
            task_schema_digest="sha256:meta-task",
            args_digest="sha256:args",
            may_profile="ReadWrite",
            provider="test",
            status="retained",
            terminalization=RunTerminalization(
                body_status="completed",
                world_disposition="retained",
                output_publication_status="failed",
                retained_custody=RunRetainedCustody(
                    custody_ref="handoff-unpublished",
                    output_world_oid=output_world_oid,
                    binding="workspace",
                    store_id="store_workspace",
                    resource_id="workspace",
                    parent_basis_world_oid=output_world_oid,
                ),
                publication_error={
                    "type": "RuntimeError",
                    "message": "descriptor publication failed",
                    "stage": "output_publication",
                    "retained_custody_ref": "handoff-unpublished",
                    "retained_output_world_oid": output_world_oid,
                },
            ),
            input_workspace_world_oid=output_world_oid,
            terminal_workspace_world_oid=output_world_oid,
        )
        workspace.runs._publish_record(merged_without_output)
        workspace.runs._publish_record(unpublished_retained)
        source_identity = f"world:{output_world_oid}:path:sample_tasks.py"

        for record in (merged_without_output, unpublished_retained):
            with pytest.raises(TaskRegistrationError, match="no published workspace output"):
                workspace.tasks.update(
                    "sample_tasks.fix_bug",
                    source,
                    base_version=v1.version,
                    produced_by_run=record.run_ref,
                    source_identity=source_identity,
                )
    finally:
        workspace.close()


def test_raw_ledger_publish_requires_workspace_control_authority(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path / "ws")
    try:
        with pytest.raises(DriverAuthorityRequiredError, match="orchestration authority"):
            workspace.mg.exec(
                TASK_LEDGER_BINDING,
                "publish",
                scope=workspace.mg.ground,
                payload={"schema": TASK_LEDGER_SCHEMA, "tasks": {}},
                authority={},
            )
    finally:
        workspace.close()


@pytest.mark.slow
def test_workspace_control_cli_reopens_real_workspace_for_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from shepherd_dialect import cli

    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    root = tmp_path / "ws"
    workspace = _make_workspace(root)
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        record = _start_fenced_run(workspace, "sample_tasks.fix_bug", args={"issue": "parser"})
        _start_fenced_run(workspace, "sample_tasks.fix_bug", args={"issue": "renderer"})
        released_record = _start_fenced_run(workspace, "sample_tasks.fix_bug", args={"issue": "released"})
        (released_output,) = workspace.runs.outputs(run_ref=released_record.run_ref)
        workspace.release(released_output)
        discarded_record = _start_fenced_run(workspace, "sample_tasks.fix_bug", args={"issue": "discarded"})
        (discarded_output,) = workspace.runs.outputs(run_ref=discarded_record.run_ref)
        workspace.discard(discarded_output)
        ledger_before_changeset = json.loads(
            json.dumps(workspace.mg.read_selected_binding_revision(RUN_LEDGER_BINDING))
        )
    finally:
        workspace.close()

    monkeypatch.chdir(root)
    runner = CliRunner()

    task_result = runner.invoke(cli.main, ["task", "list", "--json"])
    assert task_result.exit_code == 0, task_result.output
    assert json.loads(task_result.output)[0]["task_id"] == "sample_tasks.fix_bug"

    resolve_result = runner.invoke(cli.main, ["task", "resolve", "sample_tasks.fix_bug", "--json"])
    assert resolve_result.exit_code == 0, resolve_result.output
    resolved = json.loads(resolve_result.output)
    assert resolved["reason"] == "cli"
    assert resolved["task_lock"]["task_id"] == "sample_tasks.fix_bug"

    run_result = runner.invoke(cli.main, ["run", "show", "@latest", "--json"])
    assert run_result.exit_code == 0, run_result.output
    assert json.loads(run_result.output)["run_ref"] == discarded_record.run_ref

    outputs_result = runner.invoke(cli.main, ["run", "outputs", record.run_ref, "--json"])
    assert outputs_result.exit_code == 0, outputs_result.output
    outputs = json.loads(outputs_result.output)
    assert outputs[0]["identity"]["output_name"] == "workspace"
    assert outputs[0]["state"] == "unconsumed"

    changeset_result = runner.invoke(
        cli.main,
        ["run", "changeset", record.run_ref[:8], "--binding", "workspace", "--state", "unconsumed", "--json"],
    )
    assert changeset_result.exit_code == 0, changeset_result.output
    changeset = json.loads(changeset_result.output)
    assert changeset["output_id"] == record.outputs["workspace"].output_id
    assert changeset["binding"] == "workspace"
    assert changeset["state"] == "unconsumed"
    assert changeset["changed_paths"] == changeset["output"]["changed_paths"]
    assert changeset["output"]["identity"]["output_id"] == record.outputs["workspace"].output_id

    read_result = runner.invoke(cli.main, ["run", "changeset", record.run_ref, "--read", "candidate.txt"])
    assert read_result.exit_code == 0, read_result.output
    assert read_result.output == "selected candidate: parser\n"

    read_missing_result = runner.invoke(cli.main, ["run", "changeset", record.run_ref, "--read", "missing.txt"])
    assert read_missing_result.exit_code != 0
    assert "no file" in read_missing_result.output

    read_json_conflict = runner.invoke(
        cli.main, ["run", "changeset", record.run_ref, "--read", "candidate.txt", "--json"]
    )
    assert read_json_conflict.exit_code != 0
    assert "mutually exclusive" in read_json_conflict.output

    for settled_record, expected_state in (
        (released_record, "released"),
        (discarded_record, "discarded"),
    ):
        settled_changeset_result = runner.invoke(
            cli.main,
            ["run", "changeset", settled_record.run_ref, "--state", expected_state, "--json"],
        )
        assert settled_changeset_result.exit_code == 0, settled_changeset_result.output
        settled_changeset = json.loads(settled_changeset_result.output)
        assert settled_changeset["output_id"] == settled_record.outputs["workspace"].output_id
        assert settled_changeset["state"] == expected_state

    latest_changeset_result = runner.invoke(cli.main, ["run", "changeset", "@latest", "--json"])
    assert latest_changeset_result.exit_code == 0, latest_changeset_result.output
    latest_changeset = json.loads(latest_changeset_result.output)
    assert latest_changeset["output_id"] == discarded_record.outputs["workspace"].output_id

    reader = _make_workspace(root)
    try:
        assert reader.mg.read_selected_binding_revision(RUN_LEDGER_BINDING) == ledger_before_changeset
    finally:
        reader.close()

    ambiguous_changeset_result = runner.invoke(cli.main, ["run", "changeset", "run"])
    assert ambiguous_changeset_result.exit_code != 0
    assert "ambiguous" in ambiguous_changeset_result.output


@pytest.mark.parametrize(("command", "expected_state"), [("release", "released"), ("discard", "discarded")])
def test_workspace_control_cli_release_and_discard_settle_run_outputs_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    expected_state: str,
) -> None:
    from click.testing import CliRunner

    from shepherd_dialect import cli

    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    root = tmp_path / "ws"
    workspace = _make_workspace(root)
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        selected_before = _seed_selected_workspace(workspace).basis
        run = workspace.run(
            "sample_tasks.fix_bug",
            repo=workspace.git_repo(),
            args={"issue": command},
            placement="advisory",
        )
        run_ref = run.ref.id
        ledger_before = json.loads(json.dumps(workspace.mg.read_selected_binding_revision(RUN_LEDGER_BINDING)))
    finally:
        workspace.close()

    monkeypatch.chdir(root)
    runner = CliRunner()

    result = runner.invoke(cli.main, ["run", command, run_ref, "--binding", "workspace"])

    assert result.exit_code == 0, result.output
    settlement = json.loads(result.output)
    assert settlement["settlement"]["action"] == expected_state
    assert settlement["parent_world_after"] == settlement["parent_world_before"]

    outputs_result = runner.invoke(cli.main, ["run", "outputs", run_ref, "--state", expected_state, "--json"])
    assert outputs_result.exit_code == 0, outputs_result.output
    outputs = json.loads(outputs_result.output)
    assert len(outputs) == 1
    assert outputs[0]["state"] == expected_state
    assert outputs[0]["settlement_ref"] == settlement["settlement"]["settlement_ref"]

    second_result = runner.invoke(cli.main, ["run", command, run_ref])
    assert second_result.exit_code != 0
    assert "unconsumed" in second_result.output or "already settled" in second_result.output

    reader = _make_workspace(root)
    try:
        assert reader.mg.read_selected_binding_revision(RUN_LEDGER_BINDING) == ledger_before
        assert reader.runs.outputs(run_ref=run_ref, state="unconsumed") == ()
        (settled,) = reader.runs.outputs(run_ref=run_ref, state=expected_state)
        assert settled.settlement_ref == settlement["settlement"]["settlement_ref"]
        selected_after = _assert_selected_git_repo_for_workspace(reader)
        assert same_git_binding_state(selected_after.basis, selected_before)
    finally:
        reader.close()


def test_workspace_control_cli_select_settles_output_and_advances_selected_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from shepherd_dialect import cli

    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    root = tmp_path / "ws"
    workspace = _make_workspace(root)
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)
        run = workspace.run("sample_tasks.fix_bug", repo=repo, args={"issue": "selected"}, placement="advisory")
        run_ref = run.ref.id
        output_repo = _assert_readonly_git_repo_for_output(run.output())
        ledger_before = json.loads(json.dumps(workspace.mg.read_selected_binding_revision(RUN_LEDGER_BINDING)))
    finally:
        workspace.close()

    monkeypatch.chdir(root)
    runner = CliRunner()

    result = runner.invoke(cli.main, ["run", "select", run_ref, "--binding", "workspace"])

    assert result.exit_code == 0, result.output
    selection = json.loads(result.output)
    assert selection["settlement"]["action"] == "selected"
    assert selection["authority_operation_id"] is not None

    outputs_result = runner.invoke(cli.main, ["run", "outputs", run_ref, "--state", "selected", "--json"])
    assert outputs_result.exit_code == 0, outputs_result.output
    outputs = json.loads(outputs_result.output)
    assert len(outputs) == 1
    assert outputs[0]["state"] == "selected"
    assert outputs[0]["settlement_ref"] == selection["settlement"]["settlement_ref"]

    reader = _make_workspace(root)
    try:
        assert reader.mg.read_selected_binding_revision(RUN_LEDGER_BINDING) == ledger_before
        assert reader.runs.outputs(run_ref=run_ref, state="unconsumed") == ()
        selected_repo = _assert_selected_git_repo_for_workspace(reader, expected_head_basis=output_repo.basis)
        assert selected_repo.basis.world_oid != output_repo.basis.world_oid
        assert same_git_binding_state(selected_repo.basis, output_repo.basis)
    finally:
        reader.close()


def test_workspace_control_cli_settlement_requires_exact_run_and_named_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from shepherd_dialect import cli

    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    root = tmp_path / "ws"
    workspace = _make_workspace(root)
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)
        run = workspace.run("sample_tasks.fix_bug", repo=repo, args={"issue": "guard"}, placement="advisory")
        run_ref = run.ref.id
    finally:
        workspace.close()

    monkeypatch.chdir(root)
    runner = CliRunner()

    latest_result = runner.invoke(cli.main, ["run", "release", "@latest"])
    assert latest_result.exit_code != 0
    assert "exact run identity" in latest_result.output

    prefix_result = runner.invoke(cli.main, ["run", "release", run_ref[:8]])
    assert prefix_result.exit_code != 0
    assert "exact run identity" in prefix_result.output

    missing_output_result = runner.invoke(cli.main, ["run", "release", run_ref, "--output-name", "missing"])
    assert missing_output_result.exit_code != 0
    assert "no output named 'missing'" in missing_output_result.output

    reader = _make_workspace(root)
    try:
        (output,) = reader.runs.outputs(run_ref=run_ref, state="unconsumed")
        assert output.output_name == "workspace"
    finally:
        reader.close()


def test_workspace_control_cli_parent_run_attachment_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from shepherd_dialect import cli

    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    root = tmp_path / "ws"
    workspace = _make_workspace(root)
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        record = _start_fenced_run(workspace, "sample_tasks.fix_bug", args={"issue": "parser"})
    finally:
        workspace.close()

    monkeypatch.chdir(root)
    monkeypatch.setenv("SHEPHERD_PARENT_RUN_REF", record.run_ref)
    monkeypatch.setenv("SHEPHERD_INVOCATION_REF", "caller-supplied")
    runner = CliRunner()

    result = runner.invoke(
        cli.main,
        ["task", "resolve", "sample_tasks.fix_bug", "--parent-run", record.run_ref],
    )

    assert result.exit_code != 0
    assert "managed invocation authority" in result.output


def test_workspace_defaults_trace_store_to_documented_sqlite_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pre-launch cut: the semantic TraceStore ABI ships on the SQLite reference backend at the
    # documented default path. A workspace built without an explicit trace_store_path must resolve to
    # .vcscore/shepherd/trace.sqlite, a run must persist that backing store on disk, and outputs must
    # still resolve through it.
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    root = tmp_path / "ws"
    workspace = _make_workspace(root, explicit_trace_path=False)
    try:
        assert workspace.trace_store_path == root / ".vcscore" / "shepherd" / "trace.sqlite"
        workspace.tasks.register(source, may_default="ReadWrite")
        record = _start_fenced_run(workspace, "sample_tasks.fix_bug", args={"issue": "parser"})
        assert record.status == "retained"
        assert workspace.trace_store_path.exists()  # the SQLite backend persisted at the documented path
        output_refs = workspace.runs.outputs(run_ref=record.run_ref)
        assert output_refs[0].descriptor.output_name == "workspace"
    finally:
        workspace.close()


def test_workspace_reopen_resolves_outputs_from_persisted_default_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pre-launch persistence (the real launch question): a FRESH workspace -- new VcsCore + Store + trace
    # connection, the new-process equivalent -- must resolve a prior run's outputs from the on-disk default
    # .vcscore/shepherd/trace.sqlite + vcs-core custody, given ONLY the durable run_ref string (not in-memory
    # state). File-exists proves bytes persisted; this proves a fresh workspace can RESOLVE through them.
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    root = tmp_path / "ws"
    workspace = _make_workspace(root, explicit_trace_path=False)
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        record = _start_fenced_run(workspace, "sample_tasks.fix_bug", args={"issue": "parser"})
        assert record.status == "retained"
        run_ref = record.run_ref
        first_names = [ref.descriptor.output_name for ref in workspace.runs.outputs(run_ref=run_ref)]
        assert "workspace" in first_names
    finally:
        workspace.close()

    # Fresh workspace at the same root; given only the durable run_ref string, it must rediscover the run
    # from the persisted ledger and re-resolve its outputs through the on-disk SQLite trace store.
    reopened = _make_workspace(root, explicit_trace_path=False)
    try:
        assert reopened.trace_store_path.exists()
        reopened_names = [ref.descriptor.output_name for ref in reopened.runs.outputs(run_ref=run_ref)]
        assert reopened_names == first_names
    finally:
        reopened.close()


def test_runs_outputs_fails_closed_when_trace_descriptor_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pre-launch join integrity: a RunOutputRef is (run-ledger citation + retained custody + TraceStore
    # descriptor). If retained custody survives in vcs-core but the trace descriptor cannot resolve, the
    # product join must FAIL CLOSED (raise) -- never return a custody row as if it were a valid output.
    from shepherd_dialect.workspace_control.outputs import TraceDescriptorNotResolvedError

    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    root = tmp_path / "ws"
    workspace = _make_workspace(root, explicit_trace_path=False)
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        record = _start_fenced_run(workspace, "sample_tasks.fix_bug", args={"issue": "parser"})
        assert record.status == "retained"
        run_ref = record.run_ref
        # Happy path: custody + descriptor both present -> the output resolves.
        assert [ref.descriptor.output_name for ref in workspace.runs.outputs(run_ref=run_ref)]
    finally:
        workspace.close()

    # Reopen at the same vcs-core root but with a DIFFERENT, empty trace store: retained custody persists
    # in vcs-core, but the trace descriptor is gone -- the clean "trace lost, custody survived" split.
    reopened = _make_workspace(root, trace_store_path_override=tmp_path / "empty-trace.sqlite")
    try:
        with pytest.raises(TraceDescriptorNotResolvedError):
            reopened.runs.outputs(run_ref=run_ref)
    finally:
        reopened.close()


def test_runs_publish_retained_workspace_output_repairs_missing_trace_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shepherd_dialect.workspace_control.outputs import TraceDescriptorNotResolvedError

    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    root = tmp_path / "ws"
    workspace = _make_workspace(root, explicit_trace_path=False)
    run_ref: str
    original_finish_revision: str | None
    original_output_id: str
    repair_trace_path = tmp_path / "repair-trace.sqlite"
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        record = _start_fenced_run(workspace, "sample_tasks.fix_bug", args={"issue": "parser"})
        assert record.status == "retained"
        run_ref = record.run_ref
        original_finish_revision = record.operation_refs.run_finish_revision
        original_output_id = record.outputs["workspace"].output_id
    finally:
        workspace.close()

    repair_workspace = _make_workspace(root, trace_store_path_override=repair_trace_path)
    try:
        with pytest.raises(TraceDescriptorNotResolvedError):
            repair_workspace.runs.outputs(run_ref=run_ref)

        with pytest.raises(WorkspaceControlError, match="exact run identity"):
            repair_workspace.runs.publish_retained_workspace_output("@latest")
        with pytest.raises(WorkspaceControlError, match="exact run identity"):
            repair_workspace.runs.publish_retained_workspace_output(run_ref[:8])

        repaired = repair_workspace.runs.publish_retained_workspace_output(RunRef(id=run_ref))

        assert repaired.operation_refs.run_finish_revision == original_finish_revision
        assert repaired.outputs["workspace"].output_id == original_output_id
        assert repair_trace_path.exists()
        output_refs = repair_workspace.runs.outputs(run_ref=run_ref)
        assert len(output_refs) == 1
        assert output_refs[0].identity.output_id == original_output_id
    finally:
        repair_workspace.close()


def test_workspace_select_run_output_consumes_once_and_reflects_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)
        run = workspace.run("sample_tasks.fix_bug", repo=repo, args={"issue": "parser"}, placement="advisory")
        record = run.record
        assert record.authority_context is not None
        assert record.authority_context.task_default_may == "ReadWrite"
        assert record.authority_context.requested_may is None
        assert record.authority_context.effective_may == record.may_profile
        _assert_execution_enforcement(
            record,
            mode="in_process",
            requested_monitor=None,
            established_monitor=None,
            monitor_refusal=None,
            profile="Permissive",
            provider="in-process",
        )
        assert record.execution_evidence.execution_descriptor == {
            "mode": "in_process",
            "enforcement": "advisory",
            "profile": "Permissive",
            "provider": "in-process",
        }
        assert record.authority_context.grant_clamp["effective_digest"] == (
            record.authority_context.effective_grant_digest
        )
        output = run.output()
        assert isinstance(output, RunOutput)
        assert output.output_name == "workspace"
        assert output.binding == "workspace"
        assert output.output_id == output.identity.output_id
        assert output.ref.identity == output.identity
        assert output.refresh().state == "unconsumed"
        assert output.inspect()["identity"]["output_name"] == "workspace"
        assert output.inspect()["state"] == "unconsumed"
        evidence_before = output.settlement_evidence()
        assert evidence_before.state == "unconsumed"
        assert evidence_before.settlement_action is None
        assert evidence_before.authority_operation_id is None
        assert evidence_before.permission_plan_digest is None
        assert output.read_file("candidate.txt") == (b"selected candidate: parser\n", 0o100644)
        git_repo = _assert_readonly_git_repo_for_output(output)
        assert run.to_json()["outputs"]["workspace"]["state"] == "unconsumed"
        with pytest.raises(WorkspaceControlError, match="relative POSIX path"):
            output.read_file("../candidate.txt")
        foreign_workspace = ShepherdWorkspace(
            workspace.mg,
            trace_store_path=workspace.trace_store_path,
            workspace_path=workspace.workspace_path,
        )
        with pytest.raises(WorkspaceControlError, match="this workspace"):
            foreign_workspace.release(output)
        ledger_before = json.loads(json.dumps(workspace.mg.read_selected_binding_revision(RUN_LEDGER_BINDING)))

        selection = output.select()

        assert selection.settlement.action == "selected"
        assert selection.authority_operation_id is not None
        authority_history = workspace.mg.resolve_operation_history(
            selection.authority_operation_id,
            scope=workspace.mg.ground,
        )
        authority_decision = next(
            effect
            for effect in _authority_effects(authority_history)
            if effect["type"] == "RetainedOutputAuthorityDecision"
        )
        assert authority_decision["effective_match_digest"]
        assert authority_decision["authority_surface_plan_digest"]
        assert authority_decision["effective_match_digest"] == record.authority_context.effective_match_digest
        assert (
            authority_decision["authority_surface_plan_digest"]
            == record.authority_context.authority_surface_plan_digest
        )
        assert authority_decision["reason_code"] == "gitrepo_grant_retained_output_selection_match"
        assert authority_decision["monitor_basis"] == "carrier_check_at_commit"
        assert authority_decision["permission_plan_digest"]
        assert authority_decision["permission_plan_digest"] != authority_decision["authority_surface_plan_digest"]
        permission_plan_descriptor = authority_decision["permission_plan_descriptor"]
        assert isinstance(permission_plan_descriptor, dict)
        (assignment,) = permission_plan_descriptor["assignments"]
        assert assignment["monitor"] == "carrier_check_at_commit"
        assert assignment["route"] == "retained_output_selection"
        assert authority_decision["completeness"] == "complete"
        evidence_after = output.settlement_evidence()
        assert evidence_after.state == "selected"
        assert evidence_after.settlement_action == "selected"
        assert evidence_after.authority_operation_id == selection.authority_operation_id
        assert evidence_after.authority_settlement_operation_id == selection.authority_settlement_operation_id
        assert evidence_after.authority_outcome == "allowed"
        assert evidence_after.permission_plan_digest == authority_decision["permission_plan_digest"]
        assert evidence_after.permission_plan_descriptor == permission_plan_descriptor
        assert evidence_after.authority_settlement is not None
        assert workspace.mg.read_selected_binding_revision(RUN_LEDGER_BINDING) == ledger_before
        assert output.inspect()["state"] == "selected"
        assert run.to_json()["outputs"]["workspace"]["state"] == "selected"
        assert output.read_file("candidate.txt") == (b"selected candidate: parser\n", 0o100644)
        _assert_readonly_git_repo_for_output(output, git_repo)
        assert workspace.runs.outputs(run_ref=record.run_ref, state="unconsumed") == ()
        (selected,) = workspace.runs.outputs(run_ref=record.run_ref, state="selected")
        assert selected.state == "selected"
        assert selected.settlement_ref == selection.settlement.settlement_ref
        _assert_readonly_git_repo_for_output(selected, git_repo)
        with pytest.raises(InvalidRepositoryStateError, match="already settled"):
            workspace.select(output)
    finally:
        workspace.close()


@pytest.mark.workspace_native_jail
def test_public_workspace_run_explicit_readonly_uses_confined_process_for_raw_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if detect_containment_backend() is None:
        pytest.skip("explicit ReadOnly facade enforcement requires a native containment backend")
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
from pathlib import Path

def fix_bug(repo: GitRepo):
    Path("raw-write.txt").write_text("must be denied by the jail\\n", encoding="utf-8")
    return "unreachable"
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        selected_before = _seed_selected_workspace(workspace).basis
        repo = workspace.git_repo()

        with pytest.raises(RunStartError, match=r"confined workspace task refused.*PermissionError"):
            workspace.run("sample_tasks.fix_bug", repo=repo, may="ReadOnly")

        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        assert record.outputs == {}
        enforcement = _assert_execution_enforcement(
            record,
            mode="confined_process",
            requested_monitor="syscall_jail",
            established_monitor="syscall_jail",
            monitor_refusal=None,
            prelaunch_refusal=None,
            profile="ReadOnly",
            provider="workspace-control-confined-task",
        )
        body_refusal = enforcement["body_refusal"]
        assert isinstance(body_refusal, dict)
        assert "PermissionError" in body_refusal["type"] or "Operation not permitted" in body_refusal["message"]
        assert enforcement["authority_basis"] == "effective_gitrepo_readonly"
        assert record.enforcement == "jail"
        assert record.execution_evidence.execution_descriptor == {
            "mode": "confined_process",
            "enforcement": "syscall_jail",
            "profile": "ReadOnly",
            "provider": "workspace-control-confined-task",
        }
        assert record.execution_evidence.enforcement_basis == "launch_confined_attempted"
        assert record.task_executions
        assert record.task_executions[0].executor_kind == "confined_process"
        assert same_git_binding_state(workspace.git_repo().basis, selected_before)
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "raw-write.txt") is None
    finally:
        workspace.close()


@pytest.mark.workspace_native_jail
def test_readonly_workspace_run_fails_before_producing_mutating_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo):
    return repo.write("candidate.txt", b"readonly must not write\\n")
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadOnly")
        selected_before = _seed_selected_workspace(workspace).basis

        with pytest.raises(RunStartError, match="write is not permitted"):
            _start_fenced_run(workspace, "sample_tasks.fix_bug", placement="auto")

        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        assert record.may_profile == "ReadOnly"
        assert record.outputs == {}
        assert workspace.runs.outputs(run_ref=record.run_ref) == ()
        assert same_git_binding_state(workspace.git_repo().basis, selected_before)
    finally:
        workspace.close()


@pytest.mark.workspace_native_jail
def test_public_may_readonly_gitrepo_grant_narrows_workspace_run_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert May[GitRepo, ReadOnly] is not None
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
from shepherd_runtime.nucleus import GitRepo
from shepherd_dialect.workspace_control import May, ReadOnly

def fix_bug(repo: May[GitRepo, ReadOnly]):
    return repo.write("candidate.txt", b"not allowed by parameter grant\\n")
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        task = workspace.tasks.register(source, may_default="ReadWrite")
        (repo_param,) = [parameter for parameter in task.signature_schema["parameters"] if parameter["name"] == "repo"]
        assert repo_param["gitrepo_grant"]["grant_ref"] == "signature:repo"
        repo = _seed_selected_workspace(workspace)

        message = "write is not permitted" if detect_containment_backend() is not None else "no jail-capable"
        with pytest.raises(RunStartError, match=message):
            workspace.run("sample_tasks.fix_bug", repo=repo)

        record = workspace.runs.show("@latest")
        assert record is not None
        _assert_execution_enforcement(
            record,
            mode="confined_process",
            requested_monitor="syscall_jail",
            profile="ReadOnly",
            provider="workspace-control-confined-task",
        )
        assert workspace.runs.outputs() == ()
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "candidate.txt") is None
    finally:
        workspace.close()


@pytest.mark.workspace_native_jail
def test_public_may_readonly_gitrepo_grant_with_postponed_annotations_narrows_workspace_run_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
from __future__ import annotations

from shepherd_runtime.nucleus import GitRepo
from shepherd_dialect.workspace_control import May, ReadOnly

def fix_bug(repo: May[GitRepo, ReadOnly]):
    return repo.write("candidate.txt", b"not allowed by postponed parameter grant\\n")
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        task = workspace.tasks.register(source, may_default="ReadWrite")
        (repo_param,) = [parameter for parameter in task.signature_schema["parameters"] if parameter["name"] == "repo"]
        assert repo_param["gitrepo_grant"]["grant_ref"] == "signature:repo"
        repo = _seed_selected_workspace(workspace)

        message = "write is not permitted" if detect_containment_backend() is not None else "no jail-capable"
        with pytest.raises(RunStartError, match=message):
            workspace.run("sample_tasks.fix_bug", repo=repo)

        record = workspace.runs.show("@latest")
        assert record is not None
        _assert_execution_enforcement(
            record,
            mode="confined_process",
            requested_monitor="syscall_jail",
            profile="ReadOnly",
            provider="workspace-control-confined-task",
        )
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "candidate.txt") is None
    finally:
        workspace.close()


@pytest.mark.parametrize(
    ("future_import", "grant_expr", "message"),
    [
        ("", "UnknownGrant", "unsupported GitRepo May grant"),
        ("from __future__ import annotations\n\n", "UnknownGrant", "unsupported GitRepo May grant"),
        ("", "ReadOnly, ReadWrite", "exactly one GitRepo grant"),
        ("from __future__ import annotations\n\n", "ReadOnly, ReadWrite", "exactly one GitRepo grant"),
    ],
)
def test_callable_public_may_gitrepo_grant_rejects_unsupported_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    future_import: str,
    grant_expr: str,
    message: str,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        f"""
{future_import}from shepherd_runtime.nucleus import GitRepo
from shepherd_dialect.workspace_control import May, ReadOnly, ReadWrite

UnknownGrant = object()

def fix_bug(repo: May[GitRepo, {grant_expr}]):
    return None
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        with pytest.raises(TaskRegistrationError, match=message):
            workspace.tasks.register(source, may_default="ReadWrite")
    finally:
        workspace.close()


def test_generated_source_public_may_gitrepo_grant_lowers_to_descriptor(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path / "ws")
    try:
        # Path-scoped grants are fenced out of the public seam; the adoption-boundary lane
        # compiles them through the private escape (here, the generated-source / AST route).
        with _allow_path_prefix_grants():
            task = workspace.tasks.register_source(
                task_id="generated.fix_bug",
                module="generated_tasks",
                entrypoint="fix_bug",
                source_text='def fix_bug(repo: May[GitRepo, GitRepoPath("src/app")]):\n'
                ' return repo.write("src/app/main.py", b"generated\\n")\n',
                may_default="ReadWrite",
            )

        (repo_param,) = [parameter for parameter in task.signature_schema["parameters"] if parameter["name"] == "repo"]
        assert repo_param["gitrepo_grant"]["clauses"] == [
            {
                "binding_ref": "workspace",
                "path_prefix": "src/app",
                "mutates": True,
            }
        ]
    finally:
        workspace.close()


def test_generated_source_path_gitrepo_grant_refused_at_ast_seam_by_default(tmp_path: Path) -> None:
    # P-030 v0.2 fence: the generated-source (AST) route refuses a path-scoped grant unless the
    # private escape is active. This is the second acceptance seam — a runtime-only fence would
    # silently re-open the generated-source lane.
    workspace = _make_workspace(tmp_path / "ws")
    try:
        with pytest.raises(TaskRegistrationError, match=r"not part of the P-030 v0\.2 claim"):
            workspace.tasks.register_source(
                task_id="generated.fix_bug",
                module="generated_tasks",
                entrypoint="fix_bug",
                source_text='def fix_bug(repo: May[GitRepo, GitRepoPath("src/app")]):\n return None\n',
                may_default="ReadWrite",
            )
    finally:
        workspace.close()


def test_generated_source_public_may_gitrepo_grant_rejects_unknown_grant(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path / "ws")
    try:
        with pytest.raises(TaskRegistrationError, match="unsupported GitRepo May grant"):
            workspace.tasks.register_source(
                task_id="generated.fix_bug",
                module="generated_tasks",
                entrypoint="fix_bug",
                source_text="def fix_bug(repo: May[GitRepo, UnknownGrant]):\n return None\n",
                may_default="ReadWrite",
            )
    finally:
        workspace.close()


def test_public_may_path_gitrepo_grant_authorizes_retained_output_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
from shepherd_runtime.nucleus import GitRepo
from shepherd_dialect.workspace_control import May
from shepherd_dialect.workspace_control.authority import GitRepoPath

def fix_bug(repo: May[GitRepo, GitRepoPath("src/app")]):
    return repo.write("src/app/main.py", b"allowed by grant\\n")
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        with _allow_path_prefix_grants():
            workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)
        run = workspace.run("sample_tasks.fix_bug", repo=repo, placement="advisory")
        output = run.output()
        launch_policy = run.record.launch_context.settlement_policy
        assert launch_policy is not None
        launch_authority_context = launch_policy["authority_context"]
        assert isinstance(launch_authority_context, dict)
        assert launch_authority_context["schema"] == "shepherd.workspace-control.authority-context.v1"
        assert launch_authority_context["transaction_kind"] == "retained_output_selection"
        assert launch_authority_context["shepherd"]["run_ref"] == run.run_ref

        selection = workspace.select(output)

        assert selection.authority_operation_id is not None
        authority_history = workspace.mg.resolve_operation_history(
            selection.authority_operation_id,
            scope=workspace.mg.ground,
        )
        authority_decision = next(
            effect
            for effect in _authority_effects(authority_history)
            if effect["type"] == "RetainedOutputAuthorityDecision"
        )
        authority_prepared = next(
            effect
            for effect in _authority_effects(authority_history)
            if effect["type"] == "PreparedRetainedOutputSelection"
        )
        assert authority_prepared["authority_context"] == launch_authority_context
        assert authority_decision["authority_context"] == launch_authority_context
        assert authority_decision["reason_code"] == "gitrepo_grant_retained_output_selection_match"
        assert authority_decision["matched_grant_ref"].startswith("workspace-effective:ReadWrite:")
        assert authority_decision["monitor_basis"] == "carrier_check_at_commit"
        assert authority_decision["permission_plan_digest"]
        assert authority_decision["completeness"] == "complete"
        assert selection.authority_settlement_operation_id is not None
        settlement_history = workspace.mg.resolve_operation_history(
            selection.authority_settlement_operation_id,
            scope=workspace.mg.ground,
        )
        authority_settlement = next(
            effect
            for effect in _authority_effects(settlement_history)
            if effect["type"] == "RetainedOutputAuthoritySettlement"
        )
        assert authority_settlement["authority_context"] == launch_authority_context
        assert authority_settlement["permission_plan_digest"] == authority_decision["permission_plan_digest"]
        assert authority_settlement["permission_plan_descriptor"] == authority_decision["permission_plan_descriptor"]
        assert output.refresh().state == "selected"
        output_repo = _assert_readonly_git_repo_for_output(output)
        _assert_selected_git_repo_for_workspace(workspace, expected_head_basis=output_repo.basis)
        assert output.read_file("src/app/main.py") == (b"allowed by grant\n", 0o100644)
    finally:
        workspace.close()


def test_public_may_path_gitrepo_grant_denies_retained_output_selection_outside_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
from shepherd_runtime.nucleus import GitRepo
from shepherd_dialect.workspace_control import May
from shepherd_dialect.workspace_control.authority import GitRepoPath

def fix_bug(repo: May[GitRepo, GitRepoPath("src/app")]):
    return repo.write("docs/forbidden.txt", b"outside grant\\n")
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        with _allow_path_prefix_grants():
            workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)
        run = workspace.run("sample_tasks.fix_bug", repo=repo, placement="advisory")
        output = run.output()

        with pytest.raises(
            WorkspaceControlError,
            match="gitrepo_grant_retained_output_selection_mutates_outside_effective_grant",
        ):
            workspace.select(output)

        assert output.refresh().state == "unconsumed"
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "docs/forbidden.txt") is None
    finally:
        workspace.close()


def test_retained_output_select_uses_run_authority_across_task_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unconsumed retained output is a legitimate state, so updating the task
    # definition while it is pending succeeds. Crucially, `select` still settles
    # against the authority the RUN recorded (v1's narrow `GitRepoPath("src/app")`
    # grant), not the task's current definition — so a run that wrote outside its
    # recorded grant stays unselectable even after the task is widened to
    # `ReadWrite`.
    workspace = _make_workspace(tmp_path / "ws")
    try:
        with _allow_path_prefix_grants():
            v1 = workspace.tasks.register_source(
                task_id="generated.fix_bug",
                module="generated_tasks",
                entrypoint="fix_bug",
                source_text="""
from shepherd_runtime.nucleus import GitRepo
from shepherd_dialect.workspace_control import May
from shepherd_dialect.workspace_control.authority import GitRepoPath

def fix_bug(repo: May[GitRepo, GitRepoPath("src/app")]):
    return repo.write("docs/forbidden.txt", b"outside original grant\\n")
""",
                may_default="ReadWrite",
            )
        repo = _seed_selected_workspace(workspace)
        run = workspace.run("generated.fix_bug", repo=repo, placement="advisory")
        output = run.output()

        # Retained-legitimate: the pending output does not block the task update.
        workspace.tasks.update_source(
            "generated.fix_bug",
            base_version=v1.version,
            module="generated_tasks",
            entrypoint="fix_bug",
            source_text="""
from shepherd_runtime.nucleus import GitRepo
from shepherd_dialect.workspace_control import May, ReadWrite

def fix_bug(repo: May[GitRepo, ReadWrite]):
    return repo.write("docs/forbidden.txt", b"allowed by updated grant\\n")
""",
            may_default="ReadWrite",
        )
        assert workspace.tasks.get("generated.fix_bug").version != v1.version

        # Select still uses the RUN's recorded (narrow) authority, not the widened task.

        with pytest.raises(
            WorkspaceControlError,
            match="gitrepo_grant_retained_output_selection_mutates_outside_effective_grant",
        ):
            workspace.select(output)

        assert output.refresh().state == "unconsumed"
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "docs/forbidden.txt") is None
    finally:
        workspace.close()


def test_public_may_path_gitrepo_grant_denies_direct_authority_launch_outside_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
from shepherd_runtime.nucleus import GitRepo
from shepherd_dialect.workspace_control import May
from shepherd_dialect.workspace_control.authority import GitRepoPath

def fix_bug(repo: May[GitRepo, GitRepoPath("src/app")]):
    return repo.write("docs/forbidden.txt", b"outside grant\\n")
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        with _allow_path_prefix_grants():
            workspace.tasks.register(source, may_default="ReadWrite")
        _seed_selected_workspace(workspace)

        with pytest.raises(RunStartError, match="filesystem_merge_mutates_outside_effective_match"):
            workspace.runs._start_authority_workspace_run("sample_tasks.fix_bug")

        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.provider == "shepherd.workspace_control.nucleus-authority.v0"
        assert record.status == "failed"
        assert record.operation_refs.authority_operation
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "docs/forbidden.txt") is None
    finally:
        workspace.close()


def test_workspace_run_output_settlement_reflects_after_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    root = tmp_path / "ws"
    workspace = _make_workspace(root)
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)
        run = workspace.run("sample_tasks.fix_bug", repo=repo, args={"issue": "parser"}, placement="advisory")
        record = run.record
        output = run.output()
        git_repo = _assert_readonly_git_repo_for_output(output)

        selection = workspace.select(output)
        run_ref = record.run_ref
        settlement_ref = selection.settlement.settlement_ref
    finally:
        workspace.close()

    reopened = _make_workspace(root)
    try:
        assert reopened.runs.outputs(run_ref=run_ref, state="unconsumed") == ()
        (selected,) = reopened.runs.outputs(run_ref=run_ref, state="selected")
        assert selected.state == "selected"
        assert selected.settlement_ref == settlement_ref
        _assert_readonly_git_repo_for_output(selected, git_repo)
    finally:
        reopened.close()


def test_workspace_run_output_git_repo_hydrates_after_reopen_unconsumed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    root = tmp_path / "ws"
    workspace = _make_workspace(root)
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        record = _start_fenced_run(workspace, "sample_tasks.fix_bug", args={"issue": "parser"})
        (output,) = workspace.runs.outputs(run_ref=record.run_ref)
        git_repo = _assert_readonly_git_repo_for_output(output)
        run_ref = record.run_ref
    finally:
        workspace.close()

    reopened = _make_workspace(root)
    try:
        (reopened_output,) = reopened.runs.outputs(run_ref=run_ref, state="unconsumed")
        assert reopened_output.read_file("candidate.txt") == (b"selected candidate: parser\n", 0o100644)
        _assert_readonly_git_repo_for_output(reopened_output, git_repo)
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("method_name", "expected_state"),
    [
        ("release", "released"),
        ("discard", "discarded"),
    ],
)
def test_workspace_run_output_git_repo_hydrates_after_reopen_receipt_only_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    expected_state: str,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    root = tmp_path / "ws"
    workspace = _make_workspace(root)
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        record = _start_fenced_run(workspace, "sample_tasks.fix_bug", args={"issue": "parser"})
        (output,) = workspace.runs.outputs(run_ref=record.run_ref)
        git_repo = _assert_readonly_git_repo_for_output(output)
        settlement = getattr(output, method_name)()
        run_ref = record.run_ref
        settlement_ref = settlement.settlement.settlement_ref
    finally:
        workspace.close()

    reopened = _make_workspace(root)
    try:
        assert reopened.runs.outputs(run_ref=run_ref, state="unconsumed") == ()
        (settled,) = reopened.runs.outputs(run_ref=run_ref, state=expected_state)
        assert settled.state == expected_state
        assert settled.settlement_ref == settlement_ref
        assert settled.read_file("candidate.txt") == (b"selected candidate: parser\n", 0o100644)
        _assert_readonly_git_repo_for_output(settled, git_repo)
    finally:
        reopened.close()


def test_workspace_git_repo_acquisition_fails_closed_without_selected_workspace_world(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    workspace = _make_workspace(root)
    try:
        with pytest.raises(WorkspaceControlError, match="current workspace world"):
            workspace.git_repo()
    finally:
        workspace.close()


def test_workspace_git_repo_acquisition_hydrates_selected_workspace_basis_after_reopen(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ws"
    workspace = _make_workspace(root)
    try:
        basis = _seed_selected_workspace(workspace).basis
    finally:
        workspace.close()

    reopened = _make_workspace(root)
    try:
        _assert_selected_git_repo_for_workspace(reopened, basis)
    finally:
        reopened.close()


def test_workspace_git_repo_acquisition_reflects_selected_retained_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)
        run = workspace.run("sample_tasks.fix_bug", repo=repo, args={"issue": "parser"}, placement="advisory")
        output = run.output()
        output_repo = _assert_readonly_git_repo_for_output(output)

        workspace.select(output)

        selected_repo = _assert_selected_git_repo_for_workspace(workspace, expected_head_basis=output_repo.basis)
        assert selected_repo.authority == frozenset({"read", "write"})
        assert selected_repo.readonly().basis == selected_repo.basis
        assert selected_repo.basis.world_oid != output_repo.basis.world_oid
        assert same_git_binding_state(selected_repo.basis, output_repo.basis)
    finally:
        workspace.close()


def test_workspace_git_repo_acquisition_success_does_not_enable_run_start_execution_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _make_workspace(tmp_path / "ws")
    try:
        _seed_selected_workspace(workspace)
        for name in ("SHEPHERD2_SKELETON", "VCS_CORE_NESTED_OPERATIONS"):
            monkeypatch.delenv(name, raising=False)

        selected_repo = workspace.git_repo()

        assert selected_repo.binding == "workspace"
        for name in ("SHEPHERD2_SKELETON", "VCS_CORE_NESTED_OPERATIONS"):
            assert os.environ.get(name) is None
    finally:
        workspace.close()


@pytest.mark.parametrize(("method_name", "expected_state"), [("release", "released"), ("discard", "discarded")])
def test_workspace_git_repo_acquisition_release_and_discard_do_not_create_selected_basis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    expected_state: str,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    root = tmp_path / "ws"
    workspace = _make_workspace(root)
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        with pytest.raises(WorkspaceControlError, match="selected workspace binding"):
            workspace.git_repo()
        record = _start_fenced_run(workspace, "sample_tasks.fix_bug", args={"issue": "candidate"})
        (output,) = workspace.runs.outputs(run_ref=record.run_ref)
        output_repo = _assert_readonly_git_repo_for_output(output)

        getattr(workspace, method_name)(output)

        assert output.refresh().state == expected_state
        with pytest.raises(WorkspaceControlError, match="selected workspace binding"):
            workspace.git_repo()
        assert _assert_readonly_git_repo_for_output(output) == output_repo
    finally:
        workspace.close()


def test_workspace_run_facade_accepts_same_state_gitrepo_with_different_world_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        selected = _seed_selected_workspace(workspace)
        copied_with_different_world = GitRepo(
            binding=selected.binding,
            basis=GitRepoBasis(
                world_oid="synthetic-value-copy-world",
                store_id=selected.basis.store_id,
                resource_id=selected.basis.resource_id,
                head=selected.basis.head,
            ),
            authority=selected.authority,
        )

        run = workspace.run(
            "sample_tasks.fix_bug",
            repo=copied_with_different_world,
            args={"issue": "parser"},
            placement="advisory",
        )

        assert run.output().read_file("candidate.txt") == (b"selected candidate: parser\n", 0o100644)
        assert same_git_binding_state(copied_with_different_world.basis, selected.basis)
        assert copied_with_different_world.basis != selected.basis
    finally:
        workspace.close()


@pytest.mark.parametrize(
    ("repo_factory", "message"),
    [
        (lambda basis: "not-a-gitrepo", "GitRepo value"),
        (
            lambda basis: GitRepo(
                binding="docs",
                basis=basis,
                authority=frozenset({"read", "write"}),
            ),
            "workspace GitRepo binding",
        ),
        (
            lambda basis: GitRepo(
                binding="workspace",
                basis=basis,
                authority=frozenset({"read"}),
            ),
            "read/write",
        ),
        (
            lambda basis: GitRepo(
                binding="workspace",
                basis=GitRepoBasis(
                    world_oid=basis.world_oid,
                    store_id=basis.store_id,
                    resource_id=basis.resource_id,
                    head="stale-head",
                ),
                authority=frozenset({"read", "write"}),
            ),
            "current selected workspace binding state",
        ),
    ],
)
def test_workspace_run_facade_rejects_invalid_gitrepo_inputs(
    tmp_path: Path,
    repo_factory: Any,
    message: str,
) -> None:
    workspace = _make_workspace(tmp_path / "ws")
    try:
        selected = _seed_selected_workspace(workspace)
        repo = repo_factory(selected.basis)

        with pytest.raises(WorkspaceControlError, match=message):
            workspace.run("sample_tasks.fix_bug", repo=repo)
    finally:
        workspace.close()


@pytest.mark.parametrize(("method_name", "expected_state"), [("release", "released"), ("discard", "discarded")])
def test_workspace_git_repo_acquisition_release_and_discard_preserve_selected_binding_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    expected_state: str,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        selected_before = _seed_selected_workspace(workspace)

        candidate_run = workspace.run(
            "sample_tasks.fix_bug",
            repo=selected_before,
            args={"issue": "candidate"},
            placement="advisory",
        )
        candidate_output = candidate_run.output()
        settlement = getattr(workspace, method_name)(candidate_output)
        selected_after = workspace.git_repo()

        assert settlement.settlement.action == expected_state
        assert same_git_binding_state(selected_after.basis, selected_before.basis)
        assert candidate_run.to_json()["outputs"]["workspace"]["state"] == expected_state

        followup_run = workspace.run(
            "sample_tasks.fix_bug",
            repo=selected_before,
            args={"issue": "followup"},
            placement="advisory",
        )
        assert followup_run.output().read_file("candidate.txt") == (b"selected candidate: followup\n", 0o100644)
    finally:
        workspace.close()


def test_workspace_git_repo_acquisition_does_not_enable_run_start_execution_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _make_workspace(tmp_path / "ws")
    try:
        for name in ("SHEPHERD2_SKELETON", "VCS_CORE_NESTED_OPERATIONS"):
            monkeypatch.delenv(name, raising=False)

        with pytest.raises(WorkspaceControlError, match="current workspace world"):
            workspace.git_repo()

        for name in ("SHEPHERD2_SKELETON", "VCS_CORE_NESTED_OPERATIONS"):
            assert os.environ.get(name) is None
    finally:
        workspace.close()


@pytest.mark.slow
def test_workspace_run_facade_supports_select_reacquire_run_select_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo1 = _seed_selected_workspace(workspace)
        task_ref = TaskRef("sample_tasks.fix_bug")

        assert isinstance(workspace.ref, WorkspaceRef)

        repo1_copy = _copy_git_repo(repo1)
        run1 = workspace.run(task_ref, repo=repo1_copy, args={"issue": "first"}, placement="advisory")
        short_run_selector = run1.run_ref[:8]
        assert run1.ref == RunRef(id=run1.run_ref)
        assert workspace.runs.show(short_run_selector) == run1.record
        assert workspace.runs.show(RunRef(id=short_run_selector)) is None
        assert workspace.runs.show(run1.ref) == run1.record
        output1 = run1.output()
        assert workspace.runs.outputs(run_ref=run1.ref)[0].output_id == output1.output_id
        assert run1.changeset().read_file("candidate.txt") == (b"selected candidate: first\n", 0o100644)
        assert workspace.runs.changeset(run1.ref).output_id == output1.output_id
        assert workspace.runs.changeset(short_run_selector).output_id == output1.output_id
        with pytest.raises(WorkspaceControlError, match="no output named"):
            workspace.runs.changeset(RunRef(id=short_run_selector))
        assert workspace.runs.changeset(run1.run_ref).read_file("candidate.txt") == (
            b"selected candidate: first\n",
            0o100644,
        )
        assert workspace.runs.changeset("@latest").output_id == output1.output_id
        with pytest.raises(WorkspaceControlError, match="no output named"):
            workspace.runs.changeset(run1.ref, output_name="other")
        candidate1 = output1.as_readonly_git_repo()
        workspace.select(output1)
        repo2 = workspace.git_repo()

        assert output1.refresh().state == "selected"
        selected_changeset = workspace.runs.changeset(run1.ref)
        assert selected_changeset.stat().state == "selected"
        assert selected_changeset.read_file("candidate.txt") == (b"selected candidate: first\n", 0o100644)
        assert same_git_binding_state(repo2.basis, candidate1.basis)
        assert not same_git_binding_state(repo1.basis, repo2.basis)
        with pytest.raises(WorkspaceControlError, match="current selected workspace binding state"):
            workspace.run(task_ref, repo=repo1, args={"issue": "stale"}, placement="advisory")

        repo2_copy = _copy_git_repo(repo2)
        run2 = workspace.run(task_ref, repo=repo2_copy, args={"issue": "second"}, placement="advisory")
        output2 = run2.output()
        candidate2 = output2.as_readonly_git_repo()

        assert output2.read_file("candidate.txt") == (b"selected candidate: second\n", 0o100644)
        workspace.select(output2)
        repo3 = workspace.git_repo()

        assert output2.refresh().state == "selected"
        assert same_git_binding_state(repo3.basis, candidate2.basis)
        assert not same_git_binding_state(repo2.basis, repo3.basis)
    finally:
        workspace.close()


def test_workspace_run_facade_uses_nucleus_retained_producer_without_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shepherd2.vnext import skeleton

    def fail_bridge_session(*args: object, **kwargs: object) -> None:
        raise AssertionError("workspace.run must not route through the skeleton bridge")

    monkeypatch.setattr(skeleton, "Session", fail_bridge_session)
    monkeypatch.delenv("SHEPHERD_ENABLE_FENCED_RUN_START", raising=False)
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        run = workspace.run("sample_tasks.fix_bug", repo=repo, args={"issue": "nucleus"}, placement="advisory")
        output = run.output()

        assert run.record.provider == "shepherd.workspace_control.nucleus.v0"
        vcscore = workspace.runs.vcscore(run.run_ref)
        assert vcscore["runtime_operation"] is not None
        assert vcscore["operation_show"] == ("vcs-core", "operation", "show", vcscore["runtime_operation"])
        assert run.changeset().read_file("candidate.txt") == (b"selected candidate: nucleus\n", 0o100644)
        assert output.refresh().state == "unconsumed"
        selection = workspace.select(output)
        assert selection.authority_operation_id is not None
        authority_history = workspace.mg.resolve_operation_history(
            selection.authority_operation_id,
            scope=workspace.mg.ground,
        )
        authority_decision = next(
            effect
            for effect in _authority_effects(authority_history)
            if effect["type"] == "RetainedOutputAuthorityDecision"
        )
        assert authority_decision["reason_code"] == "gitrepo_grant_retained_output_selection_match"
        assert authority_decision["monitor_basis"] == "carrier_check_at_commit"
        assert authority_decision["permission_plan_digest"]
        assert output.refresh().state == "selected"
    finally:
        workspace.close()


def test_workspace_run_facade_records_failed_root_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    raise RuntimeError(f"cannot fix {issue}")
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        with pytest.raises(RunStartError, match="cannot fix parser"):
            workspace.run("sample_tasks.fix_bug", repo=repo, args={"issue": "parser"}, placement="advisory")

        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.provider == "shepherd.workspace_control.nucleus.v0"
        assert record.status == "failed"
        assert record.error is not None
        assert record.error["type"] == "RuntimeError"
        assert "cannot fix parser" in record.error["message"]
        assert record.outputs == {}
        assert record.operation_refs.run_start_revision
        assert record.operation_refs.run_finish_revision is None
        assert len(record.task_executions) == 1
        execution = record.task_executions[0]
        assert execution.call_kind == "root_run"
        assert execution.status == "failed"
        assert record.execution_evidence.requested_placement == "advisory"
        assert record.execution_evidence.resolved_placement == "advisory"
        assert record.enforcement == "advisory"
        assert record.execution_evidence.enforcement_basis == "explicit_advisory"
        assert execution.executor_kind == "in_process"
        assert execution.executor_policy == "trusted_bridge"
        assert execution.task_lock.task_id == "sample_tasks.fix_bug"
        assert execution.resolution_id == record.task_resolutions[0].resolution_id
        assert execution.error is not None
        assert execution.error["type"] == "RuntimeError"
        assert "cannot fix parser" in execution.error["message"]
        assert workspace.runs.outputs() == ()
    finally:
        workspace.close()


@pytest.mark.workspace_native_jail
def test_workspace_run_facade_lowers_readonly_may_to_non_writable_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo):
    return repo.write("candidate.txt", b"not allowed\\n")
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        message = "authority='readonly'" if detect_containment_backend() is not None else "no jail-capable"
        with pytest.raises(RunStartError, match=message):
            workspace.run("sample_tasks.fix_bug", repo=repo, may="ReadOnly")

        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "candidate.txt") is None
        assert workspace.runs.outputs() == ()
    finally:
        workspace.close()


@pytest.mark.workspace_native_jail
def test_workspace_run_task_default_readonly_uses_confined_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo):
    return repo.write("candidate.txt", b"not allowed by task default\\n")
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadOnly")
        repo = _seed_selected_workspace(workspace)

        message = "write is not permitted" if detect_containment_backend() is not None else "no jail-capable"
        with pytest.raises(RunStartError, match=message):
            workspace.run("sample_tasks.fix_bug", repo=repo)

        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        enforcement = _assert_execution_enforcement(
            record,
            mode="confined_process",
            requested_monitor="syscall_jail",
            provider="workspace-control-confined-task",
        )
        assert enforcement["authority_basis"] == "effective_gitrepo_readonly"
        assert record.task_executions
        assert record.task_executions[0].executor_kind == "confined_process"
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "candidate.txt") is None
        assert workspace.runs.outputs() == ()
    finally:
        workspace.close()


@pytest.mark.workspace_native_jail
def test_workspace_run_signature_readonly_gitrepo_grant_uses_confined_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
from shepherd_runtime.nucleus import GitRepo
from shepherd_dialect.workspace_control import May, ReadOnly

def fix_bug(repo: May[GitRepo, ReadOnly]):
    return repo.write("candidate.txt", b"not allowed by signature grant\\n")
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        message = "write is not permitted" if detect_containment_backend() is not None else "no jail-capable"
        with pytest.raises(RunStartError, match=message):
            workspace.run("sample_tasks.fix_bug", repo=repo)

        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        assert record.may_profile == "ReadWrite"
        enforcement = _assert_execution_enforcement(
            record,
            mode="confined_process",
            requested_monitor="syscall_jail",
            profile="ReadOnly",
            provider="workspace-control-confined-task",
        )
        assert enforcement["authority_basis"] == "effective_gitrepo_readonly"
        assert record.task_executions
        assert record.task_executions[0].executor_kind == "confined_process"
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "candidate.txt") is None
        assert workspace.runs.outputs() == ()
    finally:
        workspace.close()


@pytest.mark.workspace_native_jail
def test_workspace_run_signature_readwrite_cannot_widen_task_default_readonly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
from shepherd_runtime.nucleus import GitRepo
from shepherd_dialect.workspace_control import May, ReadWrite

def fix_bug(repo: May[GitRepo, ReadWrite]):
    return repo.write("candidate.txt", b"not allowed by readonly task default\\n")
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadOnly")
        repo = _seed_selected_workspace(workspace)

        message = "write is not permitted" if detect_containment_backend() is not None else "no jail-capable"
        with pytest.raises(RunStartError, match=message):
            workspace.run("sample_tasks.fix_bug", repo=repo)

        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        assert record.may_profile == "ReadOnly"
        enforcement = _assert_execution_enforcement(
            record,
            mode="confined_process",
            requested_monitor="syscall_jail",
            profile="ReadOnly",
            provider="workspace-control-confined-task",
        )
        assert enforcement["authority_basis"] == "effective_gitrepo_readonly"
        assert record.task_executions
        assert record.task_executions[0].executor_kind == "confined_process"
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "candidate.txt") is None
        assert workspace.runs.outputs() == ()
    finally:
        workspace.close()


@pytest.mark.workspace_native_jail
def test_workspace_run_signature_readwrite_cannot_widen_call_site_readonly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
from shepherd_runtime.nucleus import GitRepo
from shepherd_dialect.workspace_control import May, ReadWrite

def fix_bug(repo: May[GitRepo, ReadWrite]):
    return repo.write("candidate.txt", b"not allowed by readonly call-site grant\\n")
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        message = "write is not permitted" if detect_containment_backend() is not None else "no jail-capable"
        with pytest.raises(RunStartError, match=message):
            workspace.run("sample_tasks.fix_bug", repo=repo, may="ReadOnly")

        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        assert record.may_profile == "ReadOnly"
        enforcement = _assert_execution_enforcement(
            record,
            mode="confined_process",
            requested_monitor="syscall_jail",
            profile="ReadOnly",
            provider="workspace-control-confined-task",
        )
        assert enforcement["authority_basis"] == "effective_gitrepo_readonly"
        assert record.task_executions
        assert record.task_executions[0].executor_kind == "confined_process"
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "candidate.txt") is None
        assert workspace.runs.outputs() == ()
    finally:
        workspace.close()


@pytest.mark.workspace_native_jail
def test_workspace_run_readonly_noop_succeeds_under_confined_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if detect_containment_backend() is None:
        pytest.skip("successful ReadOnly retained execution requires a native containment backend")
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return {"issue": issue, "repo": repo}
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        run = workspace.run("sample_tasks.fix_bug", repo=repo, may="ReadOnly", args={"issue": "parser"})

        assert run.record.status == "retained"
        enforcement = _assert_execution_enforcement(
            run.record,
            mode="confined_process",
            requested_monitor="syscall_jail",
            established_monitor="syscall_jail",
            monitor_refusal=None,
            prelaunch_refusal=None,
            body_refusal=None,
            profile="ReadOnly",
            provider="workspace-control-confined-task",
        )
        assert enforcement["authority_basis"] == "effective_gitrepo_readonly"
        assert run.record.task_executions
        assert run.record.task_executions[0].executor_kind == "confined_process"
        assert run.output().inspect()["state"] == "unconsumed"
    finally:
        workspace.close()


@pytest.mark.workspace_native_jail
def test_workspace_run_readonly_confined_process_records_no_jail_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vcscore_runtime, "detect_containment_backend", lambda: None)
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo):
    return "unreachable"
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        with pytest.raises(RunStartError, match="no jail-capable"):
            workspace.run("sample_tasks.fix_bug", repo=repo, may="ReadOnly")

        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        enforcement = _assert_execution_enforcement(
            record,
            mode="confined_process",
            requested_monitor="syscall_jail",
            established_monitor=None,
            provider="workspace-control-confined-task",
            prelaunch_refusal=None,
            body_refusal=None,
        )
        refusal = enforcement["monitor_refusal"]
        assert isinstance(refusal, dict)
        assert refusal["type"] == "JailNotEstablished"
        assert "no jail-capable" in refusal["message"]
        assert record.task_executions
        assert record.task_executions[0].executor_kind == "confined_process"
        assert workspace.runs.outputs() == ()
    finally:
        workspace.close()


def test_confined_provider_rejects_non_relative_artifact_paths_before_launch() -> None:
    class LaunchProbe:
        def launch_confined(self, command: list[str], confinement: object) -> object:
            del command, confinement
            raise AssertionError("prelaunch artifact validation must run before launch_confined")

    provider = ConfinedRootTaskProvider(
        artifact_payload={
            "entrypoint": {"module": "sample_tasks", "qualname": "fix_bug"},
            "files": [
                {
                    "path": "../escape.py",
                    "content_encoding": "utf-8",
                    "content": "from shepherd_runtime.nucleus import GitRepo\n\n"
                    "def fix_bug(repo: GitRepo):\n return None\n",
                }
            ],
        },
        kwargs={},
        repo_authority="readonly",
    )

    with pytest.raises(ConfinedTaskExecutionError, match="relative POSIX") as exc_info:
        provider.execute(None, None, None, {}, execution=LaunchProbe(), confinement=object())

    assert exc_info.value.phase == "prelaunch_refused"
    assert exc_info.value.monitor_established is False


@pytest.mark.workspace_native_jail
def test_workspace_run_readonly_confined_process_records_prelaunch_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo):
    return "unreachable"
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)
        original_read_task_artifact = workspace_module._read_task_artifact

        def forged_task_artifact(mg: object, ref: object) -> dict[str, object]:
            payload = dict(original_read_task_artifact(mg, ref))  # type: ignore[arg-type]
            files = [dict(file) for file in payload["files"]]  # type: ignore[index]
            files[0]["path"] = "../escape.py"
            payload["files"] = files
            return payload

        monkeypatch.setattr(workspace_module, "_read_task_artifact", forged_task_artifact)

        with pytest.raises(RunStartError, match="relative POSIX"):
            workspace.run("sample_tasks.fix_bug", repo=repo, may="ReadOnly")

        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        enforcement = _assert_execution_enforcement(
            record,
            mode="confined_process",
            requested_monitor="syscall_jail",
            established_monitor=None,
            monitor_refusal=None,
            body_refusal=None,
            provider="workspace-control-confined-task",
        )
        prelaunch_refusal = enforcement["prelaunch_refusal"]
        assert isinstance(prelaunch_refusal, dict)
        assert prelaunch_refusal["type"] == "RuntimeError"
        assert "relative POSIX" in prelaunch_refusal["message"]
        assert record.task_executions
        assert record.task_executions[0].executor_kind == "confined_process"
        assert workspace.runs.outputs() == ()
    finally:
        workspace.close()


def test_workspace_run_facade_rejects_unsupported_may_before_run_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo):
    return repo
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        repo = _seed_selected_workspace(workspace)

        with pytest.raises(RunStartError, match="may='WriteOnly'"):
            workspace.run("sample_tasks.fix_bug", repo=repo, may="WriteOnly")

        assert workspace.runs.list() == ()
    finally:
        workspace.close()


def test_workspace_run_facade_rejects_widening_may_before_run_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo):
    return repo
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadOnly")
        repo = _seed_selected_workspace(workspace)

        with pytest.raises(RunStartError, match="may='ReadWrite' exceeds task may_default='ReadOnly'"):
            workspace.run("sample_tasks.fix_bug", repo=repo, may="ReadWrite")

        assert workspace.runs.list() == ()
    finally:
        workspace.close()


def test_workspace_run_output_settlement_resolves_non_ground_parent_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        parent = workspace.mg.fork(workspace.mg.ground, "settlement-parent")
        record = _start_fenced_run(workspace, "sample_tasks.fix_bug", args={"issue": "parser"}, parent=parent)
        (output,) = workspace.runs.outputs(run_ref=record.run_ref)

        assert output.identity.parent_scope_name == parent.name
        assert output.identity.parent_ref == parent.ref
        assert output.identity.parent_scope_instance_id == parent.instance_id

        forged_identity = replace(output.identity, parent_scope_instance_id="forged-parent-instance")
        forged_parent = RunOutput(workspace, replace(output.ref, identity=forged_identity))
        with pytest.raises(WorkspaceControlError, match="parent scope"):
            workspace.release(forged_parent)

        selection = workspace.select(output)

        assert selection.parent == parent
        assert output.refresh().state == "selected"
    finally:
        workspace.close()


@pytest.mark.parametrize(
    ("method_name", "expected_state"),
    [
        ("release", "released"),
        ("discard", "discarded"),
    ],
)
def test_workspace_receipt_only_run_output_settlement_reflects_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    expected_state: str,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        record = _start_fenced_run(workspace, "sample_tasks.fix_bug", args={"issue": "parser"})
        (output,) = workspace.runs.outputs(run_ref=record.run_ref)
        ledger_before = json.loads(json.dumps(workspace.mg.read_selected_binding_revision(RUN_LEDGER_BINDING)))

        settlement = getattr(workspace, method_name)(output)

        assert settlement.settlement.action == expected_state
        assert settlement.parent_world_after == settlement.parent_world_before
        assert workspace.mg.read_selected_binding_revision(RUN_LEDGER_BINDING) == ledger_before
        assert workspace.runs.outputs(run_ref=record.run_ref, state="unconsumed") == ()
        (settled,) = workspace.runs.outputs(run_ref=record.run_ref, state=expected_state)
        assert settled.state == expected_state
        assert settled.settlement_ref == settlement.settlement.settlement_ref
        changeset = workspace.runs.changeset(RunRef(id=record.run_ref), state=expected_state)
        assert changeset.stat().state == expected_state
        assert changeset.read_file("candidate.txt") == (b"selected candidate: parser\n", 0o100644)
        with pytest.raises(InvalidRepositoryStateError, match="already settled"):
            getattr(workspace, method_name)(output)
    finally:
        workspace.close()


def test_workspace_run_output_settlement_fails_closed_for_forged_or_stale_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    workspace = _make_workspace(tmp_path / "ws")
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        record = _start_fenced_run(workspace, "sample_tasks.fix_bug", args={"issue": "parser"})
        (output,) = workspace.runs.outputs(run_ref=record.run_ref)
        forged_descriptor = replace(output.descriptor, store_id="forged-store")
        forged = RunOutput(workspace, replace(output.ref, descriptor=forged_descriptor, store_id="forged-store"))

        with pytest.raises(WorkspaceControlError, match="RunOutput from this workspace"):
            workspace.release(output.ref)  # type: ignore[arg-type]

        with pytest.raises(InvalidRepositoryStateError, match="handle disagrees"):
            workspace.release(forged)
        with pytest.raises(InvalidRepositoryStateError, match="handle disagrees"):
            forged.inspect()
        with pytest.raises(InvalidRepositoryStateError, match="handle disagrees"):
            forged.read_file("candidate.txt")
        with pytest.raises(InvalidRepositoryStateError, match="handle disagrees"):
            forged.changeset().inspect()
        with pytest.raises(InvalidRepositoryStateError, match="handle disagrees"):
            forged.changeset().stat()
        with pytest.raises(InvalidRepositoryStateError, match="handle disagrees"):
            forged.changeset().read_file("candidate.txt")
        with pytest.raises(InvalidRepositoryStateError, match="handle disagrees"):
            forged.as_readonly_git_repo()
        with pytest.raises(InvalidRepositoryStateError, match="handle disagrees"):
            forged.run_authority()
        with pytest.raises(InvalidRepositoryStateError, match="handle disagrees"):
            forged.settlement_policy()

        forged_ground_identity = replace(output.identity, parent_scope_instance_id="forged-ground")
        forged_ground_parent = RunOutput(workspace, replace(output.ref, identity=forged_ground_identity))

        with pytest.raises(WorkspaceControlError, match="ground scope"):
            workspace.release(forged_ground_parent)

        retained_query_owner = replace(
            output.owner,
            kind="retained-query",
            run_id=None,
            execution_id=None,
            frontier_id=None,
        )
        retained_query_output = RunOutput(
            workspace,
            replace(output.ref, owner=retained_query_owner, descriptor_locator=None),
        )

        with pytest.raises(WorkspaceControlError, match="run-owned"):
            workspace.release(retained_query_output)
        with pytest.raises(WorkspaceControlError, match="run-owned"):
            retained_query_output.inspect()
        with pytest.raises(WorkspaceControlError, match="run-owned"):
            retained_query_output.read_file("candidate.txt")
        with pytest.raises(WorkspaceControlError, match="run-owned"):
            retained_query_output.run_authority()
        with pytest.raises(WorkspaceControlError, match="run-owned"):
            retained_query_output.settlement_policy()
        with pytest.raises(WorkspaceControlError, match="run-owned"):
            retained_query_output.as_readonly_git_repo()

        external_descriptor = replace(output.descriptor, materialization_kind="external")
        external_output = RunOutput(workspace, replace(output.ref, descriptor=external_descriptor))
        with pytest.raises(WorkspaceControlError, match="tree materialization"):
            external_output.as_readonly_git_repo()

        (still_unconsumed,) = workspace.runs.outputs(run_ref=record.run_ref)
        assert still_unconsumed.state == "unconsumed"
        (retained_row,) = workspace.mg.list_retained_outputs(parent=workspace.mg.ground, binding="workspace")
        assert retained_row.state == "unconsumed"
        assert retained_row.settlement is None

        selection = workspace.select(output)
        (selected,) = workspace.runs.outputs(run_ref=record.run_ref, state="selected")

        with pytest.raises(WorkspaceControlError, match="unconsumed"):
            workspace.release(selected)

        (after_stale_rejection,) = workspace.runs.outputs(run_ref=record.run_ref)
        assert after_stale_rejection.state == "selected"
        assert after_stale_rejection.settlement_ref == selection.settlement.settlement_ref
    finally:
        workspace.close()


def test_output_resolution_does_not_read_the_trace_carrier_behaviorally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The BEHAVIORAL architectural gate (the real version of the source-text tripwire in
    # test_output_resolution_boundary.py): make the layer-1 carrier read (VcsCore.read_trace_revision)
    # explode, then prove runs.outputs() (layer 2) still resolves through the TraceStore -- and that
    # runs.trace() (the layer-1 carrier summary) DOES hit it, so the patch is genuinely the carrier path,
    # not a no-op. This catches indirect carrier use through any helper, which the source-text guard cannot.
    source = _write_task_module(
        tmp_path,
        monkeypatch,
        """
def fix_bug(repo: GitRepo, issue: str):
    return repo.write("candidate.txt", f"selected candidate: {issue}\\n".encode())
""",
    )
    root = tmp_path / "ws"
    workspace = _make_workspace(root, explicit_trace_path=False)
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        record = _start_fenced_run(workspace, "sample_tasks.fix_bug", args={"issue": "parser"})
        assert record.status == "retained"
        run_ref = record.run_ref

        def _explode_on_carrier_read(self: object, *args: object, **kwargs: object) -> object:
            raise AssertionError("output resolution must not read the trace-revision carrier")

        monkeypatch.setattr(VcsCore, "read_trace_revision", _explode_on_carrier_read)

        # Layer 2: output resolution must succeed WITHOUT reading the carrier.
        names = [ref.descriptor.output_name for ref in workspace.runs.outputs(run_ref=run_ref)]
        assert "workspace" in names
        # Sanity: the patch is real -- the layer-1 carrier summary DOES read it and now explodes.
        with pytest.raises(AssertionError, match="carrier"):
            workspace.runs.trace(run_ref)
    finally:
        workspace.close()
