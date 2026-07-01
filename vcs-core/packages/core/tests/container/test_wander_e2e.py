"""End-to-end wander workflow tests.

Validates the full loop: auto-detect backend -> fork -> write in overlay
mount (simulating bash) -> merge -> graph -> push -> checkout.

Requires Linux with root privileges and kernel overlayfs support.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from vcs_core._graph import render_graph
from vcs_core._substrate_runtime import build_builtin_substrate_context
from vcs_core.store import Store
from vcs_core.substrates import FilesystemSubstrate, MarkerSubstrate
from vcs_core.vcscore import VcsCore

pytestmark = [
    pytest.mark.container,
    pytest.mark.skipif(
        sys.platform != "linux" or os.geteuid() != 0,
        reason="Wander e2e requires Linux with root.",
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
def overlay_state_root(tmp_path: Path) -> Path:
    _ensure_overlay_available()
    configured = os.environ.get("VCS_CORE_KERNEL_OVERLAY_STATE_ROOT")
    if configured:
        root = Path(configured) / f"wander-e2e-{uuid.uuid4().hex[:8]}"
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
def wander_mg(runtime_workspace: Path, overlay_state_root: Path) -> VcsCore:
    """VcsCore with auto-detected overlay backend (no explicit 'backend' config key)."""
    repo_path = runtime_workspace / ".vcscore"
    store = Store(str(repo_path))
    context = build_builtin_substrate_context(
        store,
        workspace=runtime_workspace,
        config={"state_root": str(overlay_state_root)},
    )
    marker = MarkerSubstrate(context)
    # No "backend" key -- auto-detection under test
    filesystem = FilesystemSubstrate(context)
    mg = VcsCore(str(runtime_workspace), substrates=[marker, filesystem], store=store)
    try:
        mg.activate()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"Overlay runtime not available in this environment: {exc}")
    if filesystem._backend is None:
        pytest.skip("No overlay backend detected — store-only mode")
    yield mg
    mg.deactivate()


def _filesystem_sub(mg: VcsCore) -> FilesystemSubstrate:
    return next(sub for sub in mg.lifecycle_substrates if getattr(sub, "name", None) == "filesystem")


def _backend(mg: VcsCore):
    return _filesystem_sub(mg)._backend


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_auto_detect_selects_backend(wander_mg: VcsCore) -> None:
    """Auto-detection should pick a backend on a capable Linux system."""
    backend = _backend(wander_mg)
    assert backend is not None, "Auto-detection failed to select an overlay backend"


def test_bash_writes_captured_at_merge(wander_mg: VcsCore, runtime_workspace: Path) -> None:
    """Files written directly in the overlay mount (simulating bash) appear as effects after merge."""
    task = wander_mg.fork(wander_mg.ground, "bash-task", hints={"isolated": True})
    backend = _backend(wander_mg)

    # Simulate bash: write directly into the overlay mount
    mount = backend.working_path(task.name)
    (mount / "hello.txt").write_text("hello from bash")
    (mount / "subdir").mkdir(exist_ok=True)
    (mount / "subdir" / "nested.py").write_text("print('nested')")

    # File should NOT be in the real workspace yet
    assert not (runtime_workspace / "hello.txt").exists()

    # Merge captures overlay diff as per-file effects
    wander_mg.merge(task, wander_mg.ground)

    # Check effects in the Store
    effects = wander_mg.store.filter_effects(effect_type="FileCreate", max_count=10)
    paths = {e.metadata.get("path") for e in effects}
    assert "hello.txt" in paths
    assert "subdir/nested.py" in paths


def test_bash_modify_produces_file_patch(wander_mg: VcsCore) -> None:
    """Modifying an existing file in the overlay produces a FilePatch effect."""
    backend = _backend(wander_mg)

    # Create a file via v1 and merge into ground
    v1 = wander_mg.fork(wander_mg.ground, "patch-v1", hints={"isolated": True})
    mount = backend.working_path(v1.name)
    (mount / "existing.txt").write_text("original content")
    wander_mg.merge(v1, wander_mg.ground)

    assert wander_mg.store.read_workspace_file(Store.GROUND_REF, "existing.txt") == b"original content"

    # Modify the file in a new scope
    v2 = wander_mg.fork(wander_mg.ground, "patch-v2", hints={"isolated": True})
    mount2 = backend.working_path(v2.name)
    (mount2 / "existing.txt").write_text("modified content")
    wander_mg.merge(v2, wander_mg.ground)

    effects = wander_mg.store.filter_effects(effect_type="FilePatch", max_count=10)
    paths = {e.metadata.get("path") for e in effects}
    assert "existing.txt" in paths
    assert wander_mg.store.read_workspace_file(Store.GROUND_REF, "existing.txt") == b"modified content"


def test_bash_delete_produces_file_delete(wander_mg: VcsCore) -> None:
    """Deleting a file in the overlay produces a FileDelete effect."""
    backend = _backend(wander_mg)

    # Create a file and merge
    v1 = wander_mg.fork(wander_mg.ground, "del-v1", hints={"isolated": True})
    mount = backend.working_path(v1.name)
    (mount / "doomed.txt").write_text("will be deleted")
    wander_mg.merge(v1, wander_mg.ground)

    assert wander_mg.store.read_workspace_file(Store.GROUND_REF, "doomed.txt") == b"will be deleted"

    # Delete the file in a new scope
    v2 = wander_mg.fork(wander_mg.ground, "del-v2", hints={"isolated": True})
    mount2 = backend.working_path(v2.name)
    (mount2 / "doomed.txt").unlink()
    wander_mg.merge(v2, wander_mg.ground)

    effects = wander_mg.store.filter_effects(effect_type="FileDelete", max_count=10)
    paths = {e.metadata.get("path") for e in effects}
    assert "doomed.txt" in paths
    assert wander_mg.store.read_workspace_file(Store.GROUND_REF, "doomed.txt") is None


def test_fork_discard_removes_changes(wander_mg: VcsCore, runtime_workspace: Path) -> None:
    """Discarding a scope removes its overlay layer and leaves no trace."""
    task = wander_mg.fork(wander_mg.ground, "discard-task", hints={"isolated": True})
    backend = _backend(wander_mg)

    mount = backend.working_path(task.name)
    (mount / "ephemeral.txt").write_text("gone soon")

    assert backend.has_layer(task.name)
    wander_mg.discard(task)
    assert not backend.has_layer(task.name)
    assert not (runtime_workspace / "ephemeral.txt").exists()


def test_graph_shows_scope_structure(wander_mg: VcsCore) -> None:
    """The ASCII graph includes scope names, merge effects, and per-file effects."""
    backend = _backend(wander_mg)

    # Fork, write, merge — should appear in the graph with scope column
    feature = wander_mg.fork(wander_mg.ground, "feature", hints={"isolated": True})
    mount = backend.working_path(feature.name)
    (mount / "feature.py").write_text("# feature")
    (mount / "util.py").write_text("# util")
    wander_mg.merge(feature, wander_mg.ground)

    entries = wander_mg.store.log(max_count=30)
    lines = render_graph(entries)
    text = "\n".join(lines)

    # Merged scope produces per-file effects and a ScopeMerge structural effect
    assert "ScopeMerge" in text
    assert "feature" in text
    assert "FileCreate" in text
    # Graph has branch structure (the * and | characters show columns)
    assert "|" in text


def test_checkout_historical_state(wander_mg: VcsCore, tmp_path: Path) -> None:
    """Time-travel: extract v1 workspace by OID after v2 has been merged."""
    backend = _backend(wander_mg)

    # First scope: write v1
    v1 = wander_mg.fork(wander_mg.ground, "v1", hints={"isolated": True})
    mount = backend.working_path(v1.name)
    (mount / "data.txt").write_text("version 1")
    wander_mg.merge(v1, wander_mg.ground)

    # Capture ground tip OID after v1 merge (this is the v1 state)
    v1_oid = wander_mg.store.log(ref=Store.GROUND_REF, max_count=1)[0].oid

    # Second scope: overwrite with v2 and add a new file
    v2 = wander_mg.fork(wander_mg.ground, "v2", hints={"isolated": True})
    mount2 = backend.working_path(v2.name)
    (mount2 / "data.txt").write_text("version 2")
    (mount2 / "v2-only.txt").write_text("only in v2")
    wander_mg.merge(v2, wander_mg.ground)
    v2_oid = wander_mg.store.log(ref=Store.GROUND_REF, max_count=1)[0].oid

    # Current ground has v2
    assert wander_mg.store.read_workspace_file(Store.GROUND_REF, "data.txt") == b"version 2"

    # Time-travel: extract v1 state by OID
    dest = str(tmp_path / "snapshot")
    count = wander_mg.store.checkout_workspace_tree(v1_oid, dest)
    assert count >= 1
    assert (Path(dest) / "data.txt").read_text() == "version 1"
    assert not (Path(dest) / "v2-only.txt").exists()

    # Re-extract v2 to the SAME dest — stale files from v1 must not linger
    count2 = wander_mg.store.checkout_workspace_tree(v2_oid, dest)
    assert count2 >= 2
    assert (Path(dest) / "data.txt").read_text() == "version 2"
    assert (Path(dest) / "v2-only.txt").read_text() == "only in v2"


def test_full_wander_loop(wander_mg: VcsCore, runtime_workspace: Path, tmp_path: Path) -> None:
    """Full wander workflow: fork -> write in overlay -> merge -> log -> push -> verify."""
    backend = _backend(wander_mg)

    # Fork an isolated scope
    task = wander_mg.fork(wander_mg.ground, "wander-task", hints={"isolated": True})

    # Write directly in overlay (simulating bash)
    mount = backend.working_path(task.name)
    (mount / "main.py").write_text("print('hello world')")
    (mount / "config.toml").write_text("[settings]\ndebug = true\n")

    # Not in real workspace yet
    assert not (runtime_workspace / "main.py").exists()
    assert not (runtime_workspace / "config.toml").exists()

    # Merge captures the changes
    wander_mg.merge(task, wander_mg.ground)

    # Graph shows the effects
    entries = wander_mg.store.log(max_count=20)
    lines = render_graph(entries)
    text = "\n".join(lines)
    assert "FileCreate" in text
    assert "wander-task" in text

    # Still not in real workspace (not yet pushed)
    assert not (runtime_workspace / "main.py").exists()

    # Push materializes
    wander_mg.push()

    # Now files are in the real workspace
    assert (runtime_workspace / "main.py").read_text() == "print('hello world')"
    assert (runtime_workspace / "config.toml").read_text() == "[settings]\ndebug = true\n"
    assert wander_mg.status().commits_ahead == 0

    # Checkout ground state to a directory
    dest = str(tmp_path / "snapshot")
    count = wander_mg.store.checkout_workspace_tree(Store.GROUND_REF, dest)
    assert count >= 2
    assert (Path(dest) / "main.py").read_text() == "print('hello world')"
    assert (Path(dest) / "config.toml").read_text() == "[settings]\ndebug = true\n"
