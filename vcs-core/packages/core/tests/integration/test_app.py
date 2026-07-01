"""App-layer control-plane integration tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner
from vcs_core._app import (
    AppCommandBlocked,
    AppOpenMode,
    AppRepositoryError,
    AppScopeNotFound,
    AppScopeResolutionError,
    VcsCoreApp,
)
from vcs_core._errors import WorkspaceAuthorityRecoveryRequiredError
from vcs_core._sibling_groups import SiblingGroupRecord, SiblingHandleRecord, sibling_machine_scope_name
from vcs_core.cli import main
from vcs_core.store import Store
from vcs_core.types import ScopeInfo
from vcs_core.vcscore import VcsCore

from ..support.cli import init_repo

if TYPE_CHECKING:
    from pathlib import Path


def _publish_deferred_sibling_group_blocker(mg: VcsCore, *, group_id: str = "sg-777777777777") -> None:
    parent_oid = mg.store.log(ref=Store.GROUND_REF, max_count=1)[0].oid
    siblings = tuple(
        SiblingHandleRecord(
            world_id=f"{group_id}-world-{ordinal}",
            machine_scope_name=sibling_machine_scope_name(group_id, ordinal),
            display_label=f"attempt-{ordinal}",
            scope_ref=f"refs/vcscore/scopes/{sibling_machine_scope_name(group_id, ordinal)}",
            parent_ref=Store.GROUND_REF,
            creation_oid=parent_oid,
            state="admitted",
        )
        for ordinal in range(2)
    )
    record = SiblingGroupRecord(
        group_id=group_id,
        parent_ref=Store.GROUND_REF,
        parent_world_id="ground-world",
        admitted_parent_oid=parent_oid,
        status="admitted",
        siblings=siblings,
        leases=(),
        created_at=1.0,
        updated_at=2.0,
    )
    assert mg.store._publish_sibling_group_for_recovery_test(record, expected_head_oid=None)


def test_failed_control_open_preserves_foreign_session_lock(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from vcs_core._lock import acquire_session_lock, release_session_lock

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    init_repo(runner, tmp_path)
    repo_path = str(tmp_path / ".vcscore")

    acquire_session_lock(repo_path, "foreign-session")
    try:
        with (
            pytest.raises(AppRepositoryError, match="Repository locked by session"),
            VcsCoreApp.open_existing(str(tmp_path), mode=AppOpenMode.CONTROL),
        ):
            pass

        assert (tmp_path / ".vcscore" / "session.lock").exists()

        with (
            pytest.raises(AppRepositoryError, match="Repository locked by session"),
            VcsCoreApp.open_existing(str(tmp_path), mode=AppOpenMode.CONTROL),
        ):
            pass
    finally:
        release_session_lock(repo_path, "foreign-session")


def test_control_app_restores_registry_live_scope_as_live_blocker(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    init_repo(runner, tmp_path)
    branch = runner.invoke(main, ["branch", "task"], catch_exceptions=False)
    assert branch.exit_code == 0, branch.output

    with VcsCoreApp.open_existing(str(tmp_path), mode=AppOpenMode.CONTROL) as app:
        blockers = app.push_blockers()

    assert [blocker.kind for blocker in blockers] == ["live_scope"]
    assert blockers[0].subject == "task"


def test_control_app_reports_stale_live_registry_scope_as_mismatch(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    init_repo(runner, tmp_path)
    branch = runner.invoke(main, ["branch", "task"], catch_exceptions=False)
    assert branch.exit_code == 0, branch.output

    mg = VcsCore.from_config(str(tmp_path))
    entry = mg.store.scope_registry_entry("task", status="live")
    assert entry is not None
    mg.store.discard(mg.store.scope_info_from_registry_entry(entry))

    with VcsCoreApp.open_existing(str(tmp_path), mode=AppOpenMode.CONTROL) as app:
        blockers = app.push_blockers()

    assert "scope_registry_mismatch" in {blocker.kind for blocker in blockers}
    assert any(blocker.subject == "task" for blocker in blockers)


def test_app_resolving_unknown_scope_raises_scope_not_found(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    init_repo(runner, tmp_path)

    with (
        VcsCoreApp.open_existing(str(tmp_path), mode=AppOpenMode.CONTROL) as app,
        pytest.raises(AppScopeNotFound),
    ):
        app.resolve_scope("missing")


def test_app_resolving_ground_live_entry_reports_resolution_error(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    init_repo(runner, tmp_path)

    with (
        VcsCoreApp.open_existing(str(tmp_path), mode=AppOpenMode.CONTROL) as app,
        pytest.raises(AppScopeResolutionError) as excinfo,
    ):
        app.scope_index.resolve_entry("ground")

    assert excinfo.value.blockers[0].kind == "scope_registry_mismatch"
    assert excinfo.value.blockers[0].detail == "scope 'ground' is not a live child scope."


def test_app_branch_rejects_invalid_scope_name_before_lifecycle_admission(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    init_repo(runner, tmp_path)
    branch = runner.invoke(main, ["branch", "live-child"], catch_exceptions=False)
    assert branch.exit_code == 0, branch.output

    with (
        VcsCoreApp.open_existing(str(tmp_path), mode=AppOpenMode.CONTROL) as app,
        pytest.raises(AppCommandBlocked) as excinfo,
    ):
        app.branch(name="bad/name", parent="ground")

    assert excinfo.value.command == "branch"
    assert [blocker.kind for blocker in excinfo.value.blockers] == ["invalid_input"]
    assert "contains '/'" in excinfo.value.blockers[0].detail


def test_recovery_app_does_not_treat_consistent_live_scope_as_orphan(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    init_repo(runner, tmp_path)
    branch = runner.invoke(main, ["branch", "task"], catch_exceptions=False)
    assert branch.exit_code == 0, branch.output

    with VcsCoreApp.open_existing(str(tmp_path), mode=AppOpenMode.RECOVERY) as app:
        archived = app.archive_orphaned_scopes()

    assert archived == []
    entry = VcsCore.from_config(str(tmp_path)).store.scope_registry_entry("task", status="live")
    assert entry is not None
    assert VcsCore.from_config(str(tmp_path)).store.ref_exists(entry.ref)


def test_app_archive_commands_report_sibling_group_blockers(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    init_repo(runner, tmp_path)
    _publish_deferred_sibling_group_blocker(VcsCore.from_config(str(tmp_path)))

    with VcsCoreApp.open_existing(str(tmp_path), mode=AppOpenMode.CONTROL) as app:
        blockers = app.push_blockers()

        assert [blocker.kind for blocker in blockers] == ["sibling_group"]
        assert blockers[0].subject == "sg-777777777777 (admitted)"

        with pytest.raises(AppCommandBlocked) as scopes_excinfo:
            app.archive_orphaned_scopes()
        with pytest.raises(AppCommandBlocked) as operations_excinfo:
            app.archive_orphaned_operations()

    assert scopes_excinfo.value.command == "archive-orphaned-scopes"
    assert [blocker.kind for blocker in scopes_excinfo.value.blockers] == ["sibling_group"]
    assert operations_excinfo.value.command == "archive-orphaned-operations"
    assert [blocker.kind for blocker in operations_excinfo.value.blockers] == ["sibling_group"]


def test_app_archive_orphaned_scopes_reports_readiness_admission_blockers(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    init_repo(runner, tmp_path)

    with VcsCoreApp.open_existing(str(tmp_path), mode=AppOpenMode.RECOVERY) as app:

        def fail_archive(*, exclude_refs: set[str]) -> list[str]:
            del exclude_refs
            raise WorkspaceAuthorityRecoveryRequiredError(
                attempted="archive orphaned scopes",
                operations=["wv_ground_unrelated_block"],
            )

        monkeypatch.setattr(app.mg, "archive_orphaned_scopes", fail_archive)
        with pytest.raises(AppCommandBlocked) as excinfo:
            app.archive_orphaned_scopes()

    assert excinfo.value.command == "archive-orphaned-scopes"
    assert [blocker.kind for blocker in excinfo.value.blockers] == ["workspace_authority"]
    assert excinfo.value.blockers[0].subject == "wv_ground_unrelated_block"


def test_active_app_reports_already_active_scope_identity_mismatch(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    init_repo(runner, tmp_path)
    branch = runner.invoke(main, ["branch", "task"], catch_exceptions=False)
    assert branch.exit_code == 0, branch.output

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate()
    try:
        entry = mg.store.scope_registry_entry("task", status="live")
        assert entry is not None
        mg._active_scopes["task"] = ScopeInfo(
            name="task",
            ref=entry.ref,
            instance_id="wrong-instance",
            creation_oid=entry.creation_oid,
            world_id=entry.world_id,
        )
        mg._scope_parents["task"] = mg.ground

        with VcsCoreApp.active_view(mg) as app:
            blockers = app.scope_index.blockers

        assert any(blocker.kind == "scope_registry_mismatch" and blocker.subject == "task" for blocker in blockers)
        assert any("instance_id" in blocker.detail for blocker in blockers)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_active_app_cleans_transiently_restored_scope_handles(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    init_repo(runner, tmp_path)
    with VcsCoreApp.open_existing(str(tmp_path), mode=AppOpenMode.CONTROL) as app:
        app.branch(name="stateless-task", parent="ground")

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate()
    try:
        previous_context = mg._pipeline.context

        with VcsCoreApp.active_view(mg) as app:
            stateless_scope = app.resolve_scope("stateless-task")
            assert mg.lookup_scope("stateless-task") == stateless_scope
            assert "stateless-task" in mg._restored_scopes
            assert mg._pipeline.context == previous_context

        assert mg.lookup_scope("stateless-task") is None
        assert "stateless-task" not in mg._restored_scopes
        assert mg._pipeline.context == previous_context
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_active_app_preserves_daemon_owned_scope_handles(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    init_repo(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate()
    try:
        daemon_scope = mg.fork(mg.ground, "daemon-task")
        previous_context = mg._pipeline.context

        with VcsCoreApp.active_view(mg) as app:
            resolved = app.resolve_scope("daemon-task")
            assert resolved == daemon_scope
            assert "daemon-task" not in app.scope_index.restored_names
            assert "daemon-task" not in mg._restored_scopes
            assert mg._pipeline.context == previous_context

        assert mg.lookup_scope("daemon-task") == daemon_scope
        assert mg._pipeline.context == previous_context
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_repo_status_uses_planning_helper_not_push_dry_run(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    init_repo(runner, tmp_path)

    with VcsCoreApp.open_existing(str(tmp_path), mode=AppOpenMode.CONTROL) as app:

        def fail_push(*args, **kwargs):  # type: ignore[no-untyped-def]
            del args, kwargs
            raise AssertionError("repo_status must not call push")

        monkeypatch.setattr(app.mg, "push", fail_push)

        summary = app.repo_status()

    assert summary.pending_plan is not None


def test_repo_status_reports_physical_workspace_blockers_without_planning(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.txt").write_text("adopted\n")
    runner = CliRunner()
    init_result = runner.invoke(main, ["init", "--adopt", "worktree", "--all", str(tmp_path)])
    assert init_result.exit_code == 0, init_result.output
    (tmp_path / "notes.txt").write_text("dirty\n")

    with VcsCoreApp.open_existing(str(tmp_path), mode=AppOpenMode.CONTROL) as app:

        def fail_assess_push(*args, **kwargs):  # type: ignore[no-untyped-def]
            del args, kwargs
            raise AssertionError("repo_status must not plan when physical workspace admission is blocked")

        monkeypatch.setattr(app.mg, "assess_push", fail_assess_push)

        summary = app.repo_status()

    assert summary.pending_plan is None
    assert [blocker.kind for blocker in summary.blockers] == ["physical_workspace"]
    assert summary.blockers[0].subject == "notes.txt"
