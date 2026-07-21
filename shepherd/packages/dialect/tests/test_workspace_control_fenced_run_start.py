"""Fenced ``runs.start`` compatibility guardrails.

The product floor is ``workspace.run(..., repo=...)``. These tests keep
the historical run-start probe loud, opt-in, and routed through the retained
nucleus spine rather than the retired skeleton bridge.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest
from vcs_core import FilesystemSubstrate, MarkerSubstrate, Store, VcsCore, build_builtin_substrate_context
from vcs_core.runtime_substrate import TaskTraceSubstrateDriver

from shepherd_dialect.run_driver import ShepherdRunDriver
from shepherd_dialect.workspace_control import (
    RUN_LEDGER_BINDING,
    RunRecord,
    RunRef,
    RunStartError,
    ShepherdRunLedgerDriver,
    ShepherdTaskArtifactDriver,
    ShepherdTaskLedgerDriver,
    ShepherdWorkspace,
)


def _make_workspace(
    root: Path,
    *,
    explicit_trace_path: bool = True,
) -> ShepherdWorkspace:
    root.mkdir(parents=True, exist_ok=True)
    store = Store(str(root / ".vcscore"))
    context = build_builtin_substrate_context(store=store, workspace=root)
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
    trace_path = root / ".vcscore" / "shepherd" / "trace.sqlite" if explicit_trace_path else None
    return ShepherdWorkspace(mg, trace_store_path=trace_path, workspace_path=root)


def _write_task_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> str:
    module_path = tmp_path / "sample_tasks.py"
    module_path.write_text(_body_with_gitrepo_import(body), encoding="utf-8")
    sys.modules.pop("sample_tasks", None)
    monkeypatch.syspath_prepend(str(tmp_path))
    return "sample_tasks:fix_bug"


def _body_with_gitrepo_import(body: str) -> str:
    if "GitRepo" not in body:
        return body
    stripped = body.lstrip("\n")
    leading = body[: len(body) - len(stripped)]
    gitrepo_import = "from shepherd_runtime.nucleus import GitRepo\n"
    if stripped.startswith(gitrepo_import):
        return body
    return f"{leading}{gitrepo_import}{stripped}"


def _start_fenced_run(
    workspace: ShepherdWorkspace,
    task_ref: str,
    **kwargs: Any,
) -> RunRecord:
    old_value = os.environ.get("SHEPHERD_ENABLE_FENCED_RUN_START")
    os.environ["SHEPHERD_ENABLE_FENCED_RUN_START"] = "1"
    try:
        return workspace.runs.start(task_ref, **kwargs)
    finally:
        if old_value is None:
            os.environ.pop("SHEPHERD_ENABLE_FENCED_RUN_START", None)
        else:
            os.environ["SHEPHERD_ENABLE_FENCED_RUN_START"] = old_value


def test_workspace_control_production_does_not_reintroduce_skeleton_bridge() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src" / "shepherd_dialect" / "workspace_control"
    # These needles must stay specific to the retired deferred-bridge spine.
    # The rename scrub (p030 -> '') collapsed the original bridge-specific
    # tokens onto generic substrings — `_p030_enabled` -> `_enabled` matched
    # legitimate feature-flag/`*_enabled` code all over workspace_control. Keep
    # the module path and the full env-var / flag names; do NOT re-introduce a
    # bare `_enabled` needle.
    forbidden = (
        "shepherd2.vnext import skeleton",
        "shepherd2.vnext.skeleton",
        "SHEPHERD2_SKELETON",
        "SHEPHERD_ENABLE_DEFERRED_BRIDGE",
        "_deferred_bridge_enabled",
    )
    offenders: list[str] = []
    for path in source_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            if needle in text:
                offenders.append(f"{path.relative_to(source_root)}: {needle}")
    assert offenders == []


def test_cli_run_start_help_defaults_to_auto_placement() -> None:
    from click.testing import CliRunner

    from shepherd_dialect import cli

    result = CliRunner().invoke(cli.main, ["run", "start", "--help"], terminal_width=100)

    assert result.exit_code == 0, result.output
    assert "--placement [auto|advisory|jail]" in result.output
    assert "default:" in result.output
    assert "auto" in result.output


def test_run_start_fails_closed_without_fenced_run_start_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = ShepherdWorkspace(object(), trace_store_path=Path(":memory:"))
    monkeypatch.delenv("SHEPHERD_ENABLE_FENCED_RUN_START", raising=False)

    with pytest.raises(RunStartError, match=r"V1D-015: runs\.start is fenced"):
        workspace.runs.start("sample_tasks.fix_bug", args={"issue": "parser"})


def test_cli_run_start_fails_closed_without_fenced_run_start_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from shepherd_dialect import cli

    workspace = ShepherdWorkspace(object(), trace_store_path=Path(":memory:"))
    monkeypatch.setattr(cli, "_open_workspace", lambda *, activate=False: workspace)
    monkeypatch.delenv("SHEPHERD_ENABLE_FENCED_RUN_START", raising=False)

    result = CliRunner().invoke(
        cli.main,
        ["run", "start", "sample_tasks.fix_bug", "--args", '{"issue": "parser"}'],
    )

    assert result.exit_code != 0
    assert "runs.start is fenced" in result.output


@pytest.mark.workspace_smoke
def test_fenced_run_start_routes_through_retained_nucleus_spine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shepherd2.vnext import skeleton

    def fail_bridge_session(*args: object, **kwargs: object) -> None:
        raise AssertionError("runs.start must not route through the skeleton bridge")

    monkeypatch.setattr(skeleton, "Session", fail_bridge_session)
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

        record = _start_fenced_run(
            workspace,
            "sample_tasks.fix_bug",
            args={"issue": "parser"},
            placement="advisory",
        )

        assert record.status == "retained"
        assert record.provider == "shepherd.workspace_control.nucleus.v0"
        assert record.task_id == "sample_tasks.fix_bug"
        assert record.operation_refs.run_start_revision
        assert record.operation_refs.runtime_operation
        assert record.operation_refs.trace_head
        assert record.operation_refs.run_finish_revision is None
        assert len(record.task_resolutions) == 1
        assert record.task_resolutions[0].reason == "run_start"
        assert record.task_resolutions[0].task_lock.task_id == "sample_tasks.fix_bug"
        assert record.task_resolutions[0].task_lock.artifact_digest == record.resolved_task_graph.root.artifact_digest
        assert len(record.task_executions) == 1
        execution = record.task_executions[0]
        assert execution.call_kind == "root_run"
        assert execution.status == "completed"
        assert record.execution_evidence.requested_placement == "advisory"
        assert record.execution_evidence.resolved_placement == "advisory"
        assert record.enforcement == "advisory"
        assert record.execution_evidence.enforcement_basis == "explicit_advisory"
        assert execution.executor_kind == "in_process"
        assert execution.executor_policy == "trusted_bridge"
        assert execution.task_lock.task_id == "sample_tasks.fix_bug"
        assert execution.resolution_id == record.task_resolutions[0].resolution_id
        assert record.outputs["workspace"].trace_ref.frontier_id == record.trace_ref.frontier_id
        assert record.launch_context.launch_surface == "python"
        assert record.launch_context.may_profile == "ReadWrite"
        short_run_selector = record.run_ref[:8]
        assert workspace.runs.show(short_run_selector) == record
        assert workspace.runs.show(RunRef(id=short_run_selector)) is None
        assert workspace.runs.show(RunRef(id=record.run_ref)) == record
        assert workspace.runs.show("@latest") == record
        assert workspace.runs.list() == (record.summary(),)
        trace = workspace.runs.trace(record.run_ref)
        assert trace.summary()["run_ref"] == record.run_ref
        assert workspace.runs.trace(short_run_selector).summary()["run_ref"] == record.run_ref
        assert workspace.runs.trace(RunRef(id=short_run_selector)) is None
        assert trace.summary()["terminal_status"] == "retained"
        (lifecycle,) = trace.filter("run.lifecycle")
        assert lifecycle["transition"] == "retained"
        assert lifecycle["terminal_status"] == "retained"
        assert workspace.runs.vcscore(short_run_selector)["run_ref"] == record.run_ref
        assert workspace.runs.vcscore(RunRef(id=short_run_selector)) is None
        assert (
            workspace.runs.output_citations(run_ref=short_run_selector)[0].output_id
            == record.outputs["workspace"].output_id
        )
        assert workspace.runs.output_citations(run_ref=RunRef(id=short_run_selector)) == ()
        output_refs = workspace.runs.outputs(run_ref=record.run_ref)
        assert len(output_refs) == 1
        assert output_refs[0].state == "unconsumed"
        assert output_refs[0].descriptor.output_name == "workspace"
        assert workspace.runs.outputs(run_ref=short_run_selector)[0].output_id == output_refs[0].output_id
        assert workspace.runs.outputs(run_ref=RunRef(id=short_run_selector)) == ()
        assert workspace.mg.read_selected_binding_revision(RUN_LEDGER_BINDING) is not None
    finally:
        workspace.close()


@pytest.mark.workspace_smoke
def test_runs_outputs_does_not_enable_run_start_execution_flags(
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
    workspace = _make_workspace(root, explicit_trace_path=False)
    try:
        workspace.tasks.register(source, may_default="ReadWrite")
        record = _start_fenced_run(
            workspace,
            "sample_tasks.fix_bug",
            args={"issue": "parser"},
            placement="advisory",
        )
        assert record.status == "retained"
        for name in ("SHEPHERD2_SKELETON", "VCS_CORE_NESTED_OPERATIONS"):
            monkeypatch.delenv(name, raising=False)

        assert [ref.descriptor.output_name for ref in workspace.runs.outputs(run_ref=record.run_ref)]
        for name in ("SHEPHERD2_SKELETON", "VCS_CORE_NESTED_OPERATIONS"):
            assert os.environ.get(name) is None
    finally:
        workspace.close()


@pytest.mark.workspace_smoke
def test_fenced_run_start_records_failed_terminal_record(
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

        with pytest.raises(RunStartError, match="cannot fix parser"):
            _start_fenced_run(
                workspace,
                "sample_tasks.fix_bug",
                args={"issue": "parser"},
                placement="advisory",
            )

        record = workspace.runs.show("@latest")
        assert record is not None
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
    finally:
        workspace.close()
