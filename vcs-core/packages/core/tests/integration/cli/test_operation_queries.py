"""Operation-query CLI integration tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner
from vcs_core.cli import main
from vcs_core.types import EffectRecord
from vcs_core.vcscore import VcsCore

from ...support.cli import init_repo as _init

if TYPE_CHECKING:
    from pathlib import Path

    from vcs_core.types import ScopeInfo


def _dirty_shutdown(mg: VcsCore) -> None:
    from vcs_core._lock import release_session_lock

    mg._pipeline.reset()
    mg._active_scopes.clear()
    mg._scope_parents.clear()
    mg._isolated_scopes.clear()
    mg._restored_scopes.clear()
    mg._patch_manager.uninstall_all()
    for substrate in reversed(mg.lifecycle_substrates):
        substrate.deactivate()
    release_session_lock(mg._repo_path, mg._session_id)


def _restored_vcscore(tmp_path: Path) -> tuple[VcsCore, dict[str, ScopeInfo]]:
    from vcs_core.store import GROUND_REF

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    scopes: dict[str, ScopeInfo] = {"ground": mg.ground}
    scopes_by_ref = {GROUND_REF: mg.ground}
    snapshot = mg.store.require_scope_registry_projection()
    remaining = [entry for entry in snapshot.entries if entry.status == "live"]
    while remaining:
        progress = False
        next_remaining = []
        for entry in remaining:
            parent = scopes_by_ref.get(entry.parent_ref)
            if parent is None:
                next_remaining.append(entry)
                continue
            scope = mg.restore_scope(
                name=entry.name,
                ref=entry.ref,
                instance_id=entry.instance_id,
                creation_oid=entry.creation_oid,
                world_id=entry.world_id,
                parent=parent,
                isolated=entry.isolation_mode == "isolated",
            )
            scopes[entry.name] = scope
            scopes_by_ref[entry.ref] = scope
            progress = True
        if not progress:
            break
        remaining = next_remaining
    return mg, scopes


def _record_marker_runtime_effect(mg: VcsCore, scope: ScopeInfo, *, label: str = "hello") -> None:
    mg._record_runtime_effects(
        [EffectRecord(effect_type="Marker", metadata={"label": label})],
        substrate="marker",
        scope=scope,
        boundary_policy="append_or_root",
    )


def test_operations_lists_visible_operations_on_ground(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    runner.invoke(main, ["branch", "task-visible"])
    runner.invoke(main, ["exec", "marker", "mark", "--scope", "task-visible", "-p", "label=visible-checkpoint"])
    runner.invoke(main, ["merge", "task-visible"])

    result = runner.invoke(main, ["operations"])

    assert result.exit_code == 0, result.output
    assert "marker-mark" in result.output
    assert "[visible/ok]" in result.output


def test_operations_without_scope_uses_ground_not_hidden_stateless_scope(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    runner.invoke(main, ["branch", "task-default"])
    runner.invoke(main, ["exec", "marker", "mark", "--scope", "task-default", "-p", "label=default-scope"])
    mg, scopes = _restored_vcscore(tmp_path)
    task = scopes["task-default"]
    mg.deactivate()

    result = runner.invoke(main, ["operations", "--all"])

    assert result.exit_code == 0, result.output
    assert f"world:{task.world_id} (task-default)" not in result.output
    assert "default-scope" not in result.output
    assert "Orphaned scope refs detected" not in result.output

    scoped_result = runner.invoke(main, ["operations", "--all", "--scope", "task-default"])
    assert scoped_result.exit_code == 0, scoped_result.output
    assert f"world:{task.world_id} (task-default)" in scoped_result.output
    assert "[visible/ok]" in scoped_result.output


def test_operations_lists_open_and_archived_modes(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    open_branch = runner.invoke(main, ["branch", "task-open"])
    archived_branch = runner.invoke(main, ["branch", "task-archived", "--parent", "task-open"])
    assert open_branch.exit_code == 0, open_branch.output
    assert archived_branch.exit_code == 0, archived_branch.output

    mg, scopes = _restored_vcscore(tmp_path)
    open_task = scopes["task-open"]
    archived_task = scopes["task-archived"]

    with (
        pytest.raises(RuntimeError, match="boom"),
        mg.runtime_activity(
            scope=archived_task,
            operation_id="archived-op",
            operation_label="Archived Op",
            operation_kind="test.archived",
            failure_policy="abort_archive",
        ),
    ):
        raise RuntimeError("boom")

    with mg._lock:
        mg._pipeline.set_scope(open_task)
        mg._pipeline.begin_operation(
            handle_id="open-op",
            kind="test.open",
            scope=open_task,
            operation_id="open-op",
            operation_label="Open Op",
        )
    _dirty_shutdown(mg)

    mg, scopes = _restored_vcscore(tmp_path)
    archived_task = scopes["task-archived"]
    mg.deactivate()

    open_result = runner.invoke(main, ["operations", "--open", "--scope", "task-open"])
    archived_result = runner.invoke(main, ["operations", "--archived", "--scope", "task-archived"])

    assert open_result.exit_code == 0, open_result.output
    assert "open-op  [staged/open]" in open_result.output
    assert f"world:{open_task.world_id} (task-open)" in open_result.output
    assert "[staged/open]" in open_result.output

    assert archived_result.exit_code == 0, archived_result.output
    assert "archived-op  [archived/error]" in archived_result.output
    assert f"world:{archived_task.world_id} (task-archived)" in archived_result.output
    assert "[archived/error]" in archived_result.output
    assert "archived via: archived operation ref" in archived_result.output


def test_operation_show_renders_history_for_scope_selector(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-inspect"])

    mg, scopes = _restored_vcscore(tmp_path)
    task = scopes["task-inspect"]
    with mg.runtime_activity(
        scope=task,
        operation_label="Inspect Me",
        operation_kind="test.inspect",
    ):
        _record_marker_runtime_effect(mg, task)
    operation = next(summary for summary in mg.visible_operations(ref=task.ref) if summary.effect_count > 0)
    mg.deactivate()

    result = runner.invoke(main, ["operation", "show", operation.operation_id, "--scope", "task-inspect"])

    assert result.exit_code == 0, result.output
    assert f"Operation:    {operation.operation_id}" in result.output
    assert "Label:        " in result.output
    assert "World:        task-inspect" in result.output
    assert f"World ID:     {task.world_id}" in result.output
    assert "Phase:        completed" in result.output
    assert "Carrier:      refs/vcscore/scopes/task-inspect" in result.output
    assert "Anchor:       " in result.output
    assert "Meta-Effect:" not in result.output
    assert "Marker" in result.output


def test_operation_show_without_scope_uses_repo_wide_selector_resolution(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-default-show"])
    runner.invoke(main, ["exec", "marker", "mark", "--scope", "task-default-show", "-p", "label=default-show"])

    mg, scopes = _restored_vcscore(tmp_path)
    task = scopes["task-default-show"]
    operation = next(summary for summary in mg.visible_operations(ref=task.ref) if summary.effect_count > 0)
    mg.deactivate()

    result = runner.invoke(main, ["operation", "show", str(operation.operation_id)])

    assert result.exit_code == 0, result.output
    assert f"Operation:    {operation.operation_id}" in result.output
    assert "World:        task-default-show" in result.output
    assert f"World ID:     {task.world_id}" in result.output
    assert "Phase:        completed" in result.output
    assert "Anchor:       " in result.output
    assert "Meta-Effect:" not in result.output
    assert "Marker" in result.output


def test_operation_show_rejects_ambiguous_visible_carrier_ref(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-ambiguous-show"])

    mg, scopes = _restored_vcscore(tmp_path)
    task = scopes["task-ambiguous-show"]
    with mg.runtime_activity(
        scope=task,
        operation_label="first",
        operation_kind="test.inspect",
    ):
        pass
    with mg.runtime_activity(
        scope=task,
        operation_label="second",
        operation_kind="test.inspect",
    ):
        pass
    mg.deactivate()

    result = runner.invoke(
        main,
        ["operation", "show", "refs/vcscore/scopes/task-ambiguous-show", "--scope", "task-ambiguous-show"],
    )

    assert result.exit_code != 0
    assert "Ambiguous operation selector" in result.output


def test_archived_operations_remain_browsable_after_scope_discard(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-archived"])

    mg, scopes = _restored_vcscore(tmp_path)
    archived_task = scopes["task-archived"]
    with (
        pytest.raises(RuntimeError, match="boom"),
        mg.runtime_activity(
            scope=archived_task,
            operation_id="archived-op",
            operation_label="Archived Op",
            operation_kind="test.archived",
            failure_policy="abort_archive",
        ),
    ):
        raise RuntimeError("boom")
    mg.deactivate()

    discard_result = runner.invoke(main, ["discard", "task-archived"])
    archived_result = runner.invoke(main, ["operations", "--archived"])
    all_result = runner.invoke(main, ["operations", "--all"])
    show_result = runner.invoke(main, ["operation", "show", "archived-op"])

    assert discard_result.exit_code == 0, discard_result.output
    assert archived_result.exit_code == 0, archived_result.output
    assert all_result.exit_code == 0, all_result.output
    assert show_result.exit_code == 0, show_result.output
    assert "archived-op  [archived/error]" in archived_result.output
    assert "archived-op  [archived/error]" in all_result.output
    assert "archived via: archived operation ref" in archived_result.output
    assert "archived via: archived operation ref" in all_result.output
    assert "Operation:    archived-op" in show_result.output
    assert f"World ID:     {archived_task.world_id}" in show_result.output
    assert "Phase:        aborted" in show_result.output
    assert "Archived via: archived operation ref" in show_result.output
    assert "Meta-Effect:" not in show_result.output


def test_completed_operations_remain_browsable_after_scope_discard(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-completed-archived"])

    mg, scopes = _restored_vcscore(tmp_path)
    task = scopes["task-completed-archived"]
    with mg.runtime_activity(
        scope=task,
        operation_id="completed-archived-op",
        operation_label="Completed Archived Op",
        operation_kind="test.completed.archived",
    ):
        _record_marker_runtime_effect(mg, task)
    mg.deactivate()

    discard_result = runner.invoke(main, ["discard", "task-completed-archived"])
    archived_result = runner.invoke(main, ["operations", "--archived"])
    all_result = runner.invoke(main, ["operations", "--all"])
    show_result = runner.invoke(main, ["operation", "show", "completed-archived-op"])

    assert discard_result.exit_code == 0, discard_result.output
    assert archived_result.exit_code == 0, archived_result.output
    assert all_result.exit_code == 0, all_result.output
    assert show_result.exit_code == 0, show_result.output
    assert "completed-archived-op  [archived/ok]" in archived_result.output
    assert "completed-archived-op  [archived/ok]" in all_result.output
    assert "archived via: discarded world" in archived_result.output
    assert "archived via: discarded world" in all_result.output
    assert "Operation:    completed-archived-op" in show_result.output
    assert f"World ID:     {task.world_id}" in show_result.output
    assert "Visibility:   archived" in show_result.output
    assert "Phase:        completed" in show_result.output
    assert "Archived via: discarded world" in show_result.output
    assert "Carrier:      refs/vcscore/archive/task-completed-archived-" in show_result.output
    assert "Meta-Effect:" not in show_result.output


def test_raw_history_surfaces_can_include_structural_records_while_operations_surfaces_stay_operation_only(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-merged-structural"])
    merge_exec_result = runner.invoke(
        main,
        ["exec", "marker", "mark", "--scope", "task-merged-structural", "-p", "label=raw-history-structural"],
    )
    assert merge_exec_result.exit_code == 0, merge_exec_result.output
    merge_result = runner.invoke(main, ["merge", "task-merged-structural"])
    assert merge_result.exit_code == 0, merge_result.output

    runner.invoke(main, ["branch", "task-structural-archive"])

    mg, scopes = _restored_vcscore(tmp_path)
    task = scopes["task-structural-archive"]
    with mg.runtime_activity(
        scope=task,
        operation_id="structural-archive-op",
        operation_label="Structural Archive Op",
        operation_kind="test.completed.archived",
    ):
        _record_marker_runtime_effect(mg, task)
    mg.deactivate()

    discard_result = runner.invoke(main, ["discard", "task-structural-archive"])
    assert discard_result.exit_code == 0, discard_result.output

    raw_log_result = runner.invoke(main, ["log", "-n", "20"])
    archived_result = runner.invoke(main, ["operations", "--archived"])
    show_result = runner.invoke(main, ["operation", "show", "structural-archive-op"])

    assert raw_log_result.exit_code == 0, raw_log_result.output
    assert archived_result.exit_code == 0, archived_result.output
    assert show_result.exit_code == 0, show_result.output
    assert "ScopeMerge" in raw_log_result.output
    assert "DiscardSnapshot" not in archived_result.output
    assert "ScopeMerge" not in archived_result.output
    assert "DiscardSnapshot" not in show_result.output
    assert "ScopeMerge" not in show_result.output

    mg, _ = _restored_vcscore(tmp_path)
    archive_ref = next(
        ref for ref in mg.store.list_archive_refs() if ref.startswith("refs/vcscore/archive/task-structural-archive-")
    )
    archived_log = mg.log(ref=archive_ref, max_count=10)
    mg.deactivate()

    assert any(entry.metadata["type"] == "DiscardSnapshot" for entry in archived_log)


def test_operation_show_finds_discarded_history_by_operation_id(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-capped-discarded-history-target"])

    mg, scopes = _restored_vcscore(tmp_path)
    target = scopes["task-capped-discarded-history-target"]
    with mg.runtime_activity(
        scope=target,
        operation_id="old-discarded-op-id",
        operation_label="old-discarded-op",
        operation_kind="test.completed.archived",
    ):
        pass
    mg.deactivate()

    discard_result = runner.invoke(main, ["discard", "task-capped-discarded-history-target"])
    assert discard_result.exit_code == 0, discard_result.output

    for idx in range(2):
        scope_name = f"task-capped-discarded-history-{idx}"
        runner.invoke(main, ["branch", scope_name])
        mg, scopes = _restored_vcscore(tmp_path)
        task = scopes[scope_name]
        with mg.runtime_activity(
            scope=task,
            operation_id=f"newer-discarded-op-{idx}",
            operation_label=f"newer-discarded-op-{idx}",
            operation_kind="test.completed.archived",
        ):
            pass
        mg.deactivate()
        loop_discard = runner.invoke(main, ["discard", scope_name])
        assert loop_discard.exit_code == 0, loop_discard.output

    show_result = runner.invoke(main, ["operation", "show", "old-discarded-op-id"])

    assert show_result.exit_code == 0, show_result.output
    assert "Operation:    old-discarded-op-id" in show_result.output
    assert "Visibility:   archived" in show_result.output
    assert "Carrier:      refs/vcscore/archive/task-capped-discarded-history-target-" in show_result.output


def test_recovery_omits_discarded_world_completed_history(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-clean-discard"])

    mg, scopes = _restored_vcscore(tmp_path)
    task = scopes["task-clean-discard"]
    with mg.runtime_activity(
        scope=task,
        operation_id="clean-discard-op",
        operation_label="Clean Discard Op",
        operation_kind="test.completed.archived",
    ):
        _record_marker_runtime_effect(mg, task)
    mg.deactivate()

    discard_result = runner.invoke(main, ["discard", "task-clean-discard"])
    archived_result = runner.invoke(main, ["operations", "--archived"])
    recovery_result = runner.invoke(main, ["recovery"])

    assert discard_result.exit_code == 0, discard_result.output
    assert archived_result.exit_code == 0, archived_result.output
    assert recovery_result.exit_code == 0, recovery_result.output
    assert "clean-discard-op  [archived/ok]" in archived_result.output
    assert "No recovery state." in recovery_result.output


def test_recovery_keeps_archived_failures_when_clean_discards_exceed_cap(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    branch_result = runner.invoke(main, ["branch", "task-archived-failure"])
    assert branch_result.exit_code == 0, branch_result.output

    mg, scopes = _restored_vcscore(tmp_path)
    failed = scopes["task-archived-failure"]
    with (
        pytest.raises(RuntimeError, match="boom"),
        mg.runtime_activity(
            scope=failed,
            operation_id="failed-archived-op-id",
            operation_label="Failed Archived Op",
            operation_kind="test.archived",
            failure_policy="abort_archive",
        ),
    ):
        raise RuntimeError("boom")
    mg.deactivate()
    discard_failed_result = runner.invoke(main, ["discard", "task-archived-failure"])
    assert discard_failed_result.exit_code == 0, discard_failed_result.output

    for idx in range(60):
        scope_name = f"task-clean-discard-crowding-{idx}"
        branch_result = runner.invoke(main, ["branch", scope_name])
        assert branch_result.exit_code == 0, branch_result.output
        mg, scopes = _restored_vcscore(tmp_path)
        task = scopes[scope_name]
        with mg.runtime_activity(
            scope=task,
            operation_id=f"clean-discard-crowding-op-{idx}",
            operation_label=f"Clean Discard Crowding Op {idx}",
            operation_kind="test.completed.archived",
        ):
            pass
        mg.deactivate()
        discard_result = runner.invoke(main, ["discard", scope_name])
        assert discard_result.exit_code == 0, discard_result.output

    recovery_result = runner.invoke(main, ["recovery", "--max-count", "20"])

    assert recovery_result.exit_code == 0, recovery_result.output
    assert "Archived recovery operations:" in recovery_result.output
    assert "failed-archived-op-id  [archived/error]" in recovery_result.output


def test_recovery_reports_orphaned_open_and_archived_state(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    archived_branch = runner.invoke(main, ["branch", "task-archived"])
    orphan_branch = runner.invoke(main, ["branch", "task-orphan", "--parent", "task-archived"])
    assert archived_branch.exit_code == 0, archived_branch.output
    assert orphan_branch.exit_code == 0, orphan_branch.output

    mg, scopes = _restored_vcscore(tmp_path)
    archived_task = scopes["task-archived"]
    with (
        pytest.raises(RuntimeError, match="boom"),
        mg.runtime_activity(
            scope=archived_task,
            operation_id="archived-op",
            operation_label="Archived Op",
            operation_kind="test.archived",
            failure_policy="abort_archive",
        ),
    ):
        raise RuntimeError("boom")
    mg.deactivate()

    mg, scopes = _restored_vcscore(tmp_path)
    orphan_task = scopes["task-orphan"]
    with mg._lock:
        mg._pipeline.set_scope(orphan_task)
        mg._pipeline.begin_operation(
            handle_id="orphan-op",
            kind="test.orphan",
            scope=orphan_task,
            operation_id="orphan-op",
            operation_label="Orphan Op",
        )
    _dirty_shutdown(mg)

    result = runner.invoke(main, ["recovery"])

    assert result.exit_code == 0, result.output
    assert "Orphaned scopes:" in result.output
    assert "task-orphan" in result.output
    assert "Open operations:" in result.output
    assert "orphan-op  [staged/open]" in result.output
    assert "Archived recovery operations:" in result.output
    assert "archived-op  [archived/error]" in result.output
    assert "Orphaned operations:" in result.output


def test_operations_reports_invalid_repository_state_cleanly(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    runner.invoke(main, ["branch", "task-invalid"])

    mg, scopes = _restored_vcscore(tmp_path)
    task = scopes["task-invalid"]
    first = mg.store.begin_operation(
        task.ref,
        handle_id="shared-operation-id",
        kind="marker.mark",
        world_id=task.world_id or "",
        scope_instance_id=task.instance_id,
        operation_id="shared-operation-id",
        operation_label="shared-operation-id",
    )
    mg.store.append_operation_effect(first, "Marker", {"label": "first"}, substrate="marker")
    mg.store.finalize_operation(first, scope=task)

    second = mg.store.begin_operation(
        task.ref,
        handle_id="shared-operation-id",
        kind="marker.mark",
        world_id=task.world_id or "",
        scope_instance_id=task.instance_id,
        operation_id="shared-operation-id",
        operation_label="shared-operation-id",
    )
    mg.store.append_operation_effect(second, "Marker", {"label": "second"}, substrate="marker")
    mg.store.finalize_operation(second, scope=task)
    mg.deactivate()

    operations_result = runner.invoke(main, ["operations", "--scope", "task-invalid"])
    show_result = runner.invoke(main, ["operation", "show", "shared-operation-id", "--scope", "task-invalid"])

    assert operations_result.exit_code == 1
    assert (
        "Error: Invalid repository state: multiple visible operations share durable operation_id"
        in operations_result.output
    )
    assert show_result.exit_code == 1
    assert (
        "Error: Invalid repository state: multiple visible operations share durable operation_id" in show_result.output
    )
