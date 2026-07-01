"""Tests for internal materialization planning and execution."""

from __future__ import annotations

import subprocess
import threading
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from vcs_core._dirty_flag import read_dirty_flag, write_dirty_flag
from vcs_core._errors import OpenScopeError, WorkspaceAuthorityRecoveryRequiredError
from vcs_core._materialization_coordinator import (
    FileMaterializationState,
    MaterializationAdmission,
    MaterializationCoordinator,
    MaterializationDependencies,
    MaterializationStore,
)
from vcs_core._query_inventory import InventorySnapshot
from vcs_core._workspace_authority import WorkspaceAuthorityPending, write_pending_workspace_authority
from vcs_core.materialization import build_materializers, plan_materialization
from vcs_core.store import GROUND_REF
from vcs_core.types import DiffSummary, ScopeInfo, Status
from vcs_core.vcscore import VcsCore

from ..support.builders import make_marker_filesystem_substrates, make_marker_filesystem_vcscore, make_store
from ..support.overlays import MockOverlayBackend

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vcs_core.materialization import InternalMaterializer
    from vcs_core.types import CommitInfo


class _FakeMaterializationStore:
    def __init__(self) -> None:
        self.advance_materialized_calls = 0
        self.reset_ground_to_materialized_calls = 0
        self.walk_pending_calls = 0

    def walk_pending(self) -> tuple[CommitInfo, ...]:
        self.walk_pending_calls += 1
        return ()

    def diff(self) -> DiffSummary:
        return DiffSummary(files=[])

    def list_workspace_files(self, ref: str) -> tuple[tuple[str, str, int], ...]:
        del ref
        return ()

    def read_workspace_file(self, ref: str, path: str) -> bytes | None:
        del ref, path
        return None

    def status(self) -> Status:
        return Status(local_changes=0, commits_ahead=0)

    def advance_materialized(self) -> None:
        self.advance_materialized_calls += 1

    def reset_ground_to_materialized(self) -> int:
        self.reset_ground_to_materialized_calls += 1
        return 0


class _EmptyMaterializerSource:
    def build(self) -> tuple[InternalMaterializer, ...]:
        return ()


class _NoopGroundScopeAccess:
    def get(self) -> ScopeInfo | None:
        return None

    def set(self, scope: ScopeInfo | None) -> None:
        del scope

    def make(self) -> ScopeInfo:
        return ScopeInfo(name="ground", ref="refs/vcs-core/ground", instance_id="ground-test", creation_oid="")


def _coordinator_deps(
    tmp_path: Path,
    store: MaterializationStore,
    *,
    active_scope_names: tuple[str, ...] = (),
) -> MaterializationDependencies:
    return MaterializationDependencies(
        store=store,
        admission=MaterializationAdmission(
            active_scope_names=lambda: active_scope_names,
            ensure_no_interrupted_lifecycle=lambda attempted: None,
            ensure_no_open_operation=lambda attempted: None,
            readiness_admission=lambda command, attempted, authorized, scope: None,
        ),
        state=FileMaterializationState(str(tmp_path)),
        materializer_source=_EmptyMaterializerSource(),
        session_id="session-test",
        workspace=tmp_path,
        patch_guard=nullcontext,
        ground=_NoopGroundScopeAccess(),
    )


def _materialization_admission(
    *,
    active_scope_names: tuple[str, ...] = (),
    readiness_calls: list[tuple[str, str]] | None = None,
) -> MaterializationAdmission:
    def readiness_admission(
        command: str,
        attempted: str,
        _authorized_operations: object,
        _scope_selector: object,
    ) -> None:
        if readiness_calls is not None:
            readiness_calls.append((command, attempted))

    return MaterializationAdmission(
        active_scope_names=lambda: active_scope_names,
        ensure_no_interrupted_lifecycle=lambda attempted: None,
        ensure_no_open_operation=lambda attempted: None,
        readiness_admission=readiness_admission,
    )


class _RecordingRLock:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.events: list[str] = []

    def __enter__(self) -> None:
        self._lock.acquire()
        self.events.append("enter")

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        self.events.append("exit")
        self._lock.release()


class _FakePatchManager:
    def guard(self) -> Iterator[None]:
        return nullcontext()


class _RecoverOwner:
    def __init__(self, repo_path: Path, store: _FakeMaterializationStore) -> None:
        self._store = store
        self._workspace = str(repo_path)
        self._repo_path = str(repo_path)
        self._session_id = "session-test"
        self._lifecycle_substrates = []
        self.lifecycle_substrates = self._lifecycle_substrates
        self._patch_manager = _FakePatchManager()
        self._active_scopes = {}
        self._orphaned_refs = []
        self._scope_registry_mismatches = []
        self._ground = None
        self._lock = _RecordingRLock()
        self.readiness_requests = []

    def _ensure_no_interrupted_lifecycle(self, attempted: str) -> None:
        del attempted

    def _ensure_no_open_operation(self, attempted: str) -> None:
        del attempted

    def query_readiness(self, request: object) -> object:
        self.readiness_requests.append(request)
        return SimpleNamespace(allowed=True, blockers=(), snapshot=SimpleNamespace(items=()), request=request)

    def recovery_inventory(self) -> InventorySnapshot:
        return InventorySnapshot.create(items=())

    def list_sibling_group_blockers(self) -> tuple[str, ...]:
        return ()

    def list_workspace_authority_pending(self) -> tuple[str, ...]:
        return ()

    def _make_ground_scope(self) -> None:
        return None


@pytest.fixture
def mg(workspace: Path) -> VcsCore:
    """Provide an activated VcsCore with marker + filesystem substrates."""
    vcscore = make_marker_filesystem_vcscore(workspace, activate=True)
    try:
        yield vcscore
    finally:
        vcscore.deactivate()


def test_materialization_coordinator_blocks_live_scope_before_planning(tmp_path: Path) -> None:
    store = _FakeMaterializationStore()
    coordinator = MaterializationCoordinator(_coordinator_deps(tmp_path, store, active_scope_names=("task-open",)))

    with pytest.raises(OpenScopeError, match="requires no live child branches"):
        coordinator.plan_push()

    assert store.advance_materialized_calls == 0


def test_materialization_admission_uses_readiness_for_push_recovery_policy() -> None:
    readiness_calls: list[tuple[str, str]] = []
    admission = _materialization_admission(readiness_calls=readiness_calls)

    admission.require_push_allowed()

    assert readiness_calls == [("vcscore.push-status", "push")]


def test_materialization_admission_uses_reset_readiness_without_push_only_checks() -> None:
    readiness_calls: list[tuple[str, str]] = []
    admission = _materialization_admission(readiness_calls=readiness_calls)

    admission.require_reset_allowed()

    assert readiness_calls == [("vcscore.reset-materialized", "reset to materialized")]


def test_vcscore_materialization_apis_block_pending_workspace_authority(mg: VcsCore) -> None:
    commit = mg.store.resolve_to_commit(GROUND_REF)
    pending = WorkspaceAuthorityPending(
        operation_id="wv_scan_direct_materialization_block",
        source_operation_id="op_direct_materialization_block",
        driver_command="scan",
        scope_name=mg.ground.name,
        scope_ref=mg.ground.ref,
        scope_instance_id=mg.ground.instance_id,
        scope_world_id=mg.ground.world_id,
        expected_input_world_oid=None,
        scalar_source_commit=str(commit.id) if commit is not None else None,
    ).with_update(phase="scalar_committed")
    write_pending_workspace_authority(mg._repo_path, pending)

    blocked_calls = (
        mg.plan_push,
        mg.assess_push,
        lambda: mg.push(dry_run=True),
        mg.push,
        mg.reset_to_materialized,
    )
    for call in blocked_calls:
        with pytest.raises(WorkspaceAuthorityRecoveryRequiredError, match="wv_scan_direct_materialization_block"):
            call()


def test_materialization_coordinator_repair_recovery_advances_and_clears_flag(tmp_path: Path) -> None:
    store = _FakeMaterializationStore()
    write_dirty_flag(str(tmp_path), "crashed-session")
    coordinator = MaterializationCoordinator(_coordinator_deps(tmp_path, store))

    coordinator.recover_dirty_push(mode="repair")

    assert store.advance_materialized_calls == 1
    assert read_dirty_flag(str(tmp_path)) is None


def test_recover_dirty_push_wrapper_holds_owner_lock(tmp_path: Path) -> None:
    from vcs_core import _vcscore_materialization

    store = _FakeMaterializationStore()
    owner = _RecoverOwner(tmp_path, store)
    write_dirty_flag(str(tmp_path), "crashed-session")

    _vcscore_materialization.recover_dirty_push(owner)  # type: ignore[arg-type]

    assert owner._lock.events == ["enter", "exit"]
    assert store.advance_materialized_calls == 1


def test_plan_push_wrapper_holds_owner_lock(tmp_path: Path) -> None:
    from vcs_core import _vcscore_materialization

    store = _FakeMaterializationStore()
    owner = _RecoverOwner(tmp_path, store)

    plan = _vcscore_materialization.plan_push(owner)  # type: ignore[arg-type]

    assert owner._lock.events == ["enter", "exit"]
    assert plan.total_operations == 0
    assert store.walk_pending_calls == 1
    assert store.advance_materialized_calls == 0


def test_push_dry_run_wrapper_holds_owner_lock_without_advancing(tmp_path: Path) -> None:
    from vcs_core import _vcscore_materialization

    store = _FakeMaterializationStore()
    owner = _RecoverOwner(tmp_path, store)

    plan = _vcscore_materialization.push(owner, dry_run=True)  # type: ignore[arg-type]

    assert owner._lock.events == ["enter", "exit"]
    assert plan.total_operations == 0
    assert store.walk_pending_calls == 1
    assert store.advance_materialized_calls == 0


def test_recover_dirty_push_wrapper_is_safe_when_lock_already_held(tmp_path: Path) -> None:
    from vcs_core import _vcscore_materialization

    store = _FakeMaterializationStore()
    owner = _RecoverOwner(tmp_path, store)
    write_dirty_flag(str(tmp_path), "crashed-session")

    with owner._lock:
        _vcscore_materialization.recover_dirty_push(owner)  # type: ignore[arg-type]

    assert owner._lock.events == ["enter", "enter", "exit", "exit"]
    assert store.advance_materialized_calls == 1


def test_plan_materialization_preserves_filesystem_summary(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-plan")
    filesystem = mg.lifecycle_substrates[1]
    filesystem.record_changes([("phase2.py", b"print('phase2')\n")])  # type: ignore[attr-defined]
    mg.merge(task, mg.ground)

    planned = plan_materialization(mg._store)

    assert len(planned.units) == 1
    unit = planned.units[0]
    assert unit.unit_id == "filesystem:workspace"
    assert unit.materializer_key == "builtin:filesystem"
    assert unit.substrate == "filesystem"
    assert unit.target_id == "workspace"
    assert unit.reversibility == "auto"
    assert [(change.path, change.status) for change in unit.file_changes] == [("phase2.py", "added")]

    assert planned.plan.commits_ahead > 0
    assert len(planned.plan.phases) == 1
    phase = planned.plan.phases[0]
    assert phase.reversibility == "auto"
    assert [(change.path, change.status) for change in phase.file_changes] == [("phase2.py", "added")]
    assert phase.intents == []


def test_plan_materialization_orders_filesystem_by_first_relevant_commit(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-plan-order")
    marker = mg.lifecycle_substrates[0]
    filesystem = mg.lifecycle_substrates[1]

    marker.mark("before-filesystem")  # type: ignore[attr-defined]
    filesystem.record_changes([("phase2-order.py", b"print('ordered')\n")])  # type: ignore[attr-defined]
    mg.merge(task, mg.ground)

    planned = plan_materialization(mg._store)

    assert len(planned.units) == 1
    assert planned.units[0].commit_index == 1


def test_build_materializers_uses_substrate_registration(mg: VcsCore) -> None:
    materializers = build_materializers(mg.lifecycle_substrates)

    assert [materializer.materializer_key for materializer in materializers] == ["builtin:filesystem"]


def test_plan_materialization_does_not_fall_back_from_explicit_empty_materializers(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-no-fallback")
    filesystem = mg.lifecycle_substrates[1]
    filesystem.record_changes([("phase2-empty.py", b"print('empty')\n")])  # type: ignore[attr-defined]
    mg.merge(task, mg.ground)

    with pytest.raises(RuntimeError, match="unavailable materializer 'builtin:filesystem'"):
        plan_materialization(mg._store, materializers=())


def test_push_fails_closed_before_side_effects_for_unknown_materializer(workspace: Path) -> None:
    class SpySubstrate:
        name = "spy"
        commands = {}
        effects = {}

        def __init__(self) -> None:
            self.push_calls = 0

        def bind_pipeline(self, pipeline, *, scope_queries=None) -> None:
            del pipeline, scope_queries

        def activate(self) -> None:
            pass

        def deactivate(self) -> None:
            pass

        def push(self, scope_id: str | None = None) -> None:
            del scope_id
            self.push_calls += 1

        def authority(self):
            return None

        def python_patches(self) -> tuple[object, ...]:
            return ()

    store = make_store(workspace)
    spy = SpySubstrate()
    vcscore = VcsCore(str(workspace), substrates=[spy], store=store)  # type: ignore[list-item]
    vcscore.activate()
    try:
        task = vcscore.fork(vcscore.ground, "task-missing-materializer")
        store._emit_effect(
            task,
            "CustomReplay",
            {"materializer_key": "custom.sqlite"},
            substrate="custom",
        )
        vcscore.merge(task, vcscore.ground)

        with pytest.raises(RuntimeError, match=r"unavailable materializer 'custom\.sqlite'"):
            vcscore.push()

        assert spy.push_calls == 0
    finally:
        vcscore.deactivate()


def test_push_fails_closed_for_pending_filesystem_work_without_filesystem_materializer(workspace: Path) -> None:
    store = make_store(workspace)
    marker, _ = make_marker_filesystem_substrates(store)
    vcscore = VcsCore(str(workspace), substrates=[marker], store=store)
    vcscore.activate()
    try:
        task = vcscore.fork(vcscore.ground, "task-missing-filesystem-materializer")
        store._emit_effect(
            task,
            "FileCreate",
            {"path": "missing.txt"},
            substrate="filesystem",
            workspace_changes=(("missing.txt", b"payload"),),
        )
        vcscore.merge(task, vcscore.ground)

        with pytest.raises(RuntimeError, match="unavailable materializer 'builtin:filesystem'"):
            vcscore.push()
    finally:
        vcscore.deactivate()


def test_push_materializes_overlay_filesystem_from_store_diff(workspace: Path) -> None:
    store = make_store(workspace)
    backend = MockOverlayBackend()
    marker, filesystem = make_marker_filesystem_substrates(store, declarative=False, backend=backend)
    vcscore = VcsCore(str(workspace), substrates=[marker, filesystem], store=store)
    vcscore.activate()
    try:
        task = vcscore.fork(vcscore.ground, "task-overlay-push", hints={"isolated": True})
        outcome = vcscore.exec("filesystem", "write", scope=task, path="overlay.txt", content=b"payload")
        assert outcome.oids == ()

        vcscore.merge(task, vcscore.ground)
        vcscore.push()

        assert backend.pushed == []
        assert (workspace / "overlay.txt").read_bytes() == b"payload"
        assert vcscore.status().commits_ahead == 0
    finally:
        vcscore.deactivate()


def test_push_refuses_physical_file_that_differs_from_materialized_baseline(workspace: Path) -> None:
    store = make_store(workspace)
    marker, filesystem = make_marker_filesystem_substrates(store, declarative=False)
    vcscore = VcsCore(str(workspace), substrates=[marker, filesystem], store=store)
    vcscore.activate()
    try:
        task = vcscore.fork(vcscore.ground, "task-conflict")
        filesystem.record_changes([("conflict.txt", b"expected")])
        vcscore.merge(task, vcscore.ground)
        vcscore.push()

        task = vcscore.fork(vcscore.ground, "task-conflict-update")
        filesystem.record_changes([("conflict.txt", b"desired")])
        vcscore.merge(task, vcscore.ground)
        with vcscore._patch_manager.guard():
            (workspace / "conflict.txt").write_text("external")

        with pytest.raises(RuntimeError, match=r"conflict.txt.*worktree-not-adopted"):
            vcscore.push()
    finally:
        vcscore.deactivate()


def test_push_noop_refuses_unrelated_plain_directory_file(workspace: Path) -> None:
    store = make_store(workspace)
    marker, filesystem = make_marker_filesystem_substrates(store, declarative=False)
    vcscore = VcsCore(str(workspace), substrates=[marker, filesystem], store=store)
    vcscore.activate()
    try:
        with vcscore._patch_manager.guard():
            (workspace / "helper.py").write_text("print('helper')\n")

        with pytest.raises(RuntimeError, match=r"helper.py.*worktree-not-adopted"):
            vcscore.push(dry_run=True)
        with pytest.raises(RuntimeError, match=r"helper.py.*worktree-not-adopted"):
            vcscore.push()

        assert vcscore.status().commits_ahead == 0
    finally:
        vcscore.deactivate()


def test_push_refuses_unrelated_git_worktree_file_before_side_effects(workspace: Path) -> None:
    subprocess.run(["git", "-C", str(workspace), "init", "-q"], check=True)

    store = make_store(workspace)
    marker, filesystem = make_marker_filesystem_substrates(store, declarative=False)
    vcscore = VcsCore(str(workspace), substrates=[marker, filesystem], store=store)
    vcscore.activate()
    try:
        task = vcscore.fork(vcscore.ground, "task-baseline")
        filesystem.record_changes([("baseline.txt", b"baseline")])
        vcscore.merge(task, vcscore.ground)
        vcscore.push()

        with vcscore._patch_manager.guard():
            (workspace / "external.txt").write_text("external")

        task = vcscore.fork(vcscore.ground, "task-pending")
        filesystem.record_changes([("pending.txt", b"pending")])
        vcscore.merge(task, vcscore.ground)

        with pytest.raises(RuntimeError, match=r"external.txt.*worktree-not-adopted"):
            vcscore.push()

        assert (workspace / "external.txt").read_text() == "external"
        assert not (workspace / "pending.txt").exists()
        assert vcscore.status().commits_ahead > 0
    finally:
        vcscore.deactivate()


def test_push_refuses_unrelated_plain_directory_file_before_side_effects(workspace: Path) -> None:
    store = make_store(workspace)
    marker, filesystem = make_marker_filesystem_substrates(store, declarative=False)
    vcscore = VcsCore(str(workspace), substrates=[marker, filesystem], store=store)
    vcscore.activate()
    try:
        with vcscore._patch_manager.guard():
            (workspace / "helper.py").write_text("print('helper')\n")

        task = vcscore.fork(vcscore.ground, "task-pending")
        filesystem.record_changes([("pending.txt", b"pending")])
        vcscore.merge(task, vcscore.ground)

        with pytest.raises(RuntimeError, match=r"helper.py.*worktree-not-adopted"):
            vcscore.push()

        assert (workspace / "helper.py").read_text() == "print('helper')\n"
        assert not (workspace / "pending.txt").exists()
        assert vcscore.status().commits_ahead > 0
    finally:
        vcscore.deactivate()


def test_push_refuses_git_index_drift_before_side_effects(workspace: Path) -> None:
    subprocess.run(["git", "-C", str(workspace), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "Meta Git Test"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.email", "vcs-core-test@example.invalid"], check=True)

    store = make_store(workspace)
    marker, filesystem = make_marker_filesystem_substrates(store, declarative=False)
    vcscore = VcsCore(str(workspace), substrates=[marker, filesystem], store=store)
    vcscore.activate()
    try:
        task = vcscore.fork(vcscore.ground, "task-baseline")
        filesystem.record_changes([("tracked.txt", b"old\n")])
        vcscore.merge(task, vcscore.ground)
        vcscore.push()
        subprocess.run(["git", "-C", str(workspace), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "baseline"], check=True)

        with vcscore._patch_manager.guard():
            (workspace / "tracked.txt").write_text("new\n")
            subprocess.run(["git", "-C", str(workspace), "add", "tracked.txt"], check=True)
            (workspace / "tracked.txt").write_text("old\n")

        task = vcscore.fork(vcscore.ground, "task-pending")
        filesystem.record_changes([("pending.txt", b"pending")])
        vcscore.merge(task, vcscore.ground)

        with pytest.raises(RuntimeError, match=r"tracked.txt.*git-index-dirty"):
            vcscore.push()

        assert not (workspace / "pending.txt").exists()
        assert vcscore.status().commits_ahead > 0
    finally:
        vcscore.deactivate()


def test_push_ignores_gitignored_unsupported_path(workspace: Path) -> None:
    subprocess.run(["git", "-C", str(workspace), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "Meta Git Test"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.email", "vcs-core-test@example.invalid"], check=True)
    (workspace / ".git" / "info" / "exclude").write_text("ignored-link\n")

    store = make_store(workspace)
    marker, filesystem = make_marker_filesystem_substrates(store, declarative=False)
    vcscore = VcsCore(str(workspace), substrates=[marker, filesystem], store=store)
    vcscore.activate()
    try:
        with vcscore._patch_manager.guard():
            (workspace / "ignored-link").symlink_to(".git")

        task = vcscore.fork(vcscore.ground, "task-pending")
        filesystem.record_changes([("pending.txt", b"pending")])
        vcscore.merge(task, vcscore.ground)

        vcscore.push()

        assert (workspace / "ignored-link").is_symlink()
        assert (workspace / "pending.txt").read_bytes() == b"pending"
        assert vcscore.status().commits_ahead == 0
    finally:
        vcscore.deactivate()


def test_push_refuses_to_overwrite_ignored_exact_target(workspace: Path) -> None:
    subprocess.run(["git", "-C", str(workspace), "init", "-q"], check=True)
    (workspace / ".git" / "info" / "exclude").write_text("debug.log\n")

    store = make_store(workspace)
    marker, filesystem = make_marker_filesystem_substrates(store, declarative=False)
    vcscore = VcsCore(str(workspace), substrates=[marker, filesystem], store=store)
    vcscore.activate()
    try:
        with vcscore._patch_manager.guard():
            (workspace / "debug.log").write_text("external ignored\n")

        task = vcscore.fork(vcscore.ground, "task-ignored-conflict")
        filesystem.record_changes([("debug.log", b"vcs-core desired\n")])
        vcscore.merge(task, vcscore.ground)

        with pytest.raises(RuntimeError, match=r"debug.log.*materialized baseline is absent"):
            vcscore.push()

        assert (workspace / "debug.log").read_text() == "external ignored\n"
        assert vcscore.status().commits_ahead > 0
    finally:
        vcscore.deactivate()


def test_push_refuses_ignored_unsupported_exact_target(workspace: Path) -> None:
    subprocess.run(["git", "-C", str(workspace), "init", "-q"], check=True)
    (workspace / ".git" / "info" / "exclude").write_text("ignored-dir\n")

    store = make_store(workspace)
    marker, filesystem = make_marker_filesystem_substrates(store, declarative=False)
    vcscore = VcsCore(str(workspace), substrates=[marker, filesystem], store=store)
    vcscore.activate()
    try:
        with vcscore._patch_manager.guard():
            (workspace / "ignored-dir").mkdir()

        task = vcscore.fork(vcscore.ground, "task-ignored-dir-conflict")
        filesystem.record_changes([("ignored-dir", b"vcs-core desired\n")])
        vcscore.merge(task, vcscore.ground)

        with pytest.raises(RuntimeError, match=r"ignored-dir.*unsupported directory"):
            vcscore.push()

        assert (workspace / "ignored-dir").is_dir()
        assert vcscore.status().commits_ahead > 0
    finally:
        vcscore.deactivate()
