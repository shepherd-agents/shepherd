"""Scope lifecycle CLI integration tests."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner
from vcs_core._materialization_run import MaterializationRun, write_materialization_run
from vcs_core._world_storage_installation import open_existing_default_world_storage
from vcs_core.cli import main
from vcs_core.vcscore import VcsCore

from ...support.cli import init_repo as _init

if TYPE_CHECKING:
    from pathlib import Path

    from vcs_core._projection_store import ScopeRegistryEntry


def _scope_registry_entry(workspace: Path, scope_name: str) -> ScopeRegistryEntry:
    mg = VcsCore.from_config(str(workspace))
    snapshot = mg.store.require_scope_registry_projection()
    return snapshot.entries_by_name[scope_name]


def _scope_file_mode(workspace: Path, scope_name: str, path: str) -> int | None:
    mg = VcsCore.from_config(str(workspace))
    entry = _scope_registry_entry(workspace, scope_name)
    return mg.store.workspace_file_mode(entry.ref, path)


def _mark_scope_registry_isolated(workspace: Path, scope_name: str) -> None:
    mg = VcsCore.from_config(str(workspace))
    snapshot = mg.store.load_scope_registry_projection()
    assert snapshot is not None
    entry = snapshot.entries_by_name[scope_name]
    updated_entries = tuple(
        sorted(
            (
                replace(item, isolation_mode="isolated") if item.name == scope_name else item
                for item in snapshot.entries
            ),
            key=lambda item: item.ref,
        )
    )
    assert mg.store.publish_scope_registry_projection(
        entries=updated_entries,
        expected_head_oid=snapshot.head_oid,
        expected_source_digest=snapshot.source_digest,
    )


def test_branch_creates_scope(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    result = runner.invoke(main, ["branch", "task-1"])
    assert result.exit_code == 0, result.output
    assert "Created scope 'task-1'" in result.output

    state_file = tmp_path / ".vcscore" / "cli_state.json"
    assert not state_file.exists()
    assert _scope_registry_entry(tmp_path, "task-1").status == "live"


def test_branch_does_not_warn_about_expected_open_scope_persistence(
    tmp_path: Path,
    caplog,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    with caplog.at_level(logging.WARNING, logger="vcs_core.vcscore"):
        result = runner.invoke(main, ["branch", "task-1"])

    assert result.exit_code == 0, result.output
    assert "Deactivating with" not in caplog.text


def test_branch_with_parent(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-1"])
    result = runner.invoke(main, ["branch", "step-A", "--parent", "task-1"])
    assert result.exit_code == 0, result.output
    assert "Created scope 'step-A' from 'task-1'" in result.output


def test_branch_rejects_second_live_child_for_parent(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    first = runner.invoke(main, ["branch", "task-1"])
    assert first.exit_code == 0, first.output

    second = runner.invoke(main, ["branch", "task-2"])

    assert second.exit_code != 0
    assert "already has live child scope 'task-1'" in second.output


@pytest.mark.parametrize("name", ["bad/name", "../escape", ".hidden", "space name"])
def test_branch_rejects_invalid_scope_names_without_traceback(tmp_path: Path, name: str) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    result = runner.invoke(main, ["branch", name])

    assert result.exit_code == 2
    assert "Error: cannot branch:" in result.output
    assert "Traceback" not in result.output
    assert "InvalidSpecError" not in result.output


def test_exec_rejects_invalid_scope_name_before_dispatch(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    result = runner.invoke(main, ["exec", "marker", "mark", "--scope", "bad/name", "-p", "label=test"])

    assert result.exit_code == 2
    assert "Error: cannot exec:" in result.output
    assert "contains '/'" in result.output


def test_branch_rejects_isolated_scope_in_stateless_cli(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    result = runner.invoke(main, ["branch", "task-iso", "--isolated"])
    assert result.exit_code != 0
    assert "persistent session" in result.output


def test_merge_scope(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-1"])
    runner.invoke(main, ["exec", "marker", "mark", "--scope", "task-1", "-p", "label=test"])
    result = runner.invoke(main, ["merge", "task-1"])
    assert result.exit_code == 0, result.output
    assert "Merged 'task-1' into 'ground'" in result.output


def test_discard_scope(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-1"])
    runner.invoke(main, ["exec", "marker", "mark", "--scope", "task-1", "-p", "label=test"])
    result = runner.invoke(main, ["discard", "task-1"])
    assert result.exit_code == 0, result.output
    assert "Discarded 'task-1'" in result.output


def test_exec_on_scope(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-1"])
    result = runner.invoke(main, ["exec", "marker", "mark", "--scope", "task-1", "-p", "label=checkpoint"])
    assert result.exit_code == 0, result.output
    assert "Recorded 1 effect" in result.output


def test_log_scope_shows_live_scope_history_before_merge(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-log"])
    runner.invoke(main, ["exec", "marker", "mark", "--scope", "task-log", "-p", "label=live-history"])

    unscoped_result = runner.invoke(main, ["log", "--effect-type", "Marker"])
    scoped_result = runner.invoke(main, ["log", "--effect-type", "Marker", "--scope", "task-log"])

    assert unscoped_result.exit_code == 0, unscoped_result.output
    assert "scope:task-log" not in unscoped_result.output
    assert scoped_result.exit_code == 0, scoped_result.output
    assert "Marker" in scoped_result.output
    assert "scope:task-log" in scoped_result.output


def test_filesystem_exec_accepts_user_facing_filemode(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-1"])
    payload = tmp_path.parent / f"{tmp_path.name}-script-ok.payload"
    payload.write_text("#!/bin/sh")

    result = runner.invoke(
        main,
        [
            "exec",
            "filesystem",
            "write",
            "--scope",
            "task-1",
            "-p",
            "path=script.sh",
            "-p",
            f"content=@{payload}",
            "-p",
            "mode=100755",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _scope_file_mode(tmp_path, "task-1", "script.sh") == 0o100755


def test_filesystem_exec_rejects_invalid_filemode_cleanly(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-1"])
    payload = tmp_path.parent / f"{tmp_path.name}-script-bad.payload"
    payload.write_text("#!/bin/sh")

    result = runner.invoke(
        main,
        [
            "exec",
            "filesystem",
            "write",
            "--scope",
            "task-1",
            "-p",
            "path=script.sh",
            "-p",
            f"content=@{payload}",
            "-p",
            "mode=123",
        ],
    )

    assert result.exit_code != 0
    assert "Error: Git filemode must be 100644 or 100755." in result.output


def test_exec_marker_on_scope(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-1"])
    result = runner.invoke(main, ["exec", "marker", "mark", "--scope", "task-1", "-p", "label=test"])
    assert result.exit_code == 0, result.output
    assert "Recorded 1 effect" in result.output


def test_exec_object_param_on_scope(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-1"])

    result = runner.invoke(
        main,
        ["exec", "marker", "mark", "--scope", "task-1", "-p", "label=test", "-p", 'metadata={"phase":"start"}'],
    )

    assert result.exit_code == 0, result.output
    assert "Recorded 1 effect" in result.output


def test_exec_rejects_unknown_schema_command(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-1"])

    result = runner.invoke(main, ["exec", "filesystem", "rename", "--scope", "task-1", "-p", "path=old.py"])

    assert result.exit_code != 0
    assert "unknown filesystem command" in result.output


def test_exec_rejects_missing_required_schema_param(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-1"])

    result = runner.invoke(main, ["exec", "filesystem", "write", "--scope", "task-1", "-p", "path=hello.txt"])

    assert result.exit_code != 0
    assert "missing required parameter 'content'" in result.output


def test_exec_supports_schema_typed_json_metadata(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-1"])

    result = runner.invoke(
        main,
        ["exec", "marker", "mark", "--scope", "task-1", "-p", "label=test", "-p", 'metadata={"phase":"start"}'],
    )

    assert result.exit_code == 0, result.output

    merge = runner.invoke(main, ["merge", "task-1"])
    assert merge.exit_code == 0, merge.output

    result = runner.invoke(main, ["log", "--effect-type", "Marker"])
    assert result.exit_code == 0
    assert "Marker" in result.output


def test_merge_nonexistent_errors(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    result = runner.invoke(main, ["merge", "nonexistent"])
    assert result.exit_code != 0
    assert "Error: no tracked scope 'nonexistent'." in result.output


def test_missing_scope_errors_are_not_registry_mismatches(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    cases = [
        ["branch", "child", "--parent", "missing"],
        ["discard", "missing"],
        ["exec", "marker", "mark", "--scope", "missing", "-p", "label=missing"],
    ]
    for args in cases:
        result = runner.invoke(main, args)
        assert result.exit_code != 0
        assert result.output == "Error: no tracked scope 'missing'.\n"


def test_merge_ground_reports_live_child_scope_error(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    result = runner.invoke(main, ["merge", "ground"])

    assert result.exit_code != 0
    assert "scope 'ground' is not a live child scope." in result.output
    assert "AppScopeResolutionError" not in result.output


def test_branch_merge_push_round_trip(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-1"])
    payload = tmp_path.parent / f"{tmp_path.name}-hello.payload"
    payload.write_text("hello")
    runner.invoke(
        main,
        ["exec", "filesystem", "write", "--scope", "task-1", "-p", "path=hello.txt", "-p", f"content=@{payload}"],
    )
    runner.invoke(main, ["merge", "task-1"])

    result = runner.invoke(main, ["push"])
    assert result.exit_code == 0, result.output
    assert "Materialized" in result.output

    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert f"Managed workspace: {tmp_path.resolve()}" in result.output
    assert "Environment: host state outside workspace is untracked" in result.output
    assert "Commits ahead: 0" in result.output


def test_push_reports_live_scope_not_orphaned_prior_session(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    branch = runner.invoke(main, ["branch", "task"])
    assert branch.exit_code == 0, branch.output

    result = runner.invoke(main, ["push"])

    assert result.exit_code != 0
    assert "Live scope 'task' must be merged or discarded before materialization." in result.output
    assert "orphan" not in result.output.lower()
    assert "prior session" not in result.output.lower()
    assert result.exception is not None
    assert result.exception.__class__.__name__ == "SystemExit"


def test_archive_orphaned_scopes_does_not_archive_registry_live_scope(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    branch = runner.invoke(main, ["branch", "task"])
    assert branch.exit_code == 0, branch.output

    result = runner.invoke(main, ["archive-orphaned-scopes"])

    assert result.exit_code == 0, result.output
    assert "No orphaned scopes." in result.output
    entry = _scope_registry_entry(tmp_path, "task")
    assert entry.status == "live"
    assert VcsCore.from_config(str(tmp_path)).store.ref_exists(entry.ref)


def test_status_does_not_report_registry_live_scope_as_orphaned(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    branch = runner.invoke(main, ["branch", "task"])
    assert branch.exit_code == 0, branch.output

    result = runner.invoke(main, ["status"])

    assert result.exit_code == 0, result.output
    assert "Recovery:" not in result.output
    assert "orphan" not in result.output.lower()


def test_status_reports_orphaned_operations(tmp_path: Path) -> None:
    from vcs_core._lock import release_session_lock

    runner = CliRunner()
    _init(runner, tmp_path)

    m1 = VcsCore(str(tmp_path))
    m1.activate()
    task = m1.fork(m1.ground, "task-orphan-status")
    with m1._lock, m1._scoped(task):
        m1._pipeline.begin_operation(handle_id="op-status", kind="test.operation", scope=task)
    m1._pipeline.reset()
    m1._active_scopes.clear()
    m1._scope_parents.clear()
    m1._isolated_scopes.clear()
    m1._restored_scopes.clear()
    m1._patch_manager.uninstall_all()
    for substrate in reversed(m1.lifecycle_substrates):
        substrate.deactivate()
    release_session_lock(m1._repo_path, m1._session_id)

    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "Recovery:" in result.output
    assert "op-status" in result.output


def test_run_auto_recovers_orphaned_operation_while_status_still_reports(tmp_path: Path) -> None:
    """'Just run it again': `vcs-core run` reclaims a dead prior run's orphaned operation and
    proceeds, while read-only `status` still *reports* it — a read never silently mutates."""
    from vcs_core._lock import release_session_lock

    runner = CliRunner()
    _init(runner, tmp_path)

    # a run killed mid-operation: an open ground operation ref left behind, lock released
    m1 = VcsCore(str(tmp_path))
    m1.activate()
    with m1._lock:
        m1._pipeline.reset()
        m1._pipeline.begin_operation(handle_id="op-run-wedge", kind="test.operation", scope=m1.ground)
    m1._pipeline.reset()
    m1._active_scopes.clear()
    m1._scope_parents.clear()
    m1._isolated_scopes.clear()
    m1._restored_scopes.clear()
    m1._patch_manager.uninstall_all()
    for substrate in reversed(m1.lifecycle_substrates):
        substrate.deactivate()
    release_session_lock(m1._repo_path, m1._session_id)

    # read-only `status` still surfaces the orphan (no silent mutation on a read)
    status = runner.invoke(main, ["status"])
    assert status.exit_code == 0, status.output
    assert "op-run-wedge" in status.output

    # `vcs-core run` reclaims the dead orphan and proceeds — no manual recovery step
    script = tmp_path / "work.py"
    script.write_text("print('ran')\n")
    result = runner.invoke(main, ["run", str(script)])
    assert result.exit_code == 0, result.output

    # ...and the wedge is gone afterward
    after = runner.invoke(main, ["status"])
    assert "op-run-wedge" not in after.output


def test_status_reports_operation_journal_recovery(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate()
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")
        manager = open_existing_default_world_storage(mg._repo_path)
        manager.open_operation_journal(
            operation_id="op-status-journal",
            operation_kind="shepherd.task",
            target_ref=mg.ground.ref,
            input_world_oid=None,
        )
    finally:
        mg.deactivate()

    result = runner.invoke(main, ["status"])

    assert result.exit_code == 0, result.output
    assert "Recovery:" in result.output
    assert "Operation journals:" in result.output
    assert "op-status-journal" in result.output


def test_status_reports_materialization_recovery(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    write_materialization_run(
        mg._repo_path,
        MaterializationRun(
            session_id="session-status",
            run_id="run-status-materialization",
            timestamp=1.0,
            planned_unit_ids=("filesystem:workspace",),
        ),
    )

    result = runner.invoke(main, ["status"])

    assert result.exit_code == 0, result.output
    assert "Recovery:" in result.output
    assert "Materialization recovery:" in result.output
    assert "run-status-materialization" in result.output


def test_activate_recover_repairs_corrupt_materialization_state(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    repo_path = tmp_path / ".vcscore"
    (repo_path / "dirty").write_text("{not json")
    (repo_path / "materialization-run.json").write_text("{not json")

    result = runner.invoke(main, ["activate", "--recover", "repair", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Repository validated." in result.output
    assert not (repo_path / "dirty").exists()
    assert not (repo_path / "materialization-run.json").exists()


def test_recover_materialization_cli_clears_run_only_ledger(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    mg = VcsCore.from_config(str(tmp_path))
    write_materialization_run(
        mg._repo_path,
        MaterializationRun(
            session_id="session-cli-recover",
            run_id="run-cli-recover",
            timestamp=1.0,
            planned_unit_ids=("filesystem:workspace",),
        ),
    )

    result = runner.invoke(main, ["recover-materialization", "--mode", "repair", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Materialization recovery completed (repair)." in result.output
    assert "Cleared materialization run ledger." in result.output
    assert not (tmp_path / ".vcscore" / "materialization-run.json").exists()


def test_archive_orphaned_operations_cli(tmp_path: Path) -> None:
    from vcs_core._lock import release_session_lock

    runner = CliRunner()
    _init(runner, tmp_path)

    m1 = VcsCore(str(tmp_path))
    m1.activate()
    task = m1.fork(m1.ground, "task-orphan-cli-cleanup")
    with m1._lock, m1._scoped(task):
        m1._pipeline.begin_operation(handle_id="op-cleanup", kind="test.operation", scope=task)
    m1._pipeline.reset()
    m1._active_scopes.clear()
    m1._scope_parents.clear()
    m1._isolated_scopes.clear()
    m1._restored_scopes.clear()
    m1._patch_manager.uninstall_all()
    for substrate in reversed(m1.lifecycle_substrates):
        substrate.deactivate()
    release_session_lock(m1._repo_path, m1._session_id)

    result = runner.invoke(main, ["archive-orphaned-operations"])
    assert result.exit_code == 0, result.output
    assert "Archived 1 orphaned operation(s)" in result.output
    assert "op-cleanup" in result.output

    status = runner.invoke(main, ["status"])
    assert status.exit_code == 0, status.output
    assert "Orphaned operations:" not in status.output


def test_scope_registry_survives_across_stateless_invocations(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    result = runner.invoke(main, ["branch", "persistent"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, ["exec", "marker", "mark", "--scope", "persistent", "-p", "label=survived"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, ["merge", "persistent"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, ["log"])
    assert result.exit_code == 0


def test_merge_already_merged_scope_reports_terminal_state(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    result = runner.invoke(main, ["branch", "done"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, ["merge", "done"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, ["merge", "done"])
    assert result.exit_code == 1
    assert "scope 'done' is already merged and is no longer live" in result.output
    assert "no tracked scope" not in result.output


def test_discard_already_discarded_scope_reports_terminal_state(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    result = runner.invoke(main, ["branch", "doomed"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, ["discard", "doomed"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, ["discard", "doomed"])
    assert result.exit_code == 1
    assert "scope 'doomed' is already discarded and is no longer live" in result.output
    assert "no tracked scope" not in result.output


def test_stateless_cli_restore_preserves_registry_scope_world_id(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    branch = runner.invoke(main, ["branch", "task-world"])
    assert branch.exit_code == 0, branch.output

    entry = _scope_registry_entry(tmp_path, "task-world")
    assert entry.world_id is not None

    result = runner.invoke(main, ["exec", "marker", "mark", "--scope", "task-world", "-p", "label=world-id"])
    assert result.exit_code == 0, result.output

    mg = VcsCore(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.restore_scope(
            name=entry.name,
            ref=entry.ref,
            instance_id=entry.instance_id,
            creation_oid=entry.creation_oid,
            parent=mg.ground,
            world_id=entry.world_id,
        )
        entries = mg.log(ref=task.ref, max_count=3)
    finally:
        mg.deactivate()

    marker = next(commit for commit in entries if commit.metadata.get("type") == "Marker")
    assert marker.metadata["mg"]["world"]["id"] == entry.world_id
    assert marker.metadata["mg"]["world"]["instance_id"] == entry.instance_id
    assert "Recorded 1 effect" in result.output


def test_stateless_cli_without_scope_rejects_when_live_scope_exists(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task"])

    result = runner.invoke(main, ["exec", "marker", "mark", "-p", "label=test"])
    assert result.exit_code != 0
    assert "live scope(s) exist (task)" in result.output
    assert "pass `--scope ground` or `--scope <name>` explicitly" in result.output


def test_stateless_cli_explicit_ground_scope_records_on_ground_with_live_scope(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task"])

    result = runner.invoke(main, ["exec", "marker", "mark", "--scope", "ground", "-p", "label=test"])
    assert result.exit_code == 0, result.output

    registry_entry = _scope_registry_entry(tmp_path, "task")
    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        ground_entries = mg.log(ref=mg.ground.ref, max_count=3)
        task = mg.restore_scope(
            name=registry_entry.name,
            ref=registry_entry.ref,
            instance_id=registry_entry.instance_id,
            creation_oid=registry_entry.creation_oid,
            parent=mg.ground,
            world_id=registry_entry.world_id,
        )
        task_entries = mg.log(ref=task.ref, max_count=3)
    finally:
        mg.deactivate()

    assert any(entry.metadata.get("type") == "Marker" for entry in ground_entries)
    assert not any(entry.metadata.get("type") == "Marker" for entry in task_entries)


def test_exec_rejects_explicit_isolated_scope_in_stateless_cli(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task"])
    _mark_scope_registry_isolated(tmp_path, "task")

    result = runner.invoke(main, ["exec", "marker", "mark", "--scope", "task", "-p", "label=test"])

    assert result.exit_code != 0
    assert "isolated scopes require a persistent session" in result.output
    assert "stateless CLI cannot exec isolated scope 'task'" in result.output


def test_merge_rejects_restored_isolated_scope(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task"])
    _mark_scope_registry_isolated(tmp_path, "task")

    result = runner.invoke(main, ["merge", "task"])

    assert result.exit_code != 0
    assert "cannot merge isolated scope 'task'" in result.output


def test_discard_rejects_restored_isolated_scope(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task"])
    _mark_scope_registry_isolated(tmp_path, "task")

    result = runner.invoke(main, ["discard", "task"])

    assert result.exit_code != 0
    assert "cannot discard isolated scope 'task'" in result.output
