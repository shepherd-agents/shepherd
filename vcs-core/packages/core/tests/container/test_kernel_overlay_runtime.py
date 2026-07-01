"""Linux-only runtime validation for the real kernel overlay flow."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from vcs_core._substrate_runtime import build_builtin_substrate_context
from vcs_core.store import Store
from vcs_core.substrates import FilesystemSubstrate, MarkerSubstrate
from vcs_core.vcscore import VcsCore

pytestmark = [
    pytest.mark.container,
    pytest.mark.skipif(
        sys.platform != "linux" or os.geteuid() != 0,
        reason="Kernel overlay runtime tests require Linux and root privileges.",
    ),
]


def _ensure_overlay_available() -> None:
    filesystems = Path("/proc/filesystems")
    if not filesystems.exists():
        pytest.skip("/proc/filesystems not available")
    if "overlay" not in filesystems.read_text():
        pytest.skip("overlayfs is not available in this environment")
    if shutil.which("mount") is None or shutil.which("umount") is None:
        pytest.skip("mount/umount are required for kernel overlay validation")


@pytest.fixture
def kernel_overlay_state_root(tmp_path: Path) -> Path:
    _ensure_overlay_available()
    configured = os.environ.get("VCS_CORE_KERNEL_OVERLAY_STATE_ROOT")
    if configured:
        root = Path(configured) / f"vcs-core-{uuid.uuid4().hex[:8]}"
    else:
        root = tmp_path / "overlay-state"
    root.mkdir(parents=True, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def runtime_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


@pytest.fixture
def kernel_mg(runtime_workspace: Path, kernel_overlay_state_root: Path) -> VcsCore:
    repo_path = runtime_workspace / ".vcscore"
    store = Store(str(repo_path))
    context = build_builtin_substrate_context(
        store,
        workspace=runtime_workspace,
        config={"backend": "kernel", "state_root": str(kernel_overlay_state_root)},
    )
    marker = MarkerSubstrate(context)
    filesystem = FilesystemSubstrate(context)
    mg = VcsCore(str(runtime_workspace), substrates=[marker, filesystem], store=store)
    try:
        mg.activate()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"Kernel overlay runtime not available in this environment: {exc}")
    yield mg
    mg.deactivate()


def _filesystem_backend(mg: VcsCore):
    filesystem = next(sub for sub in mg.lifecycle_substrates if getattr(sub, "name", None) == "filesystem")
    return filesystem._backend


def test_kernel_runtime_write_merge_push_materializes_only_at_push(kernel_mg: VcsCore, runtime_workspace: Path) -> None:
    task = kernel_mg.fork(kernel_mg.ground, "task-kernel", hints={"isolated": True})

    kernel_mg.exec("filesystem", "write", scope=task, path="src/example.py", content=b"print('hi')\n")

    assert not (runtime_workspace / "src" / "example.py").exists()

    kernel_mg.merge(task, kernel_mg.ground)
    assert not (runtime_workspace / "src" / "example.py").exists()

    kernel_mg.push()

    assert (runtime_workspace / "src" / "example.py").read_bytes() == b"print('hi')\n"
    assert kernel_mg.status().commits_ahead == 0


def test_kernel_runtime_prepare_merge_is_non_destructive(kernel_mg: VcsCore) -> None:
    task = kernel_mg.fork(kernel_mg.ground, "task-prepare", hints={"isolated": True})
    backend = _filesystem_backend(kernel_mg)

    kernel_mg.exec("filesystem", "write", scope=task, path="prepare.txt", content=b"payload")
    assert backend.has_layer(task.name) is True

    filesystem = next(sub for sub in kernel_mg.lifecycle_substrates if getattr(sub, "name", None) == "filesystem")
    effects = filesystem.prepare_merge(task, kernel_mg.ground)

    assert backend.has_layer(task.name) is True
    assert len(effects) == 1
    assert effects[0].effect_type == "FileCreate"
    assert effects[0].metadata["path"] == "prepare.txt"


def test_kernel_runtime_discard_removes_layer_without_materializing(
    kernel_mg: VcsCore, runtime_workspace: Path
) -> None:
    task = kernel_mg.fork(kernel_mg.ground, "task-discard", hints={"isolated": True})
    backend = _filesystem_backend(kernel_mg)

    kernel_mg.exec("filesystem", "write", scope=task, path="discard.txt", content=b"payload")
    assert backend.has_layer(task.name) is True

    kernel_mg.discard(task)

    assert backend.has_layer(task.name) is False
    assert not (runtime_workspace / "discard.txt").exists()


def test_kernel_runtime_non_isolated_child_reuses_parent_overlay(kernel_mg: VcsCore, runtime_workspace: Path) -> None:
    task = kernel_mg.fork(kernel_mg.ground, "task-parent", hints={"isolated": True})
    tool = kernel_mg.fork(task, "tool-child", hints={"isolated": False})
    backend = _filesystem_backend(kernel_mg)

    kernel_mg.exec("filesystem", "write", scope=tool, path="nested/tool.txt", content=b"tool-output")

    assert backend.has_layer(task.name) is True
    assert backend.has_layer(tool.name) is False

    kernel_mg.merge(tool, task)
    kernel_mg.merge(task, kernel_mg.ground)

    assert not (runtime_workspace / "nested" / "tool.txt").exists()

    kernel_mg.push()

    assert (runtime_workspace / "nested" / "tool.txt").read_bytes() == b"tool-output"


# --- Tier 1: R1a N/A items ---


def test_kernel_runtime_deactivate_without_push_leaves_workspace_clean(
    runtime_workspace: Path,
    kernel_overlay_state_root: Path,
) -> None:
    """R1a item 3: deactivate without push must not touch the real workspace."""
    repo_path = runtime_workspace / ".vcscore"
    store = Store(str(repo_path))
    context = build_builtin_substrate_context(
        store,
        workspace=runtime_workspace,
        config={"backend": "kernel", "state_root": str(kernel_overlay_state_root)},
    )
    marker = MarkerSubstrate(context)
    filesystem = FilesystemSubstrate(context)
    mg = VcsCore(str(runtime_workspace), substrates=[marker, filesystem], store=store)
    try:
        mg.activate()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"Kernel overlay not available: {exc}")

    task = mg.fork(mg.ground, "edit", hints={"isolated": True})
    mg.exec("filesystem", "write", scope=task, path="secret.txt", content=b"do-not-materialize")
    mg.merge(task, mg.ground)

    # Deactivate without push
    mg.deactivate()

    assert not (runtime_workspace / "secret.txt").exists()


def test_kernel_runtime_overlay_truth_reconciliation(kernel_mg: VcsCore) -> None:
    """R1a item 21: orphaned overlay changes from discarded non-isolated
    children are captured at the containing isolated scope's merge boundary."""
    task = kernel_mg.fork(kernel_mg.ground, "task-recon", hints={"isolated": True})
    tool0 = kernel_mg.fork(task, "tool-0", hints={"isolated": False})

    # tool-0 writes file X
    kernel_mg.exec("filesystem", "write", scope=tool0, path="file-x.txt", content=b"from-tool-0")

    # Discard tool-0 — its branch is archived, but overlay changes persist
    kernel_mg.discard(tool0)

    # File X is still in the overlay (non-isolated child doesn't undo FS state)
    backend = _filesystem_backend(kernel_mg)
    content_x = backend.read_file(task.name, "file-x.txt")
    assert content_x == b"from-tool-0"

    # tool-retry writes file Y (not X)
    tool_retry = kernel_mg.fork(task, "tool-retry", hints={"isolated": False})
    kernel_mg.exec("filesystem", "write", scope=tool_retry, path="file-y.txt", content=b"from-retry")
    kernel_mg.merge(tool_retry, task)

    # Merge task into ground — prepare_merge() should capture BOTH X and Y
    kernel_mg.merge(task, kernel_mg.ground)

    # Verify ground log has effects for both files
    fs_effects = kernel_mg.filter_effects(substrate="filesystem")
    effect_paths = {e.metadata.get("path") for e in fs_effects}
    assert "file-x.txt" in effect_paths
    assert "file-y.txt" in effect_paths

    # Verify Store workspace tree at ground has both files
    store = kernel_mg.store
    assert store.file_exists_in_workspace(Store.GROUND_REF, "file-x.txt")
    assert store.file_exists_in_workspace(Store.GROUND_REF, "file-y.txt")

    # Verify tool-0 archive ref exists (provenance for file X)
    archive_refs = store.list_archive_refs()
    tool0_archives = [r for r in archive_refs if "tool-0" in r]
    assert len(tool0_archives) >= 1


def test_kernel_runtime_recording_scope_with_isolated_parent(kernel_mg: VcsCore) -> None:
    """R1a item 12 area / R1b test 12: non-isolated child within isolated
    parent — P3 suppresses per-op file effects; cooperative child effects
    land on the containing branch and filesystem effects surface only when
    the isolated parent merges upward."""
    task = kernel_mg.fork(kernel_mg.ground, "task-rec", hints={"isolated": True})
    tool = kernel_mg.fork(task, "tool-rec", hints={"isolated": False})

    # Write file — under P3, this should return no per-op effects
    outcome = kernel_mg.exec("filesystem", "write", scope=tool, path="code.py", content=b"hello")
    assert outcome.oids == ()  # P3: overlay-active suppresses per-op write effects

    # Record a marker effect on the tool scope
    kernel_mg.exec("marker", "mark", scope=tool, label="tool-marker")

    # Merge tool into task. Non-isolated children share the containing
    # overlay, so cooperative effects land on task history, while the
    # filesystem effect is deferred until task merges upward.
    kernel_mg.merge(tool, task)

    # Verify marker effect is now present on the containing task branch.
    marker_effects = kernel_mg.filter_effects(substrate="marker", ref=task.ref)
    marker_labels = [e.metadata.get("label") for e in marker_effects]
    assert "tool-marker" in marker_labels

    # The shared overlay change is not yet materialized into task history.
    fs_effects = kernel_mg.filter_effects(substrate="filesystem", ref=task.ref)
    assert fs_effects == []

    # Once the isolated parent merges upward, the overlay diff is recorded.
    kernel_mg.merge(task, kernel_mg.ground)
    fs_effects = kernel_mg.filter_effects(substrate="filesystem")
    fs_paths = [e.metadata.get("path") for e in fs_effects]
    assert "code.py" in fs_paths


# --- Tier 2: Multi-scope overlay patterns ---


def test_kernel_runtime_nested_branches_sequential(kernel_mg: VcsCore, runtime_workspace: Path) -> None:
    """R1b test 4: sequential children within an isolated parent correctly
    see each other's changes via the overlay and prepare_merge captures all."""
    task = kernel_mg.fork(kernel_mg.ground, "task-seq", hints={"isolated": True})
    backend = _filesystem_backend(kernel_mg)

    # tool-0 writes file A
    tool0 = kernel_mg.fork(task, "tool-0", hints={"isolated": False})
    kernel_mg.exec("filesystem", "write", scope=tool0, path="file-a.txt", content=b"aaa")
    kernel_mg.merge(tool0, task)

    # tool-1 should see file A via the overlay
    tool1 = kernel_mg.fork(task, "tool-1", hints={"isolated": False})
    content_a = backend.read_file(task.name, "file-a.txt")
    assert content_a == b"aaa"

    # tool-1 writes file B
    kernel_mg.exec("filesystem", "write", scope=tool1, path="file-b.txt", content=b"bbb")
    kernel_mg.merge(tool1, task)

    # Merge task into ground and push
    kernel_mg.merge(task, kernel_mg.ground)
    kernel_mg.push()

    # Both files materialized
    assert (runtime_workspace / "file-a.txt").read_bytes() == b"aaa"
    assert (runtime_workspace / "file-b.txt").read_bytes() == b"bbb"

    # Both file effects present in ground log
    fs_effects = kernel_mg.filter_effects(substrate="filesystem")
    effect_paths = {e.metadata.get("path") for e in fs_effects}
    assert "file-a.txt" in effect_paths
    assert "file-b.txt" in effect_paths


def test_kernel_runtime_discard_archive_ref(kernel_mg: VcsCore) -> None:
    """R1b test 5: discard archives the scope; archive ref is queryable."""
    task = kernel_mg.fork(kernel_mg.ground, "doomed", hints={"isolated": True})
    kernel_mg.exec("filesystem", "write", scope=task, path="doomed.txt", content=b"payload")
    instance_id = task.instance_id

    kernel_mg.discard(task)

    store = kernel_mg.store

    # Ground has no trace of the discarded changes
    assert not store.file_exists_in_workspace(Store.GROUND_REF, "doomed.txt")

    # Archive ref exists with the expected naming pattern
    expected_archive = f"refs/vcscore/archive/doomed-{instance_id}"
    assert store.ref_exists(expected_archive)

    # Archive ref's workspace tree contains the written file
    assert store.file_exists_in_workspace(expected_archive, "doomed.txt")
    assert store.read_workspace_file(expected_archive, "doomed.txt") == b"payload"


def test_kernel_runtime_effect_interleaving(kernel_mg: VcsCore) -> None:
    """R1b test 15: cooperative substrate effects interleave correctly
    with overlay-backed filesystem changes; cooperative effects land on the
    containing branch first, and overlay reconciliation happens when that
    isolated branch merges upward."""
    task = kernel_mg.fork(kernel_mg.ground, "task-il", hints={"isolated": True})
    tool = kernel_mg.fork(task, "tool-il", hints={"isolated": False})

    # Record marker before file write
    kernel_mg.exec("marker", "mark", scope=tool, label="step-start")

    # Write file via overlay
    kernel_mg.exec("filesystem", "write", scope=tool, path="output.txt", content=b"result")

    # Record marker after file write
    kernel_mg.exec("marker", "mark", scope=tool, label="step-end")

    # Merge tool into task. Cooperative effects are promoted now, but the
    # shared overlay write still belongs to the isolated parent.
    kernel_mg.merge(tool, task)

    # Inspect the containing task branch log.
    log = kernel_mg.log(ref=task.ref)
    effect_types = [c.metadata.get("type") for c in log]

    # Marker effects should be present on the task branch.
    assert "Marker" in effect_types

    # The shared overlay write is still pending on task's overlay layer.
    assert "FileCreate" not in effect_types
    assert "FilePatch" not in effect_types

    marker_effects = kernel_mg.filter_effects(substrate="marker", ref=task.ref)
    fs_effects = kernel_mg.filter_effects(substrate="filesystem", ref=task.ref)
    assert len(marker_effects) >= 2  # step-start and step-end
    assert fs_effects == []

    # Merging the isolated parent upward records the deferred filesystem
    # change while preserving the cooperative marker history.
    kernel_mg.merge(task, kernel_mg.ground)
    log = kernel_mg.log()
    effect_types = [c.metadata.get("type") for c in log]
    assert "Marker" in effect_types
    assert "FileCreate" in effect_types or "FilePatch" in effect_types


def test_kernel_runtime_recording_only_rollback_excludes_effects(kernel_mg: VcsCore) -> None:
    """R1b test 13: discard removes cooperative effects from branch history;
    overlay changes persist across non-isolated discard."""
    task = kernel_mg.fork(kernel_mg.ground, "task-rb", hints={"isolated": True})
    tool = kernel_mg.fork(task, "tool-rb", hints={"isolated": False})
    backend = _filesystem_backend(kernel_mg)

    # Record a marker and write a file on the tool scope
    kernel_mg.exec("marker", "mark", scope=tool, label="doomed-marker")
    kernel_mg.exec("filesystem", "write", scope=tool, path="orphan.txt", content=b"orphan-data")

    # Discard tool — marker effect is lost, but overlay file persists
    kernel_mg.discard(tool)

    # File still in overlay (non-isolated child, parent overlay intact)
    content = backend.read_file(task.name, "orphan.txt")
    assert content == b"orphan-data"

    # Retry tool — no new writes, just merge to capture orphan.txt
    tool_retry = kernel_mg.fork(task, "tool-retry", hints={"isolated": False})
    kernel_mg.merge(tool_retry, task)

    # Merge task into ground
    kernel_mg.merge(task, kernel_mg.ground)

    # Ground log should have the file effect (from overlay) but NOT the discarded marker
    fs_effects = kernel_mg.filter_effects(substrate="filesystem")
    fs_paths = [e.metadata.get("path") for e in fs_effects]
    assert "orphan.txt" in fs_paths

    marker_effects = kernel_mg.filter_effects(substrate="marker")
    marker_labels = [e.metadata.get("label") for e in marker_effects]
    assert "doomed-marker" not in marker_labels


# --- Tier 3: Durability under overlay ---


def test_kernel_runtime_resumable_activation(
    runtime_workspace: Path,
    kernel_overlay_state_root: Path,
) -> None:
    """R1b test 8: pending ground state is rehydrated across activation cycles."""
    repo_path = runtime_workspace / ".vcscore"

    def _make_mg() -> VcsCore:
        store = Store(str(repo_path))
        context = build_builtin_substrate_context(
            store,
            workspace=runtime_workspace,
            config={"backend": "kernel", "state_root": str(kernel_overlay_state_root)},
        )
        marker = MarkerSubstrate(context)
        filesystem = FilesystemSubstrate(context)
        return VcsCore(str(runtime_workspace), substrates=[marker, filesystem], store=store)

    # Session 1: create work, merge, deactivate without push
    mg1 = _make_mg()
    try:
        mg1.activate()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"Kernel overlay not available: {exc}")

    task = mg1.fork(mg1.ground, "work", hints={"isolated": True})
    mg1.exec("filesystem", "write", scope=task, path="resume.txt", content=b"session-1")
    mg1.merge(task, mg1.ground)
    mg1.deactivate()

    # File should NOT be in workspace yet
    assert not (runtime_workspace / "resume.txt").exists()

    # Session 2: reactivate — pending state rehydrated
    mg2 = _make_mg()
    mg2.activate()

    assert mg2.status().commits_ahead > 0

    mg2.push()
    assert (runtime_workspace / "resume.txt").read_bytes() == b"session-1"
    assert mg2.status().commits_ahead == 0

    mg2.deactivate()


def test_kernel_runtime_push_crash_recovery(
    runtime_workspace: Path,
    kernel_overlay_state_root: Path,
) -> None:
    """R1b test 7: dirty flag protocol interacts correctly with overlay."""
    from vcs_core._dirty_flag import write_dirty_flag

    repo_path = runtime_workspace / ".vcscore"

    def _make_mg() -> VcsCore:
        store = Store(str(repo_path))
        context = build_builtin_substrate_context(
            store,
            workspace=runtime_workspace,
            config={"backend": "kernel", "state_root": str(kernel_overlay_state_root)},
        )
        marker = MarkerSubstrate(context)
        filesystem = FilesystemSubstrate(context)
        return VcsCore(str(runtime_workspace), substrates=[marker, filesystem], store=store)

    # Session 1: create work, merge, simulate crash mid-push
    mg1 = _make_mg()
    try:
        mg1.activate()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"Kernel overlay not available: {exc}")

    task = mg1.fork(mg1.ground, "crash-edit", hints={"isolated": True})
    mg1.exec("filesystem", "write", scope=task, path="crash.txt", content=b"crash-data")
    mg1.merge(task, mg1.ground)

    # Simulate crash: write dirty flag without completing push
    write_dirty_flag(str(repo_path), "crash-session")
    mg1.deactivate()

    # Session 2: activate with recover="repair"
    mg2 = _make_mg()
    mg2.activate(recover="repair")

    # Repair should have advanced materialized ref
    assert mg2.status().commits_ahead == 0

    # The synthetic repair advanced bookkeeping without applying the physical
    # side effect, so fail-closed admission should report the mismatch.
    with pytest.raises(RuntimeError, match=r"crash.txt.*worktree-not-adopted"):
        mg2.push()

    mg2.deactivate()
