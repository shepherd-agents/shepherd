"""Linux-only runtime validation for the real fuse-overlayfs flow."""

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
        sys.platform != "linux",
        reason="Fuse overlay runtime tests require Linux.",
    ),
]


def _ensure_fuse_available() -> None:
    if not Path("/dev/fuse").exists():
        pytest.skip("/dev/fuse is not available in this environment")
    if shutil.which("fuse-overlayfs") is None or shutil.which("fusermount3") is None:
        pytest.skip("fuse-overlayfs and fusermount3 are required for fuse overlay validation")


@pytest.fixture
def fuse_overlay_state_root(tmp_path: Path) -> Path:
    _ensure_fuse_available()
    configured = os.environ.get("VCS_CORE_OVERLAY_STATE_ROOT")
    if configured:
        root = Path(configured) / f"vcs-core-fuse-{uuid.uuid4().hex[:8]}"
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
def fuse_mg(runtime_workspace: Path, fuse_overlay_state_root: Path) -> VcsCore:
    repo_path = runtime_workspace / ".vcscore"
    store = Store(str(repo_path))
    context = build_builtin_substrate_context(
        store,
        workspace=runtime_workspace,
        config={"backend": "fuse", "state_root": str(fuse_overlay_state_root)},
    )
    marker = MarkerSubstrate(context)
    filesystem = FilesystemSubstrate(context)
    mg = VcsCore(str(runtime_workspace), substrates=[marker, filesystem], store=store)
    try:
        mg.activate()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"Fuse overlay runtime not available in this environment: {exc}")
    yield mg
    mg.deactivate()


def _filesystem_backend(mg: VcsCore):
    filesystem = next(sub for sub in mg.lifecycle_substrates if getattr(sub, "name", None) == "filesystem")
    return filesystem._backend


def test_fuse_runtime_write_merge_push_materializes_only_at_push(fuse_mg: VcsCore, runtime_workspace: Path) -> None:
    task = fuse_mg.fork(fuse_mg.ground, "task-fuse", hints={"isolated": True})

    fuse_mg.exec("filesystem", "write", scope=task, path="src/example.py", content=b"print('hi')\n")

    assert not (runtime_workspace / "src" / "example.py").exists()

    fuse_mg.merge(task, fuse_mg.ground)
    assert not (runtime_workspace / "src" / "example.py").exists()

    fuse_mg.push()

    assert (runtime_workspace / "src" / "example.py").read_bytes() == b"print('hi')\n"
    assert fuse_mg.status().commits_ahead == 0


def test_fuse_runtime_discard_removes_layer_without_materializing(fuse_mg: VcsCore, runtime_workspace: Path) -> None:
    task = fuse_mg.fork(fuse_mg.ground, "task-discard", hints={"isolated": True})
    backend = _filesystem_backend(fuse_mg)

    fuse_mg.exec("filesystem", "write", scope=task, path="discard.txt", content=b"payload")
    assert backend.has_layer(task.name) is True

    fuse_mg.discard(task)

    assert backend.has_layer(task.name) is False
    assert not (runtime_workspace / "discard.txt").exists()


# --- FUSE parity tests for key overlay integration scenarios ---


def test_fuse_runtime_deactivate_without_push_leaves_workspace_clean(
    runtime_workspace: Path,
    fuse_overlay_state_root: Path,
) -> None:
    """R1a item 3 (FUSE parity): deactivate without push must not touch the real workspace."""
    repo_path = runtime_workspace / ".vcscore"
    store = Store(str(repo_path))
    context = build_builtin_substrate_context(
        store,
        workspace=runtime_workspace,
        config={"backend": "fuse", "state_root": str(fuse_overlay_state_root)},
    )
    marker = MarkerSubstrate(context)
    filesystem = FilesystemSubstrate(context)
    mg = VcsCore(str(runtime_workspace), substrates=[marker, filesystem], store=store)
    try:
        mg.activate()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"Fuse overlay not available: {exc}")

    task = mg.fork(mg.ground, "edit", hints={"isolated": True})
    mg.exec("filesystem", "write", scope=task, path="secret.txt", content=b"do-not-materialize")
    mg.merge(task, mg.ground)

    mg.deactivate()

    assert not (runtime_workspace / "secret.txt").exists()


def test_fuse_runtime_nested_branches_sequential(fuse_mg: VcsCore, runtime_workspace: Path) -> None:
    """R1b test 4 (FUSE parity): sequential children within an isolated
    parent correctly see each other's changes and materialize on push."""
    task = fuse_mg.fork(fuse_mg.ground, "task-seq", hints={"isolated": True})
    backend = _filesystem_backend(fuse_mg)

    # tool-0 writes file A
    tool0 = fuse_mg.fork(task, "tool-0", hints={"isolated": False})
    fuse_mg.exec("filesystem", "write", scope=tool0, path="file-a.txt", content=b"aaa")
    fuse_mg.merge(tool0, task)

    # tool-1 should see file A via the overlay
    tool1 = fuse_mg.fork(task, "tool-1", hints={"isolated": False})
    content_a = backend.read_file(task.name, "file-a.txt")
    assert content_a == b"aaa"

    # tool-1 writes file B
    fuse_mg.exec("filesystem", "write", scope=tool1, path="file-b.txt", content=b"bbb")
    fuse_mg.merge(tool1, task)

    # Merge task into ground and push
    fuse_mg.merge(task, fuse_mg.ground)
    fuse_mg.push()

    # Both files materialized
    assert (runtime_workspace / "file-a.txt").read_bytes() == b"aaa"
    assert (runtime_workspace / "file-b.txt").read_bytes() == b"bbb"
