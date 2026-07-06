"""VcsCore recovery and lifecycle safety integration tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from vcs_core import (
    WORLD_TRANSITION_SCHEMA,
    ActivationError,
    DirtyPushError,
    InterruptedLifecycleError,
    InvalidRepositoryStateError,
    LifecycleRecoveryRequiredError,
    MergePreconditionError,
    OpenScopeError,
    OrphanedOperationsError,
    WorkspaceAuthorityRecoveryRequiredError,
    WorldSnapshot,
    build_builtin_substrate_context,
)
from vcs_core._lifecycle_run import read_lifecycle_run
from vcs_core._lock import release_session_lock
from vcs_core._workspace_authority import WorkspaceAuthorityPending, write_pending_workspace_authority
from vcs_core._world_refs import encode_ref_component
from vcs_core.store import Store
from vcs_core.substrates import FilesystemSubstrate, MarkerSubstrate
from vcs_core.testing import WorldStorageManager
from vcs_core.types import EffectRecord, ScopeInfo
from vcs_core.vcscore import VcsCore

from ...support.overlays import MockOverlayBackend


class FailOnceContainSubstrate:
    name = "fail-once"
    commands = {}
    effects = {}

    def __init__(
        self,
        failures: dict[str, int],
        calls: list[str] | None = None,
        *,
        prepare_effects: tuple[EffectRecord, ...] = (),
    ) -> None:
        self._failures = failures
        self.calls = calls if calls is not None else []
        self._prepare_effects = prepare_effects

    def bind_pipeline(self, pipeline, *, scope_queries=None) -> None:
        del pipeline, scope_queries

    def activate(self) -> None:
        pass

    def deactivate(self) -> None:
        pass

    def push(self, scope_id: str | None = None) -> None:
        del scope_id

    def authority(self):
        return None

    def python_patches(self) -> tuple[object, ...]:
        return ()

    def branch(self, scope_id: str, *, parent_scope: ScopeInfo, hints: dict | None = None) -> None:
        del parent_scope, hints
        self.calls.append(f"branch:{scope_id}")

    def prepare_merge(self, scope: ScopeInfo, parent: ScopeInfo):
        del scope, parent
        return self._prepare_effects

    def commit_merge(self, scope_id: str, *, parent_scope: ScopeInfo) -> None:
        del parent_scope
        self.calls.append(f"commit:{scope_id}")
        key = f"commit:{scope_id}"
        remaining = self._failures.get(key, 0)
        if remaining > 0:
            self._failures[key] = remaining - 1
            raise RuntimeError(f"merge failure for {scope_id}")

    def discard(self, scope_id: str) -> None:
        self.calls.append(f"discard:{scope_id}")
        key = f"discard:{scope_id}"
        remaining = self._failures.get(key, 0)
        if remaining > 0:
            self._failures[key] = remaining - 1
            raise RuntimeError(f"discard failure for {scope_id}")


def _write_workspace_authority_pending(mg: VcsCore, scope: ScopeInfo, operation_id: str) -> None:
    source_commit = mg.store.resolve_to_commit(scope.ref)
    write_pending_workspace_authority(
        mg._repo_path,
        WorkspaceAuthorityPending(
            operation_id=operation_id,
            source_operation_id=f"source_{operation_id}",
            driver_command="scan",
            scope_name=scope.name,
            scope_ref=scope.ref,
            scope_instance_id=scope.instance_id,
            scope_world_id=scope.world_id,
            expected_input_world_oid=mg._current_v2_world_oid(mg._world_storage(), scope.ref),
            scalar_source_commit=str(source_commit.id) if source_commit is not None else None,
        ).with_update(phase="scalar_committed"),
    )


def _publish_empty_v2_scope_world(mg: VcsCore, ref: str, *, operation_id: str) -> tuple[object, str]:
    manager = mg._world_storage()
    world_oid = manager.create_unsafe_world(
        snapshot=WorldSnapshot(),
        transition={
            "schema": WORLD_TRANSITION_SCHEMA,
            "operation_id": operation_id,
            "parent_worlds": [],
        },
        operation_final={
            "schema": "vcscore/operation-final/v2",
            "operation_id": operation_id,
            "selected": {},
            "candidate_commits": [],
            "candidate_outcomes": [],
            "head_selections": [],
            "selection_evidence": [],
        },
    )
    assert manager.publish_root_world(ref=ref, world_oid=world_oid)
    return manager, world_oid


def _make_marker_filesystem_vcscore(workspace: Path) -> VcsCore:
    store = Store(str(workspace / ".vcscore"))
    context = build_builtin_substrate_context(store)
    return VcsCore(
        str(workspace),
        substrates=[MarkerSubstrate(context), FilesystemSubstrate(context)],
        store=store,
    )


def _abandon_session_with_open_operation(
    vcscore: VcsCore,
    task: ScopeInfo,
    *,
    handle_id: str = "dangling-op",
) -> None:
    with vcscore._lock, vcscore._scoped(task):
        vcscore._pipeline.begin_operation(handle_id=handle_id, kind="test.operation", scope=task)

    vcscore._pipeline.reset()
    vcscore._active_scopes.clear()
    vcscore._scope_parents.clear()
    vcscore._isolated_scopes.clear()
    vcscore._restored_scopes.clear()
    vcscore._patch_manager.uninstall_all()
    for substrate in reversed(vcscore.lifecycle_substrates):
        substrate.deactivate()
    release_session_lock(vcscore._repo_path, vcscore._session_id)


def _abandon_session_with_open_ground_operation(
    vcscore: VcsCore,
    *,
    handle_id: str = "dangling-ground-op",
) -> None:
    with vcscore._lock:
        vcscore._pipeline.reset()
        vcscore._pipeline.begin_operation(handle_id=handle_id, kind="test.operation", scope=vcscore.ground)

    vcscore._pipeline.reset()
    vcscore._active_scopes.clear()
    vcscore._scope_parents.clear()
    vcscore._isolated_scopes.clear()
    vcscore._restored_scopes.clear()
    vcscore._patch_manager.uninstall_all()
    for substrate in reversed(vcscore.lifecycle_substrates):
        substrate.deactivate()
    release_session_lock(vcscore._repo_path, vcscore._session_id)


def test_recover_dirty_push(workspace: Path) -> None:
    from vcs_core.store import Store
    from vcs_core.testing import write_dirty_flag

    repo_path = str(workspace / ".vcscore")
    Store(repo_path)
    m = VcsCore(str(workspace))
    m.activate()

    task = m.fork(m.ground, "task-crash")
    m.merge(task, m.ground)
    write_dirty_flag(repo_path, "crashed-session")

    m.recover_dirty_push(mode="repair")
    assert m.status().commits_ahead == 0


def test_recover_dirty_push_verify_raises(workspace: Path) -> None:
    from vcs_core.testing import write_dirty_flag

    repo_path = str(workspace / ".vcscore")
    m = VcsCore(str(workspace))
    m.activate()

    task = m.fork(m.ground, "task-crash-v")
    m.merge(task, m.ground)
    write_dirty_flag(repo_path, "crashed-session")

    with pytest.raises(InvalidRepositoryStateError, match="ledger is missing"):
        m.recover_dirty_push(mode="verify")


def test_recover_dirty_push_force(workspace: Path) -> None:
    from vcs_core.store import Store
    from vcs_core.testing import write_dirty_flag

    repo_path = str(workspace / ".vcscore")
    Store(repo_path)
    m = VcsCore(str(workspace))
    m.activate()

    task = m.fork(m.ground, "task-crash-f")
    m.merge(task, m.ground)
    write_dirty_flag(repo_path, "crashed-session")

    m.recover_dirty_push(mode="force")
    assert m.status().commits_ahead == 0


def test_recover_dirty_push_noop_without_flag(workspace: Path) -> None:
    m = VcsCore(str(workspace))
    m.activate()
    m.recover_dirty_push(mode="repair")
    m.recover_dirty_push(mode="verify")
    m.recover_dirty_push(mode="force")


def test_recover_materialization_clears_corrupt_dirty_and_run(workspace: Path) -> None:
    repo_path = workspace / ".vcscore"
    m = VcsCore(str(workspace))
    m.activate()

    task = m.fork(m.ground, "task-corrupt-materialization")
    m.merge(task, m.ground)
    (repo_path / "dirty").write_text("{not json")
    (repo_path / "materialization-run.json").write_text("{not json")

    report = m.recover_materialization(mode="repair")

    assert report.dirty_present is True
    assert report.run_present is True
    assert report.dirty_validity == "corrupt"
    assert report.run_validity == "corrupt"
    assert report.advanced_materialized is True
    assert report.cleared_dirty is True
    assert report.cleared_run is True
    assert m.status().commits_ahead == 0
    assert not (repo_path / "dirty").exists()
    assert not (repo_path / "materialization-run.json").exists()


def test_recover_materialization_clears_run_only_without_ref_mutation(workspace: Path) -> None:
    from vcs_core._materialization_run import MaterializationRun, write_materialization_run

    repo_path = workspace / ".vcscore"
    m = VcsCore(str(workspace))
    m.activate()

    task = m.fork(m.ground, "task-run-only")
    m.merge(task, m.ground)
    assert m.status().commits_ahead > 0
    write_materialization_run(
        str(repo_path),
        MaterializationRun(
            session_id="crashed",
            run_id="run-only",
            timestamp=1.0,
            planned_unit_ids=("unit-1",),
        ),
    )

    report = m.recover_materialization(mode="repair")

    assert report.dirty_present is False
    assert report.run_present is True
    assert report.advanced_materialized is False
    assert report.reset_ground is False
    assert report.cleared_dirty is False
    assert report.cleared_run is True
    assert m.status().commits_ahead > 0
    assert not (repo_path / "materialization-run.json").exists()


def test_recover_materialization_verify_clears_stale_run_only(workspace: Path) -> None:
    from vcs_core._materialization_run import MaterializationRun, write_materialization_run

    repo_path = workspace / ".vcscore"
    m = VcsCore(str(workspace))
    m.activate()
    write_materialization_run(
        str(repo_path),
        MaterializationRun(
            session_id="crashed",
            run_id="verify-run-only",
            timestamp=1.0,
            planned_unit_ids=("unit-1",),
        ),
    )

    report = m.recover_materialization(mode="verify")

    assert report.dirty_present is False
    assert report.run_present is True
    assert report.cleared_run is True
    assert not (repo_path / "materialization-run.json").exists()


def test_recover_materialization_verify_rejects_corrupt_run_and_preserves_state(workspace: Path) -> None:
    from vcs_core.testing import write_dirty_flag

    repo_path = workspace / ".vcscore"
    m = VcsCore(str(workspace))
    m.activate()
    write_dirty_flag(str(repo_path), "crashed")
    (repo_path / "materialization-run.json").write_text("{not json")

    with pytest.raises(InvalidRepositoryStateError, match="ledger is unreadable"):
        m.recover_materialization(mode="verify")

    assert (repo_path / "dirty").exists()
    assert (repo_path / "materialization-run.json").exists()


def test_activate_reports_corrupt_dirty_as_dirty_push_error(workspace: Path) -> None:
    repo_path = workspace / ".vcscore"
    m1 = VcsCore(str(workspace))
    m1.activate()
    m1.deactivate()
    (repo_path / "dirty").write_text("{not json")

    m2 = VcsCore(str(workspace))
    with pytest.raises(DirtyPushError, match="corrupt dirty metadata"):
        m2.activate()


def test_activate_rejects_combined_materialization_and_lifecycle_recovery_before_mutation(workspace: Path) -> None:
    repo_path = workspace / ".vcscore"
    m1 = VcsCore(str(workspace))
    m1.activate()
    m1.deactivate()
    (repo_path / "dirty").write_text("{not json")

    m2 = VcsCore(str(workspace))
    with pytest.raises(InvalidRepositoryStateError, match="Cannot combine materialization recovery"):
        m2.activate(recover="repair", recover_lifecycle="resume")

    assert (repo_path / "dirty").exists()


def test_direct_materialization_recovery_fails_while_another_session_is_active(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    try:
        m2 = VcsCore.from_config(str(workspace))
        with pytest.raises(ActivationError, match="Another session is active"):
            m2.recover_materialization(mode="repair")
    finally:
        m1.deactivate()


def test_resumable_activation_rehydrates_pending(workspace: Path) -> None:
    from vcs_core import build_builtin_substrate_context
    from vcs_core.store import Store
    from vcs_core.substrates import DeclarativeFilesystemSubstrate, MarkerSubstrate

    repo_path = str(workspace / ".vcscore")
    store = Store(repo_path)
    context = build_builtin_substrate_context(store)
    fs = DeclarativeFilesystemSubstrate(context)
    marker = MarkerSubstrate(context)
    m = VcsCore(str(workspace), substrates=[marker, fs], store=store)
    m.activate()

    task = m.fork(m.ground, "task-rehydrate")
    fs.record_changes([("rehydrate.py", b"content")])
    m.merge(task, m.ground)

    status_before = m.status()
    assert status_before.commits_ahead > 0
    assert status_before.local_changes == 1

    m.deactivate()

    store2 = Store(repo_path)
    m2 = VcsCore(str(workspace), store=store2)
    m2.activate()
    status_after = m2.status()
    assert status_after.commits_ahead == status_before.commits_ahead
    assert status_after.local_changes == status_before.local_changes
    m2.deactivate()


def test_activate_recover_repair(workspace: Path) -> None:
    from vcs_core import DirtyPushError
    from vcs_core.testing import write_dirty_flag

    repo_path = str(workspace / ".vcscore")
    m1 = VcsCore(str(workspace))
    m1.activate()
    task = m1.fork(m1.ground, "task-rec-r")
    m1.merge(task, m1.ground)
    m1.deactivate()

    write_dirty_flag(repo_path, "crashed")

    m2 = VcsCore(str(workspace))
    with pytest.raises(DirtyPushError):
        m2.activate()

    m3 = VcsCore(str(workspace))
    m3.activate(recover="repair")
    assert m3.status().commits_ahead == 0
    m3.deactivate()


def test_activate_recover_force(workspace: Path) -> None:
    from vcs_core.testing import write_dirty_flag

    m1 = VcsCore(str(workspace))
    m1.activate()
    task = m1.fork(m1.ground, "task-rec-f")
    m1.merge(task, m1.ground)
    m1.deactivate()

    repo_path = str(workspace / ".vcscore")
    write_dirty_flag(repo_path, "crashed")

    m2 = VcsCore(str(workspace))
    m2.activate(recover="force")
    assert m2.status().commits_ahead == 0
    m2.deactivate()


def test_discard_emits_snapshot_effect(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-snap")
    marker = mg.lifecycle_substrates[0]
    marker.mark("SomeWork")  # type: ignore[attr-defined]

    mg.discard(task)

    import pygit2
    from vcs_core.git_store import read_effect_json

    repo = pygit2.Repository(mg.store._repo_path)
    archive_refs = [r for r in repo.references if r.startswith("refs/vcscore/archive/task-snap-")]
    assert len(archive_refs) == 1

    tip = repo.references[archive_refs[0]].peel(pygit2.Commit)
    found = False
    for commit in repo.walk(tip.id, pygit2.GIT_SORT_TOPOLOGICAL):
        meta = read_effect_json(repo, commit)
        if meta.get("type") == "DiscardSnapshot":
            assert meta.get("substrate") == "vcscore"
            assert meta.get("discarded_scope") == "task-snap"
            found = True
            break
    assert found, "DiscardSnapshot effect not found on archived branch"


def test_discard_failure_keeps_scope_live_and_unarchived(workspace: Path) -> None:
    calls: list[str] = []

    class TrackingContainSubstrate:
        name = "tracking"
        commands = {}
        effects = {}

        def bind_pipeline(self, pipeline, *, scope_queries=None) -> None:
            del pipeline, scope_queries

        def activate(self) -> None:
            pass

        def deactivate(self) -> None:
            pass

        def push(self, scope_id: str | None = None) -> None:
            del scope_id

        def authority(self):
            return None

        def python_patches(self) -> tuple[object, ...]:
            return ()

        def branch(self, scope_id: str, *, parent_scope: ScopeInfo, hints: dict | None = None) -> None:
            del parent_scope, hints
            calls.append(f"branch:{scope_id}")

        def prepare_merge(self, scope: ScopeInfo, parent: ScopeInfo):
            del scope, parent
            return []

        def commit_merge(self, scope_id: str, *, parent_scope: ScopeInfo) -> None:
            del scope_id, parent_scope

        def discard(self, scope_id: str) -> None:
            calls.append(f"discard:{scope_id}")

    class FailingContainSubstrate:
        name = "failing"
        commands = {}
        effects = {}

        def bind_pipeline(self, pipeline, *, scope_queries=None) -> None:
            del pipeline, scope_queries

        def activate(self) -> None:
            pass

        def deactivate(self) -> None:
            pass

        def push(self, scope_id: str | None = None) -> None:
            del scope_id

        def authority(self):
            return None

        def python_patches(self) -> tuple[object, ...]:
            return ()

        def branch(self, scope_id: str, *, parent_scope: ScopeInfo, hints: dict | None = None) -> None:
            del scope_id, parent_scope, hints

        def prepare_merge(self, scope: ScopeInfo, parent: ScopeInfo):
            del scope, parent
            return []

        def commit_merge(self, scope_id: str, *, parent_scope: ScopeInfo) -> None:
            del scope_id, parent_scope

        def discard(self, scope_id: str) -> None:
            raise RuntimeError(f"discard failure for {scope_id}")

    m = VcsCore(str(workspace), substrates=[TrackingContainSubstrate(), FailingContainSubstrate()])  # type: ignore[list-item]
    m.activate()
    try:
        task = m.fork(m.ground, "task-discard-failure")

        with pytest.raises(RuntimeError, match="Scope remains active for recovery"):
            m.discard(task)

        assert task.name in m._active_scopes
        assert m.store.ref_exists(task.ref)
        assert any(call == "discard:task-discard-failure" for call in calls)
        assert not any(
            effect.metadata.get("type") == "DiscardSnapshot"
            and effect.metadata.get("discarded_scope") == "task-discard-failure"
            for effect in m.log(max_count=20)
        )
    finally:
        m.deactivate()


def test_activate_blocks_and_resumes_interrupted_merge(workspace: Path) -> None:
    from vcs_core import build_builtin_substrate_context

    repo_path = str(workspace / ".vcscore")
    backend = MockOverlayBackend()
    failures = {"commit:task-merge-resume": 1}

    store1 = Store(repo_path)
    m1 = VcsCore(
        str(workspace),
        substrates=[
            FailOnceContainSubstrate(failures),
            FilesystemSubstrate(build_builtin_substrate_context(store1), backend=backend),
        ],  # type: ignore[list-item]
        store=store1,
    )
    m1.activate()
    task = m1.fork(m1.ground, "task-merge-resume", hints={"isolated": True})
    backend.write_file(task.name, "resume.txt", b"hello")

    with pytest.raises(RuntimeError, match="merge failure for task-merge-resume"):
        m1.merge(task, m1.ground)

    with pytest.raises(LifecycleRecoveryRequiredError, match="recover_lifecycle"):
        m1.fork(m1.ground, "blocked-during-merge-recovery")

    lifecycle_run = read_lifecycle_run(repo_path)
    assert lifecycle_run is not None
    assert lifecycle_run.operation == "merge"
    assert lifecycle_run.completed_substrates == ("filesystem",)
    m1.deactivate()

    store2 = Store(repo_path)
    m2 = VcsCore(
        str(workspace),
        substrates=[
            FailOnceContainSubstrate(failures),
            FilesystemSubstrate(build_builtin_substrate_context(store2), backend=backend),
        ],  # type: ignore[list-item]
        store=store2,
    )
    with pytest.raises(InterruptedLifecycleError, match="task-merge-resume"):
        m2.activate()

    store3 = Store(repo_path)
    m3 = VcsCore(
        str(workspace),
        substrates=[
            FailOnceContainSubstrate(failures),
            FilesystemSubstrate(build_builtin_substrate_context(store3), backend=backend),
        ],  # type: ignore[list-item]
        store=store3,
    )
    try:
        m3.activate(recover_lifecycle="resume")

        assert read_lifecycle_run(repo_path) is None
        assert not m3.store.ref_exists(task.ref)
        assert any(
            effect.metadata.get("type") == "ScopeMerge" and effect.metadata.get("scope") == "task-merge-resume"
            for effect in m3.log(max_count=20)
        )
        assert backend.committed == [("task-merge-resume", "ground")]
    finally:
        m3.deactivate()


def test_recover_lifecycle_resumes_partial_discard(workspace: Path) -> None:
    from vcs_core import build_builtin_substrate_context

    repo_path = str(workspace / ".vcscore")
    backend = MockOverlayBackend()
    failures = {"discard:task-discard-resume": 1}

    store = Store(repo_path)
    m = VcsCore(
        str(workspace),
        substrates=[
            FailOnceContainSubstrate(failures),
            FilesystemSubstrate(build_builtin_substrate_context(store), backend=backend),
        ],  # type: ignore[list-item]
        store=store,
    )
    m.activate()
    try:
        task = m.fork(m.ground, "task-discard-resume", hints={"isolated": True})
        backend.write_file(task.name, "resume.txt", b"hello")

        with pytest.raises(RuntimeError, match="Scope remains active for recovery"):
            m.discard(task)

        lifecycle_run = read_lifecycle_run(repo_path)
        assert lifecycle_run is not None
        assert lifecycle_run.operation == "discard"
        assert lifecycle_run.completed_substrates == ("filesystem",)

        with pytest.raises(LifecycleRecoveryRequiredError, match="recover_lifecycle"):
            m.push()

        recovered = m.recover_lifecycle()
        assert recovered == "task-discard-resume"
        assert read_lifecycle_run(repo_path) is None
        assert not m.store.ref_exists(task.ref)
        archive_refs = m.store.list_archive_refs()
        assert any("task-discard-resume" in ref for ref in archive_refs)
        assert any(
            effect.metadata.get("type") == "DiscardSnapshot"
            and effect.metadata.get("discarded_scope") == "task-discard-resume"
            for effect in m.log(ref=archive_refs[0], max_count=20)
        )
        assert backend.discarded == ["task-discard-resume"]
    finally:
        m.deactivate()


def test_recover_lifecycle_repairs_registry_after_merge_publish_failure(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = str(workspace / ".vcscore")

    m1 = VcsCore(str(workspace))
    m1.activate()
    try:
        task = m1.fork(m1.ground, "task-merge-registry-resume")
        original_publish = m1.store.publish_scope_registry_projection
        failed_once = False

        def fail_once_publish(*args, **kwargs):
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                return False
            return original_publish(*args, **kwargs)

        monkeypatch.setattr(m1.store, "publish_scope_registry_projection", fail_once_publish)

        with pytest.raises(
            InvalidRepositoryStateError,
            match="Failed to publish the scope-registry projection",
        ):
            m1.merge(task, m1.ground)

        lifecycle_run = read_lifecycle_run(repo_path)
        assert lifecycle_run is not None
        assert lifecycle_run.phase == "merge_registry"
    finally:
        m1.deactivate(warn_on_open_scopes=False)

    m2 = VcsCore(str(workspace))
    try:
        m2.activate(recover_lifecycle="resume")
        snapshot = m2.store.require_scope_registry_projection()
        assert snapshot.entries_by_name["task-merge-registry-resume"].status == "merged"
        assert not m2.store.ref_exists("refs/vcscore/scopes/task-merge-registry-resume")
        assert not any(
            mismatch.kind == "registry_live_ref_missing" and mismatch.scope_name == "task-merge-registry-resume"
            for mismatch in m2.store.scope_registry_projection_mismatches()
        )
    finally:
        m2.deactivate(warn_on_open_scopes=False)


def test_recover_lifecycle_does_not_duplicate_discard_snapshot_effects_after_prepare_phase_crash(
    workspace: Path,
) -> None:
    repo_path = str(workspace / ".vcscore")
    effect = EffectRecord(effect_type="Marker", metadata={"label": "prepared-once"})

    m1 = VcsCore(
        str(workspace),
        substrates=[
            FailOnceContainSubstrate({}, prepare_effects=(effect,)),
        ],  # type: ignore[list-item]
    )
    m1.activate()
    try:
        task = m1.fork(m1.ground, "task-discard-prepare-crash")
        parent = m1.ground

        with m1._lock:
            m1._begin_lifecycle_run(operation="discard", phase="prepare_discard_effects", scope=task, parent=parent)
            m1._snapshot_discard_effects_locked(task, parent)

        lifecycle_run = read_lifecycle_run(repo_path)
        assert lifecycle_run is not None
        assert lifecycle_run.phase == "prepare_discard_effects"
        assert lifecycle_run.prepared_effect_counts == (("fail-once", 1),)
        assert lifecycle_run.prepared_substrates == ("fail-once",)
        assert lifecycle_run.completed_substrates == ()

        prepared_effects = [
            entry for entry in m1.log(ref=task.ref, max_count=10) if entry.metadata.get("label") == "prepared-once"
        ]
        assert len(prepared_effects) == 1
    finally:
        m1.deactivate()

    m2 = VcsCore(
        str(workspace),
        substrates=[
            FailOnceContainSubstrate({}, prepare_effects=(effect,)),
        ],  # type: ignore[list-item]
    )
    try:
        m2.activate(recover_lifecycle="resume")
        assert read_lifecycle_run(repo_path) is None

        archive_ref = next(
            ref
            for ref in m2.store.list_archive_refs()
            if ref.startswith("refs/vcscore/archive/task-discard-prepare-crash-")
        )
        archived_effects = [
            entry for entry in m2.log(ref=archive_ref, max_count=20) if entry.metadata.get("label") == "prepared-once"
        ]
        assert len(archived_effects) == 1
        assert not any(key.startswith("_mg_") for key in archived_effects[0].metadata)
    finally:
        m2.deactivate()


def test_recover_lifecycle_repairs_registry_after_discard_publish_failure(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = str(workspace / ".vcscore")

    m1 = VcsCore(str(workspace))
    m1.activate()
    try:
        task = m1.fork(m1.ground, "task-discard-registry-resume")
        original_publish = m1.store.publish_scope_registry_projection
        failed_once = False

        def fail_once_publish(*args, **kwargs):
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                return False
            return original_publish(*args, **kwargs)

        monkeypatch.setattr(m1.store, "publish_scope_registry_projection", fail_once_publish)

        with pytest.raises(
            InvalidRepositoryStateError,
            match="Failed to publish the scope-registry projection",
        ):
            m1.discard(task)

        lifecycle_run = read_lifecycle_run(repo_path)
        assert lifecycle_run is not None
        assert lifecycle_run.phase == "discard_registry"
    finally:
        m1.deactivate(warn_on_open_scopes=False)

    m2 = VcsCore(str(workspace))
    try:
        m2.activate(recover_lifecycle="resume")
        snapshot = m2.store.require_scope_registry_projection()
        assert snapshot.entries_by_name["task-discard-registry-resume"].status == "discarded"
        assert not m2.store.ref_exists("refs/vcscore/scopes/task-discard-registry-resume")
        assert not any(
            mismatch.kind == "registry_live_ref_missing" and mismatch.scope_name == "task-discard-registry-resume"
            for mismatch in m2.store.scope_registry_projection_mismatches()
        )
    finally:
        m2.deactivate(warn_on_open_scopes=False)


def test_recover_lifecycle_resumes_partial_discard_snapshot_batch_without_duplicates(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = str(workspace / ".vcscore")
    first_effect = EffectRecord(effect_type="Marker", metadata={"label": "prepared-first"})
    second_effect = EffectRecord(effect_type="Marker", metadata={"label": "prepared-second"})

    m1 = VcsCore(
        str(workspace),
        substrates=[
            FailOnceContainSubstrate({}, prepare_effects=(first_effect, second_effect)),
        ],  # type: ignore[list-item]
    )
    m1.activate()
    original_pipeline_record = m1._pipeline.record
    record_calls = 0

    def crashing_pipeline_record(*args, **kwargs):
        nonlocal record_calls
        result = original_pipeline_record(*args, **kwargs)
        record_calls += 1
        if record_calls == 1:
            raise RuntimeError("crash after first prepared effect")
        return result

    monkeypatch.setattr(m1._pipeline, "record", crashing_pipeline_record)
    try:
        task = m1.fork(m1.ground, "task-discard-partial-prepare-crash")
        parent = m1.ground

        with m1._lock:
            m1._begin_lifecycle_run(operation="discard", phase="prepare_discard_effects", scope=task, parent=parent)
            with pytest.raises(RuntimeError, match="crash after first prepared effect"):
                m1._snapshot_discard_effects_locked(task, parent)

        lifecycle_run = read_lifecycle_run(repo_path)
        assert lifecycle_run is not None
        assert lifecycle_run.phase == "prepare_discard_effects"
        assert lifecycle_run.prepared_effect_counts == ()
        assert lifecycle_run.prepared_substrates == ()

        prepared_effects = [
            entry
            for entry in m1.log(ref=task.ref, max_count=10)
            if entry.metadata.get("label") in {"prepared-first", "prepared-second"}
        ]
        assert [entry.metadata.get("label") for entry in prepared_effects] == ["prepared-first"]
    finally:
        m1.deactivate()

    m2 = VcsCore(
        str(workspace),
        substrates=[
            FailOnceContainSubstrate({}, prepare_effects=(first_effect, second_effect)),
        ],  # type: ignore[list-item]
    )
    try:
        m2.activate(recover_lifecycle="resume")
        assert read_lifecycle_run(repo_path) is None

        archive_ref = next(
            ref
            for ref in m2.store.list_archive_refs()
            if ref.startswith("refs/vcscore/archive/task-discard-partial-prepare-crash-")
        )
        archived_effects = [
            entry
            for entry in m2.log(ref=archive_ref, max_count=20)
            if entry.metadata.get("label") in {"prepared-first", "prepared-second"}
        ]
        assert [entry.metadata.get("label") for entry in archived_effects] == ["prepared-second", "prepared-first"]
        assert sum(entry.metadata.get("label") == "prepared-first" for entry in archived_effects) == 1
        assert sum(entry.metadata.get("label") == "prepared-second" for entry in archived_effects) == 1
        assert not any(key.startswith("_mg_") for entry in archived_effects for key in entry.metadata)
    finally:
        m2.deactivate()


def test_fork_rolls_back_when_scope_registry_publish_fails(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = VcsCore(str(workspace))
    m.activate()
    try:
        monkeypatch.setattr(m.store, "publish_scope_registry_projection", lambda *args, **kwargs: False)

        with pytest.raises(
            InvalidRepositoryStateError,
            match="Failed to publish the scope-registry projection",
        ):
            m.fork(m.ground, "task-fork-registry-failure")

        assert not m.store.ref_exists("refs/vcscore/scopes/task-fork-registry-failure")
        assert "task-fork-registry-failure" not in m._active_scopes
        assert "task-fork-registry-failure" not in m.store.require_scope_registry_projection().entries_by_name
    finally:
        m.deactivate(warn_on_open_scopes=False)


def test_activate_detects_orphaned_scope_refs(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    m1.fork(m1.ground, "task-orphan-detect")
    m1.deactivate()

    m2 = VcsCore(str(workspace))
    m2.activate()
    assert len(m2._orphaned_refs) == 1
    assert m2._orphaned_refs[0] == "refs/vcscore/scopes/task-orphan-detect"
    m2.deactivate()


def test_push_rejects_with_orphaned_scope_refs(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    task = m1.fork(m1.ground, "task-orphan-block")
    m1._pipeline.record_one(EffectRecord(effect_type="Test", metadata={}), substrate="agent")
    m1.merge(task, m1.ground)
    m1.fork(m1.ground, "task-abandoned")
    m1.deactivate()

    m2 = VcsCore(str(workspace))
    m2.activate()
    with pytest.raises(OpenScopeError, match="orphaned scope ref"):
        m2.push()
    m2.deactivate()


def test_archive_orphaned_scopes(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    task = m1.fork(m1.ground, "task-archive-cleanup")
    m1._pipeline.record_one(EffectRecord(effect_type="Test", metadata={}), substrate="agent")
    m1.merge(task, m1.ground)
    m1.fork(m1.ground, "task-orphan-cleanup")
    m1.deactivate()

    m2 = VcsCore(str(workspace))
    m2.activate()
    assert len(m2._orphaned_refs) == 1

    archived = m2.archive_orphaned_scopes()
    assert archived == ["task-orphan-cleanup"]
    assert len(m2._orphaned_refs) == 0

    plan = m2.push()
    assert plan.commits_ahead >= 0
    m2.deactivate()


def test_archive_orphaned_scopes_removes_v2_scope_authority(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    task = m1.fork(m1.ground, "task-v2-orphan-cleanup")
    manager, _world_oid = _publish_empty_v2_scope_world(
        m1,
        task.ref,
        operation_id="seed-v2-orphan-cleanup",
    )
    assert task.ref in manager.world_store.repo.references
    m1.deactivate(warn_on_open_scopes=False)

    m2 = VcsCore(str(workspace))
    try:
        m2.activate()
        manager = m2._world_storage()
        assert m2.list_orphaned_scope_refs() == (task.ref,)
        assert task.ref in manager.world_store.repo.references

        archived = m2.archive_orphaned_scopes()

        assert archived == [task.name]
        assert m2.list_orphaned_scope_refs() == ()
        assert task.ref not in manager.world_store.repo.references
    finally:
        m2.deactivate(warn_on_open_scopes=False)


def test_archive_orphaned_scopes_clears_matching_workspace_authority_pending(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    task = m1.fork(m1.ground, "task-orphan-workspace-authority")
    _write_workspace_authority_pending(m1, task, "wv_orphan_child")
    assert m1.list_workspace_authority_pending() == ("wv_orphan_child",)
    m1.deactivate(warn_on_open_scopes=False)

    m2 = VcsCore(str(workspace))
    try:
        m2.activate()
        assert m2.list_orphaned_scope_refs() == (task.ref,)

        archived = m2.archive_orphaned_scopes()

        assert archived == [task.name]
        assert m2.list_workspace_authority_pending() == ()
    finally:
        m2.deactivate(warn_on_open_scopes=False)


def test_archive_orphaned_scopes_blocks_unrelated_workspace_authority_pending(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    task = m1.fork(m1.ground, "task-orphan-unrelated-workspace-authority")
    _write_workspace_authority_pending(m1, task, "wv_orphan_child_unrelated_block")
    _write_workspace_authority_pending(m1, m1.ground, "wv_ground_unrelated_block")
    m1.deactivate(warn_on_open_scopes=False)

    m2 = VcsCore(str(workspace))
    try:
        m2.activate()
        assert m2.list_orphaned_scope_refs() == (task.ref,)

        with pytest.raises(WorkspaceAuthorityRecoveryRequiredError, match="wv_ground_unrelated_block"):
            m2.archive_orphaned_scopes()

        assert m2.list_orphaned_scope_refs() == (task.ref,)
        assert set(m2.list_workspace_authority_pending()) == {
            "wv_orphan_child_unrelated_block",
            "wv_ground_unrelated_block",
        }
    finally:
        m2.deactivate(warn_on_open_scopes=False)


def test_archive_orphaned_scopes_preserves_workspace_authority_when_archive_fails(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    task = m1.fork(m1.ground, "task-orphan-workspace-authority-fail")
    manager, child_world_oid = _publish_empty_v2_scope_world(
        m1,
        task.ref,
        operation_id="seed-v2-orphan-workspace-authority-fail",
    )
    assert task.ref in manager.world_store.repo.references
    _write_workspace_authority_pending(m1, task, "wv_orphan_archive_fail")
    m1.deactivate(warn_on_open_scopes=False)

    m2 = VcsCore(str(workspace))
    try:
        m2.activate()
        original_discard = m2._store.discard

        def fail_discard(scope: ScopeInfo) -> None:
            if scope.ref == task.ref:
                raise RuntimeError("synthetic archive failure")
            original_discard(scope)

        monkeypatch.setattr(m2._store, "discard", fail_discard)

        archived = m2.archive_orphaned_scopes()

        assert archived == []
        assert m2.list_orphaned_scope_refs() == (task.ref,)
        assert m2.list_workspace_authority_pending() == ("wv_orphan_archive_fail",)
        recovered_manager = m2._world_storage()
        assert task.ref in recovered_manager.world_store.repo.references
        assert str(recovered_manager.world_store.repo.references[task.ref].target) == child_world_oid
    finally:
        m2.deactivate(warn_on_open_scopes=False)


def test_archive_orphaned_scopes_retries_after_v2_cleanup_failure(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    task = m1.fork(m1.ground, "task-orphan-v2-cleanup-retry")
    manager, child_world_oid = _publish_empty_v2_scope_world(
        m1,
        task.ref,
        operation_id="seed-v2-cleanup-retry",
    )
    assert task.ref in manager.world_store.repo.references
    _write_workspace_authority_pending(m1, task, "wv_orphan_v2_cleanup_retry")
    m1.deactivate(warn_on_open_scopes=False)

    m2 = VcsCore(str(workspace))
    try:
        m2.activate()
        original_discard_v2 = m2._discard_v2_scope_world
        failed_once = False

        def fail_once_discard_v2(scope: ScopeInfo) -> None:
            nonlocal failed_once
            if scope.ref == task.ref and not failed_once:
                failed_once = True
                raise RuntimeError("synthetic v2 cleanup failure")
            original_discard_v2(scope)

        monkeypatch.setattr(m2, "_discard_v2_scope_world", fail_once_discard_v2)

        assert m2.archive_orphaned_scopes() == []
        assert failed_once
        assert m2.list_orphaned_scope_refs() == (task.ref,)
        assert task.ref not in m2.store._repo.references
        recovered_manager = m2._world_storage()
        assert task.ref in recovered_manager.world_store.repo.references
        assert str(recovered_manager.world_store.repo.references[task.ref].target) == child_world_oid
        assert m2.list_workspace_authority_pending() == ("wv_orphan_v2_cleanup_retry",)

        monkeypatch.setattr(m2, "_discard_v2_scope_world", original_discard_v2)
        assert m2.archive_orphaned_scopes() == [task.name]
        assert m2.list_orphaned_scope_refs() == ()
        assert task.ref not in recovered_manager.world_store.repo.references
        assert m2.list_workspace_authority_pending() == ()
    finally:
        m2.deactivate(warn_on_open_scopes=False)


def test_activate_detects_v2_only_orphan_after_partial_archive(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    task = m1.fork(m1.ground, "task-v2-only-orphan")
    manager, child_world_oid = _publish_empty_v2_scope_world(
        m1,
        task.ref,
        operation_id="seed-v2-only-orphan",
    )
    assert task.ref in manager.world_store.repo.references
    m1.deactivate(warn_on_open_scopes=False)

    m2 = VcsCore(str(workspace))
    try:
        m2.activate()

        def fail_discard_v2(scope: ScopeInfo) -> None:
            if scope.ref == task.ref:
                raise RuntimeError("synthetic v2 cleanup failure")
            m2._discard_v2_scope_world(scope)

        monkeypatch.setattr(m2, "_discard_v2_scope_world", fail_discard_v2)
        assert m2.archive_orphaned_scopes() == []
        assert task.ref not in m2.store._repo.references
        assert task.ref in manager.world_store.repo.references
        assert str(manager.world_store.repo.references[task.ref].target) == child_world_oid
    finally:
        m2.deactivate(warn_on_open_scopes=False)

    m3 = VcsCore(str(workspace))
    try:
        m3.activate()
        assert task.ref in m3.list_orphaned_scope_refs()
    finally:
        m3.deactivate(warn_on_open_scopes=False)


def test_v2_scope_merge_publish_failure_preserves_child_authority_for_recovery(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = str(workspace / ".vcscore")
    m1 = VcsCore(str(workspace))
    m1.activate()
    task = m1.fork(m1.ground, "task-v2-merge-cas-recovery")
    manager, child_world_oid = _publish_empty_v2_scope_world(
        m1,
        task.ref,
        operation_id="seed-v2-merge-cas-recovery",
    )
    original_advance_publication = manager.advance_publication
    failed_once = False

    def fail_once_advance(prepared):
        nonlocal failed_once
        if prepared.plan.authority_ref == m1.ground.ref and not failed_once:
            failed_once = True
            return False
        return original_advance_publication(prepared)

    monkeypatch.setattr(manager, "advance_publication", fail_once_advance)

    try:
        with pytest.raises(InvalidRepositoryStateError, match="world authority ref changed before publication"):
            m1.merge(task, m1.ground)

        lifecycle_run = read_lifecycle_run(repo_path)
        assert lifecycle_run is not None
        assert lifecycle_run.phase == "merge_store"
        assert task.ref in manager.world_store.repo.references
        assert str(manager.world_store.repo.references[task.ref].target) == child_world_oid
        assert m1.ground.ref not in manager.world_store.repo.references
    finally:
        m1.deactivate(warn_on_open_scopes=False)

    m2 = VcsCore(str(workspace))
    try:
        m2.activate(recover_lifecycle="resume")
        recovered_manager = m2._world_storage()
        assert task.ref not in recovered_manager.world_store.repo.references
        assert m2.ground.ref in recovered_manager.world_store.repo.references
        assert read_lifecycle_run(repo_path) is None
    finally:
        m2.deactivate(warn_on_open_scopes=False)


def test_v2_scope_merge_recovery_advances_past_failed_retry_attempt(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = str(workspace / ".vcscore")
    m1 = VcsCore(str(workspace))
    m1.activate()
    task = m1.fork(m1.ground, "task-v2-merge-retry-chain")
    manager, child_world_oid = _publish_empty_v2_scope_world(
        m1,
        task.ref,
        operation_id="seed-v2-merge-retry-chain",
    )
    operation_id = f"world_merge_{task.instance_id}_{encode_ref_component(m1.ground.ref)}"
    original_advance_publication = manager.advance_publication
    failed_once = False

    def fail_original_advance(prepared):
        nonlocal failed_once
        if prepared.plan.authority_ref == m1.ground.ref and not failed_once:
            failed_once = True
            return False
        return original_advance_publication(prepared)

    monkeypatch.setattr(manager, "advance_publication", fail_original_advance)

    try:
        with pytest.raises(InvalidRepositoryStateError, match="world authority ref changed before publication"):
            m1.merge(task, m1.ground)
        assert failed_once
        assert task.ref in manager.world_store.repo.references
        assert str(manager.world_store.repo.references[task.ref].target) == child_world_oid
    finally:
        m1.deactivate(warn_on_open_scopes=False)

    original_class_advance = WorldStorageManager.advance_publication
    retry_failed_once = False

    def fail_retry_once(self: WorldStorageManager, prepared):
        nonlocal retry_failed_once
        if prepared.plan.authority_ref == "refs/vcscore/ground" and not retry_failed_once:
            retry_failed_once = True
            return False
        return original_class_advance(self, prepared)

    m2 = VcsCore(str(workspace))
    with monkeypatch.context() as mpatch:
        mpatch.setattr(WorldStorageManager, "advance_publication", fail_retry_once)
        with pytest.raises(InvalidRepositoryStateError, match="world authority ref changed before publication"):
            m2.activate(recover_lifecycle="resume")
    assert retry_failed_once

    m3 = VcsCore(str(workspace))
    try:
        m3.activate(recover_lifecycle="resume")
        recovered_manager = m3._world_storage()
        assert task.ref not in recovered_manager.world_store.repo.references
        assert m3.ground.ref in recovered_manager.world_store.repo.references
        assert read_lifecycle_run(repo_path) is None
        assert (
            recovered_manager.read_operation_journal(operation_id, family="archived").tip.payload["status"]
            == "archived"
        )
        retry_1 = f"{operation_id}_retry_1"
        retry_2 = f"{operation_id}_retry_2"
        assert recovered_manager.read_operation_journal(retry_1, family="archived").tip.payload["status"] == "archived"
        assert recovered_manager.read_operation_journal(retry_2, family="closed").tip.payload["status"] == "closed"
    finally:
        m3.deactivate(warn_on_open_scopes=False)


def test_v2_scope_merge_post_publication_failure_closes_original_operation(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    task = m1.fork(m1.ground, "task-v2-merge-post-publish-recovery")
    manager, _child_world_oid = _publish_empty_v2_scope_world(
        m1,
        task.ref,
        operation_id="seed-v2-merge-post-publish-recovery",
    )
    operation_id = f"world_merge_{task.instance_id}_{encode_ref_component(m1.ground.ref)}"
    original_record_published = manager.record_operation_published
    failed_once = False

    def fail_once_record_published(current_operation_id: str, *, world_oid: str) -> object:
        nonlocal failed_once
        if current_operation_id == operation_id and not failed_once:
            failed_once = True
            raise RuntimeError("synthetic merge post-publication failure")
        return original_record_published(current_operation_id, world_oid=world_oid)

    monkeypatch.setattr(manager, "record_operation_published", fail_once_record_published)

    try:
        assert m1.merge(task, m1.ground) == task.name
        assert failed_once
        assert task.ref not in manager.world_store.repo.references
        assert manager.read_operation_journal(operation_id, family="closed").tip.payload["status"] == "closed"
        with pytest.raises(InvalidRepositoryStateError, match="operation journal ref is missing"):
            manager.read_operation_journal(operation_id)
    finally:
        m1.deactivate(warn_on_open_scopes=False)


def test_activate_detects_orphaned_operation_refs(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    task = m1.fork(m1.ground, "task-orphan-op-detect")
    _abandon_session_with_open_operation(m1, task, handle_id="op-detect")

    m2 = VcsCore(str(workspace))
    m2.activate()
    try:
        orphaned = m2.list_orphaned_operations()
        assert len(orphaned) == 1
        assert orphaned[0].operation_id == "op-detect"
        assert orphaned[0].world_id == task.world_id
        assert orphaned[0].world_ref == task.ref
        assert orphaned[0].carrier_ref == "refs/vcscore/ops/op-detect"
        assert not hasattr(orphaned[0], "handle_id")
    finally:
        m2.deactivate()


def test_recovery_snapshot_preserves_orphaned_world_id(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    task = m1.fork(m1.ground, "task-orphan-world-id")
    _abandon_session_with_open_operation(m1, task, handle_id="op-world")

    m2 = VcsCore(str(workspace))
    m2.activate()
    try:
        snapshot = m2.recovery_snapshot()
        assert len(snapshot.orphaned_operations) == 1
        assert snapshot.orphaned_operations[0].operation_id is not None
        assert snapshot.orphaned_operations[0].world_id == task.world_id
    finally:
        m2.deactivate()


def test_exec_rejects_with_orphaned_operation_refs(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    task = m1.fork(m1.ground, "task-orphan-op-block")
    _abandon_session_with_open_operation(m1, task, handle_id="op-block")

    m2 = VcsCore(str(workspace))
    m2.activate()
    try:
        restored = m2.restore_scope(
            name=task.name,
            ref=task.ref,
            instance_id=task.instance_id,
            creation_oid=task.creation_oid,
            world_id=task.world_id,
            parent=m2.ground,
        )
        with pytest.raises(OrphanedOperationsError, match="archive_orphaned_operations"):
            m2.exec("filesystem", "write", scope=restored, path="blocked.txt", content=b"x")
    finally:
        m2.deactivate()


def test_archive_orphaned_operations(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    _abandon_session_with_open_ground_operation(m1, handle_id="op-cleanup")

    m2 = VcsCore(str(workspace))
    m2.activate()
    try:
        archived = m2.archive_orphaned_operations()
        assert archived == ["op-cleanup"]
        assert m2.list_orphaned_operations() == ()
        assert m2.store.list_open_operations() == []
    finally:
        m2.deactivate()


def test_archive_orphaned_operations_blocks_unrelated_orphaned_scope(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    m1.fork(m1.ground, "task-unrelated-orphan-scope")
    _abandon_session_with_open_ground_operation(m1, handle_id="op-ground-cleanup")

    m2 = VcsCore(str(workspace))
    m2.activate()
    try:
        with pytest.raises(OpenScopeError, match="archive orphaned operations blocked"):
            m2.archive_orphaned_operations()
    finally:
        m2.deactivate()


def test_archive_orphaned_scopes_blocks_unrelated_orphaned_operation(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    m1.fork(m1.ground, "task-target-orphan-scope")
    _abandon_session_with_open_ground_operation(m1, handle_id="op-unrelated-ground")

    m2 = VcsCore(str(workspace))
    m2.activate()
    try:
        with pytest.raises(OrphanedOperationsError, match="op-unrelated-ground"):
            m2.archive_orphaned_scopes()
    finally:
        m2.deactivate()


def test_archive_orphaned_scopes_archives_child_operation_refs(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    task = m1.fork(m1.ground, "task-orphan-scope-with-op")
    _abandon_session_with_open_operation(m1, task, handle_id="op-child")

    m2 = VcsCore(str(workspace))
    m2.activate()
    try:
        archived = m2.archive_orphaned_scopes()
        assert archived == ["task-orphan-scope-with-op"]
        assert m2.list_orphaned_scope_refs() == ()
        assert m2.list_orphaned_operations() == ()
        assert m2.store.list_open_operations() == []
    finally:
        m2.deactivate()


def _crash_after_full_path_nested_child_open(workspace: Path) -> None:
    script = f"""
import os

from vcs_core._substrate_runtime import build_builtin_substrate_context
from vcs_core.vcscore import VcsCore
from vcs_core.store import Store
from vcs_core.substrates import FilesystemSubstrate, MarkerSubstrate

workspace = {str(workspace)!r}
store = Store(workspace + "/.vcscore")
context = build_builtin_substrate_context(store)
mg = VcsCore(
    workspace,
    substrates=[MarkerSubstrate(context), FilesystemSubstrate(context)],
    store=store,
)
mg.activate()
parent = mg.fork(mg.ground, "task-nested-recovery-parent")
child = mg.fork(parent, "task-nested-recovery-child")
original_begin_operation = mg._pipeline.begin_operation


def crash_after_begin(*args, **kwargs):
    operation = original_begin_operation(*args, **kwargs)
    if kwargs.get("handle_id") == "op-nested-recovery-child":
        os._exit(0)
    return operation


with mg.runtime_activity(
    scope=parent,
    operation_label="parent-runtime",
    operation_kind="test.parent",
    operation_id="op-nested-recovery-parent",
):
    mg._pipeline.begin_operation = crash_after_begin
    mg._execute_recorded_in_child_operation(
        "filesystem",
        "write",
        scope=child,
        operation_id="op-nested-recovery-child",
        operation_kind="filesystem.write",
        path="crash.txt",
        content=b"crash\\n",
    )
raise AssertionError("child operation unexpectedly finalized")
"""
    env = dict(os.environ)
    env["VCS_CORE_NESTED_OPERATIONS"] = "1"
    subprocess.run([sys.executable, "-c", script], check=True, env=env)


def test_full_path_nested_recovery_archives_then_rerun_reaches_a3_merge_boundary(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    _crash_after_full_path_nested_child_open(workspace)

    m2 = _make_marker_filesystem_vcscore(workspace)
    m2.activate()
    try:
        orphaned_ids = sorted(operation.operation_id for operation in m2.list_orphaned_operations())
        assert orphaned_ids == ["op-nested-recovery-child", "op-nested-recovery-parent"]
        assert sorted(m2.list_orphaned_scope_refs()) == [
            "refs/vcscore/scopes/task-nested-recovery-child",
            "refs/vcscore/scopes/task-nested-recovery-parent",
        ]

        archived_operations = sorted(m2.archive_orphaned_operations())
        assert archived_operations == ["op-nested-recovery-child", "op-nested-recovery-parent"]
        archived_scopes = sorted(m2.archive_orphaned_scopes())
        assert archived_scopes == ["task-nested-recovery-child", "task-nested-recovery-parent"]

        rerun_parent = m2.fork(m2.ground, "task-nested-recovery-parent-rerun")
        rerun_child = m2.fork(rerun_parent, "task-nested-recovery-child-rerun")
        with m2.runtime_activity(
            scope=rerun_parent,
            operation_label="parent-rerun",
            operation_kind="test.parent",
            operation_id="op-nested-recovery-parent-rerun",
        ):
            m2._execute_recorded_in_child_operation(
                "filesystem",
                "write",
                scope=rerun_child,
                operation_id="op-nested-recovery-child-rerun",
                operation_kind="filesystem.write",
                path="rerun.txt",
                content=b"rerun\n",
            )

        with pytest.raises(MergePreconditionError, match="advanced past fork point"):
            m2.merge(rerun_child, rerun_parent)
        assert m2.discard(rerun_child) == rerun_child.name
        assert m2.merge(rerun_parent, m2.ground) == rerun_parent.name
    finally:
        m2.deactivate()


def test_in_session_stale_nested_child_discards_then_reforks_and_merges(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-session A1 C2d face (no crash/recovery): running a child op under a parent
    op advances the parent past the child's fork point, so the stale child cannot
    merge. The stale child is discarded in-session, a fresh child is forked, re-run,
    and merges green — the discard-stale-child cycle without subprocess archival."""
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_marker_filesystem_vcscore(workspace)
    mg.activate()
    try:
        parent = mg.fork(mg.ground, "insession-parent")
        child = mg.fork(parent, "insession-child")

        with mg.runtime_activity(
            scope=parent,
            operation_label="parent",
            operation_kind="test.parent",
            operation_id="op-insession-parent",
        ):
            mg._execute_recorded_in_child_operation(
                "filesystem",
                "write",
                scope=child,
                operation_id="op-insession-child",
                operation_kind="filesystem.write",
                path="child.txt",
                content=b"child\n",
            )

        # The parent advanced at parent-op close; the stale child cannot merge.
        with pytest.raises(MergePreconditionError, match="advanced past fork point"):
            mg.merge(child, parent)

        # Discard the stale child in-session, fork fresh, re-run, merge green.
        assert mg.discard(child) == child.name
        fresh_child = mg.fork(parent, "insession-child-fresh")
        mg.exec("marker", "mark", scope=fresh_child, label="rerun")
        assert mg.merge(fresh_child, parent) == fresh_child.name
        assert mg.merge(parent, mg.ground) == parent.name
    finally:
        mg.deactivate()


def test_deactivate_calls_substrate_deactivate(workspace: Path) -> None:
    from vcs_core import build_builtin_substrate_context
    from vcs_core.store import Store
    from vcs_core.substrates import MarkerSubstrate

    deactivated: list[str] = []

    class TrackingMarker(MarkerSubstrate):
        def deactivate(self) -> None:
            deactivated.append(self.name)

    store = Store(str(workspace / ".vcscore"))
    marker = TrackingMarker(build_builtin_substrate_context(store))
    m = VcsCore(str(workspace), substrates=[marker])
    m.activate()
    m.deactivate()

    assert deactivated == ["marker"]


def test_deactivate_reverse_order(workspace: Path) -> None:
    deactivated: list[str] = []

    class FakeSubA:
        name = "sub-a"
        commands = {}
        effects = {}

        def bind_pipeline(self, pipeline, *, scope_queries=None):
            del pipeline, scope_queries

        def activate(self):
            pass

        def deactivate(self):
            deactivated.append("sub-a")

        def push(self, scope_id=None):
            pass

        def authority(self):
            return None

        def python_patches(self):
            return ()

    class FakeSubB:
        name = "sub-b"
        commands = {}
        effects = {}

        def bind_pipeline(self, pipeline, *, scope_queries=None):
            del pipeline, scope_queries

        def activate(self):
            pass

        def deactivate(self):
            deactivated.append("sub-b")

        def push(self, scope_id=None):
            pass

        def authority(self):
            return None

        def python_patches(self):
            return ()

    m = VcsCore(str(workspace), substrates=[FakeSubA(), FakeSubB()])  # type: ignore[list-item]
    m.activate()
    m.deactivate()

    assert deactivated == ["sub-b", "sub-a"]


def test_deactivate_logs_open_scopes(workspace: Path, caplog) -> None:
    import logging

    m = VcsCore(str(workspace))
    m.activate()
    m.fork(m.ground, "task-open-warn")

    with caplog.at_level(logging.WARNING):
        m.deactivate()

    assert any("open scope" in r.message.lower() for r in caplog.records)


def test_recover_verify_raises_not_implemented(workspace: Path) -> None:
    from vcs_core.testing import write_dirty_flag

    m = VcsCore(str(workspace))
    m.activate()
    task = m.fork(m.ground, "task-verify-ni")
    m.merge(task, m.ground)

    repo_path = str(workspace / ".vcscore")
    write_dirty_flag(repo_path, "crashed")

    with pytest.raises(InvalidRepositoryStateError, match="ledger is missing"):
        m.recover_dirty_push(mode="verify")


def test_merge_preflight_blocks_substrate_side_effects(workspace: Path) -> None:
    calls: list[str] = []

    class TrackingContainSubstrate:
        name = "tracking"
        commands = {}
        effects = {}

        def bind_pipeline(self, pipeline, *, scope_queries=None) -> None:
            del pipeline, scope_queries

        def activate(self) -> None:
            pass

        def deactivate(self) -> None:
            pass

        def push(self, scope_id: str | None = None) -> None:
            del scope_id

        def authority(self):
            return None

        def python_patches(self) -> tuple[object, ...]:
            return ()

        def branch(self, scope_id: str, *, parent_scope: ScopeInfo, hints: dict | None = None) -> None:
            del parent_scope, hints
            calls.append(f"branch:{scope_id}")

        def prepare_merge(self, scope: ScopeInfo, parent: ScopeInfo):
            calls.append(f"prepare:{scope.name}->{parent.name}")
            return []

        def commit_merge(self, scope_id: str, *, parent_scope: ScopeInfo) -> None:
            del parent_scope
            calls.append(f"commit:{scope_id}")

        def discard(self, scope_id: str) -> None:
            calls.append(f"discard:{scope_id}")

    m = VcsCore(str(workspace), substrates=[TrackingContainSubstrate()])  # type: ignore[list-item]
    m.activate()

    second = m.fork(m.ground, "task-preflight-second")
    with m.runtime_activity(
        scope=m.ground,
        operation_id="advance-ground-op",
        operation_label="Advance Ground",
        operation_kind="test.advance-ground",
    ):
        pass

    with pytest.raises(MergePreconditionError, match="sequential live-child policy"):
        m.merge(second, m.ground)

    assert not any(call.startswith("prepare:task-preflight-second") for call in calls)
    assert not any(call.startswith("commit:task-preflight-second") for call in calls)


# --- Liveness-gated auto-recovery of orphaned operations at activation (Layer 2) -------------
#
# "If a run dies, just run it again — unless something is genuinely still running."
# The safety gate is structural: activate() calls acquire_session_lock() first, which reclaims a
# dead owner's lock but refuses while a live one holds it — so by the time auto-recovery runs, no
# live session owns the repo and every orphaned operation is a dead run's bookkeeping.


def test_activate_auto_recovers_dead_orphaned_operation(workspace: Path) -> None:
    """The fix: an interrupted run's orphaned operation ref is reclaimed at activation, so the
    next run just works — no manual archive_orphaned_operations() and no OrphanedOperationsError."""
    m1 = VcsCore(str(workspace))
    m1.activate()
    _abandon_session_with_open_ground_operation(m1, handle_id="op-auto-recover")

    m2 = VcsCore(str(workspace))
    m2.activate(auto_recover_orphaned_operations=True)
    try:
        assert m2.list_orphaned_operations() == ()
        assert m2.store.list_open_operations() == []
        # the "just run it again" run proceeds instead of raising OrphanedOperationsError
        with m2.runtime_activity(
            scope=m2.ground, operation_label="next-run", operation_kind="runtime-run"
        ):
            pass
    finally:
        m2.deactivate()


def test_activate_auto_recovery_is_fail_soft_when_recovery_is_blocked(workspace: Path) -> None:
    """Fail-soft: when recovery is blocked by other pending state (here, an entangled orphaned
    scope), auto-recovery must NOT turn activation into a failure — it leaves the orphan in place,
    so the caller still gets today's detect-and-refuse behavior, exactly as the manual path does."""
    m1 = VcsCore(str(workspace))
    m1.activate()
    m1.fork(m1.ground, "task-entangled-orphan-scope")
    _abandon_session_with_open_ground_operation(m1, handle_id="op-blocked")

    m2 = VcsCore(str(workspace))
    m2.activate(auto_recover_orphaned_operations=True)  # must not raise
    try:
        # recovery was declined (blocked by the orphaned scope); the orphan persists
        assert m2.list_orphaned_operations() != ()
    finally:
        m2.deactivate()


def test_activate_default_preserves_the_wedge(workspace: Path) -> None:
    """Default (auto_recover_orphaned_operations=False) is unchanged: the orphan persists and the
    existing detect-and-refuse contract holds, so no current caller/test behavior shifts."""
    m1 = VcsCore(str(workspace))
    m1.activate()
    _abandon_session_with_open_ground_operation(m1, handle_id="op-default")

    m2 = VcsCore(str(workspace))
    m2.activate()  # default: no auto-recovery
    try:
        assert m2.list_orphaned_operations() != ()
    finally:
        m2.deactivate()


def test_activate_refuses_when_a_live_session_holds_the_lock(workspace: Path) -> None:
    """Safety: a genuinely live session (a live PID in the lock) makes activation refuse, so
    auto-recovery can never run against — let alone reclaim — a live session's operations."""
    import time

    m1 = VcsCore(str(workspace))
    m1.activate()
    m1.deactivate()  # initialise the repo, release the lock

    lock = Path(workspace) / ".vcscore" / "session.lock"
    lock.write_text(f"other-session\n{os.getpid()}\n{time.time()}\n")  # this process = a live owner
    try:
        m2 = VcsCore(str(workspace))
        with pytest.raises(ActivationError, match="Another session is active"):
            m2.activate(auto_recover_orphaned_operations=True)
    finally:
        lock.unlink()
