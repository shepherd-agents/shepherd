"""Substrate context and backend selection integration tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from vcs_core._substrate_runtime import (
    BuiltInSubstrateContext,
    build_builtin_substrate_context,
    default_runtime_binding,
)
from vcs_core.substrates import DeclarativeFilesystemSubstrate, FilesystemSubstrate, MarkerSubstrate
from vcs_core.types import FileState, ScopeInfo

from ...support.builders import make_marker_filesystem_substrates
from ...support.scopes import set_scope as _set_scope

if TYPE_CHECKING:
    from vcs_core.store import Store


def test_marker_substrate_accepts_builtin_context(store: Store, tmp_path: Path) -> None:
    ctx = BuiltInSubstrateContext(store=store, workspace=Path(str(tmp_path)), config={})
    marker = MarkerSubstrate(ctx)

    task = store.fork(store.GROUND_REF, "task-ctx-m")
    _set_scope(marker, task)
    oid = marker.mark("test-ctx")
    assert oid

    log = store.log(ref=task.ref)
    assert any(e.metadata.get("type") == "Marker" for e in log)


def test_declarative_filesystem_accepts_builtin_context(store: Store, tmp_path: Path) -> None:
    ctx = BuiltInSubstrateContext(store=store, workspace=Path(str(tmp_path)), config={"key": "value"})
    fs = DeclarativeFilesystemSubstrate(ctx)

    task = store.fork(store.GROUND_REF, "task-ctx-f")
    _set_scope(fs, task)
    oids = fs.record_changes([("ctx_file.py", b"content")])
    assert len(oids) == 1

    store.merge(task, store.GROUND_REF)
    diff = store.diff()
    assert any(f.path == "ctx_file.py" for f in diff.files)


def test_workspace_snapshot_renders_store_state_without_metadata_in_root(store: Store, tmp_path: Path) -> None:
    from vcs_core._workspace_snapshot import render_workspace_snapshot

    task = store.fork(store.GROUND_REF, "task-snapshot-render")
    store._emit_effect(
        task,
        "FileCreate",
        {"path": "README.md"},
        workspace_changes=[("README.md", b"hello\n"), ("bin/tool", b"#!/bin/sh\n", 0o100755)],
        substrate="filesystem",
    )
    store.merge(task, store.GROUND_REF)

    snapshots_root = tmp_path / "snapshots"
    first = render_workspace_snapshot(store, store.GROUND_REF, snapshots_root=snapshots_root)
    first_metadata = first.metadata_path.read_text()
    second = render_workspace_snapshot(store, store.GROUND_REF, snapshots_root=snapshots_root)

    assert second.root == first.root
    assert second.metadata_path.read_text() == first_metadata
    assert (second.root / "README.md").read_bytes() == b"hello\n"
    assert os.access(second.root / "bin" / "tool", os.X_OK)
    assert not (second.root / ".vcscore-checkout").exists()
    assert second.metadata_path.parent == second.root.parent
    assert not second.metadata_path.is_relative_to(second.root)
    metadata = json.loads(second.metadata_path.read_text())
    assert metadata["file_count"] == 2
    assert metadata["complete"] is True


def test_workspace_snapshot_fails_closed_on_corrupt_cache(store: Store, tmp_path: Path) -> None:
    from vcs_core._workspace_snapshot import render_workspace_snapshot

    task = store.fork(store.GROUND_REF, "task-snapshot-corrupt")
    store._emit_effect(
        task,
        "FileCreate",
        {"path": "README.md"},
        workspace_changes=[("README.md", b"hello\n")],
        substrate="filesystem",
    )
    store.merge(task, store.GROUND_REF)

    snapshots_root = tmp_path / "snapshots"
    first = render_workspace_snapshot(store, store.GROUND_REF, snapshots_root=snapshots_root)
    (first.root / "README.md").write_text("corrupt\n")

    with pytest.raises(RuntimeError, match="stale content"):
        render_workspace_snapshot(store, store.GROUND_REF, snapshots_root=snapshots_root)


def test_overlay_backend_resets_state_when_snapshot_base_changes(tmp_path: Path, monkeypatch) -> None:
    from vcs_core._fuse_overlay import FuseOverlayBackend
    from vcs_core._kernel_overlay import KernelOverlayBackend

    for backend_type in (FuseOverlayBackend, KernelOverlayBackend):
        state_root = tmp_path / backend_type.__name__
        workspace = tmp_path / f"{backend_type.__name__}-workspace"
        workspace.mkdir()
        monkeypatch.setattr(backend_type, "_ensure_supported", lambda self: None)
        monkeypatch.setattr(backend_type, "_is_mounted", lambda self, path: False)
        monkeypatch.setattr(
            backend_type,
            "_mount_overlay",
            lambda self, *, lowerdir, upperdir, workdir, merged: None,
        )

        first = backend_type(workspace=workspace, state_root=state_root, base_tree_oid="old")
        first.create_layer("ground", parent_scope_id=None)
        stale = first._layer_paths("ground").upper / "stale.txt"
        stale.write_text("stale\n")
        assert first.diff_layer("ground") == [("stale.txt", b"stale\n", 0o100644)]

        second = backend_type(workspace=workspace, state_root=state_root, base_tree_oid="new")
        second.create_layer("ground", parent_scope_id=None)

        assert second.diff_layer("ground") == []
        assert (state_root / "base-tree-oid").read_text() == "new"


def test_builtin_substrates_reject_raw_store_construction(store: Store) -> None:
    with pytest.raises(TypeError, match="BuiltInSubstrateContext"):
        MarkerSubstrate(store)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="BuiltInSubstrateContext"):
        FilesystemSubstrate(store)  # type: ignore[arg-type]


def test_builder_constructs_runtime_overlay_pair_with_explicit_workspace_and_config(
    store: Store,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from vcs_core import _kernel_overlay

    created: list[tuple[Path, Path, Path | None]] = []

    class FakeKernelOverlayBackend:
        def __init__(
            self,
            workspace: Path,
            state_root: Path,
            *,
            base_lowerdir: Path | None = None,
            base_tree_oid: str | None = None,
        ) -> None:
            del base_tree_oid
            created.append((workspace, state_root, base_lowerdir))

        def create_layer(self, scope_id: str, *, parent_scope_id: str | None) -> None:
            del scope_id, parent_scope_id

        def has_layer(self, scope_id: str) -> bool:
            del scope_id
            return False

        def read_file(self, scope_id: str, path: str) -> bytes:
            del scope_id, path
            return b""

        def read_file_state(self, scope_id: str, path: str) -> FileState:
            del scope_id, path
            return FileState(b"")

        def write_file(self, scope_id: str, path: str, content: bytes, *, mode: int = 0o100644) -> None:
            del scope_id, path, content, mode

        def delete_file(self, scope_id: str, path: str) -> None:
            del scope_id, path

        def diff_layer(self, scope_id: str) -> list[tuple[str, bytes | None, int]]:
            del scope_id
            return []

        def commit_layer(self, scope_id: str, *, into_scope_id: str | None) -> None:
            del scope_id, into_scope_id

        def discard_layer(self, scope_id: str) -> None:
            del scope_id

        def push_layer(self, scope_id: str | None = None) -> None:
            del scope_id

        def working_path(self, scope_id: str) -> Path:
            return Path("/virtual") / scope_id

        def deactivate(self) -> None:
            pass

    monkeypatch.setattr(_kernel_overlay, "KernelOverlayBackend", FakeKernelOverlayBackend)

    workspace = Path(str(tmp_path))
    marker, filesystem = make_marker_filesystem_substrates(
        store,
        declarative=False,
        workspace=workspace,
        config={"backend": "kernel", "state_root": "/var/tmp/vcs-core-overlay-state"},
    )
    filesystem.activate()

    assert created == [(workspace, Path("/var/tmp/vcs-core-overlay-state"), created[0][2])]
    assert created[0][2] is not None
    assert created[0][2] != workspace
    assert filesystem._workspace == workspace

    task = store.fork(store.GROUND_REF, "task-ctx-builder")
    _set_scope(marker, task)
    assert marker.mark("builder-runtime-context")


def test_filesystem_kernel_backend_can_be_opted_in_via_context(store: Store, tmp_path: Path, monkeypatch) -> None:
    from vcs_core import _kernel_overlay

    created: list[tuple[Path, Path, Path | None]] = []

    class FakeKernelOverlayBackend:
        def __init__(
            self,
            workspace: Path,
            state_root: Path,
            *,
            base_lowerdir: Path | None = None,
            base_tree_oid: str | None = None,
        ) -> None:
            del base_tree_oid
            created.append((workspace, state_root, base_lowerdir))

        def create_layer(self, scope_id: str, *, parent_scope_id: str | None) -> None:
            del scope_id, parent_scope_id

        def has_layer(self, scope_id: str) -> bool:
            del scope_id
            return False

        def read_file(self, scope_id: str, path: str) -> bytes:
            del scope_id, path
            return b""

        def read_file_state(self, scope_id: str, path: str) -> FileState:
            del scope_id, path
            return FileState(b"")

        def write_file(self, scope_id: str, path: str, content: bytes, *, mode: int = 0o100644) -> None:
            del scope_id, path, content, mode

        def delete_file(self, scope_id: str, path: str) -> None:
            del scope_id, path

        def diff_layer(self, scope_id: str) -> list[tuple[str, bytes | None, int]]:
            del scope_id
            return []

        def commit_layer(self, scope_id: str, *, into_scope_id: str | None) -> None:
            del scope_id, into_scope_id

        def discard_layer(self, scope_id: str) -> None:
            del scope_id

        def push_layer(self, scope_id: str | None = None) -> None:
            del scope_id

        def working_path(self, scope_id: str) -> Path:
            return Path("/virtual") / scope_id

        def deactivate(self) -> None:
            pass

    monkeypatch.setattr(_kernel_overlay, "KernelOverlayBackend", FakeKernelOverlayBackend)

    workspace = Path(str(tmp_path))
    ctx = BuiltInSubstrateContext(store=store, workspace=workspace, config={"backend": "kernel"})
    fs = FilesystemSubstrate(ctx)
    fs.activate()

    assert created[0][0] == workspace
    assert created[0][1].parent.name == "vcs-core-overlay"
    assert created[0][1].name.startswith(f"{workspace.name}-")
    assert workspace not in created[0][1].parents
    assert created[0][2] is not None
    assert created[0][2] != workspace
    assert fs._backend is not None

    fs.deactivate()
    fs.activate()

    assert len(created) == 2
    assert created[1][0] == workspace
    assert created[1][2] is not None


def test_filesystem_kernel_backend_uses_configured_state_root(store: Store, tmp_path: Path, monkeypatch) -> None:
    from vcs_core import _kernel_overlay

    created: list[tuple[Path, Path, Path | None]] = []

    class FakeKernelOverlayBackend:
        def __init__(
            self,
            workspace: Path,
            state_root: Path,
            *,
            base_lowerdir: Path | None = None,
            base_tree_oid: str | None = None,
        ) -> None:
            del base_tree_oid
            created.append((workspace, state_root, base_lowerdir))

        def create_layer(self, scope_id: str, *, parent_scope_id: str | None) -> None:
            del scope_id, parent_scope_id

        def has_layer(self, scope_id: str) -> bool:
            del scope_id
            return False

        def read_file(self, scope_id: str, path: str) -> bytes:
            del scope_id, path
            return b""

        def read_file_state(self, scope_id: str, path: str) -> FileState:
            del scope_id, path
            return FileState(b"")

        def write_file(self, scope_id: str, path: str, content: bytes, *, mode: int = 0o100644) -> None:
            del scope_id, path, content, mode

        def delete_file(self, scope_id: str, path: str) -> None:
            del scope_id, path

        def diff_layer(self, scope_id: str) -> list[tuple[str, bytes | None, int]]:
            del scope_id
            return []

        def commit_layer(self, scope_id: str, *, into_scope_id: str | None) -> None:
            del scope_id, into_scope_id

        def discard_layer(self, scope_id: str) -> None:
            del scope_id

        def push_layer(self, scope_id: str | None = None) -> None:
            del scope_id

        def working_path(self, scope_id: str) -> Path:
            return Path("/virtual") / scope_id

        def deactivate(self) -> None:
            pass

    monkeypatch.setattr(_kernel_overlay, "KernelOverlayBackend", FakeKernelOverlayBackend)

    ctx = BuiltInSubstrateContext(
        store=store,
        workspace=Path(str(tmp_path)),
        config={"backend": "kernel", "state_root": "/var/tmp/vcs-core-overlay-state"},
    )
    fs = FilesystemSubstrate(ctx)
    fs.activate()

    assert created == [(Path(str(tmp_path)), Path("/var/tmp/vcs-core-overlay-state"), created[0][2])]
    assert created[0][2] is not None


def test_filesystem_fuse_backend_uses_configured_binaries_and_state_root(
    store: Store, tmp_path: Path, monkeypatch
) -> None:
    from vcs_core import _fuse_overlay

    created: list[tuple[Path, Path, Path | None, str, str]] = []

    class FakeFuseOverlayBackend:
        def __init__(
            self,
            workspace: Path,
            state_root: Path,
            *,
            base_lowerdir: Path | None = None,
            base_tree_oid: str | None = None,
            fuse_overlayfs_bin: str = "fuse-overlayfs",
            fusermount_bin: str = "fusermount3",
        ) -> None:
            del base_tree_oid
            created.append((workspace, state_root, base_lowerdir, fuse_overlayfs_bin, fusermount_bin))

        def create_layer(self, scope_id: str, *, parent_scope_id: str | None) -> None:
            del scope_id, parent_scope_id

        def has_layer(self, scope_id: str) -> bool:
            del scope_id
            return False

        def read_file(self, scope_id: str, path: str) -> bytes:
            del scope_id, path
            return b""

        def read_file_state(self, scope_id: str, path: str) -> FileState:
            del scope_id, path
            return FileState(b"")

        def write_file(self, scope_id: str, path: str, content: bytes, *, mode: int = 0o100644) -> None:
            del scope_id, path, content, mode

        def delete_file(self, scope_id: str, path: str) -> None:
            del scope_id, path

        def diff_layer(self, scope_id: str) -> list[tuple[str, bytes | None, int]]:
            del scope_id
            return []

        def commit_layer(self, scope_id: str, *, into_scope_id: str | None) -> None:
            del scope_id, into_scope_id

        def discard_layer(self, scope_id: str) -> None:
            del scope_id

        def push_layer(self, scope_id: str | None = None) -> None:
            del scope_id

        def working_path(self, scope_id: str) -> Path:
            return Path("/virtual") / scope_id

        def deactivate(self) -> None:
            pass

    monkeypatch.setattr(_fuse_overlay, "FuseOverlayBackend", FakeFuseOverlayBackend)

    ctx = BuiltInSubstrateContext(
        store=store,
        workspace=Path(str(tmp_path)),
        config={
            "backend": "fuse",
            "state_root": "/var/tmp/vcs-core-fuse-state",
            "fuse_overlayfs_bin": "/usr/local/bin/fuse-overlayfs",
            "fusermount_bin": "/usr/local/bin/fusermount3",
        },
    )
    fs = FilesystemSubstrate(ctx)
    fs.activate()

    assert created == [
        (
            Path(str(tmp_path)),
            Path("/var/tmp/vcs-core-fuse-state"),
            created[0][2],
            "/usr/local/bin/fuse-overlayfs",
            "/usr/local/bin/fusermount3",
        )
    ]
    assert created[0][2] is not None


def test_substrate_context_config_is_accessible(tmp_path: Path) -> None:
    from vcs_core.types import SubstrateContext

    ctx = SubstrateContext(
        workspace=Path(str(tmp_path)),
        config={"api_key": "resolved-secret", "timeout": 30},
    )
    assert ctx.config["api_key"] == "resolved-secret"
    assert ctx.config["timeout"] == 30
    assert ctx.workspace == Path(str(tmp_path))


def test_auto_detect_backend_returns_none_on_non_linux(monkeypatch) -> None:
    from vcs_core.substrates import detect_overlay_backend

    # The native overlay probe stays Linux-only (None off-Linux); the substrate
    # resolver then floors to the macOS APFS clonefile carrier on darwin.
    monkeypatch.setattr("vcs_core.substrates.sys.platform", "darwin")
    assert detect_overlay_backend() is None
    fs = FilesystemSubstrate.__new__(FilesystemSubstrate)
    assert fs._auto_detect_backend_name() == "clonefile"


def test_auto_detect_backend_returns_none_on_unsupported_platform(monkeypatch) -> None:
    from vcs_core.substrates import detect_overlay_backend

    monkeypatch.setattr("vcs_core.substrates.sys.platform", "win32")
    assert detect_overlay_backend() is None


@pytest.mark.skipif(sys.platform != "darwin", reason="clonefile carrier is macOS (APFS)")
def test_filesystem_clonefile_backend_builds_and_activates_on_macos(store: Store, tmp_path: Path) -> None:
    from vcs_core._clonefile_carrier import ClonefileCarrierBackend

    workspace = tmp_path / "ws"
    workspace.mkdir()
    _marker, filesystem = make_marker_filesystem_substrates(
        store,
        declarative=False,
        workspace=workspace,
        config={"backend": "clonefile", "state_root": str(tmp_path / "state")},
    )
    filesystem.activate()
    assert isinstance(filesystem._backend, ClonefileCarrierBackend)
    assert filesystem._backend.has_layer("ground")
    assert filesystem.overlay_mount_path("ground").is_dir()  # the ground clone (working tree)


def test_auto_detect_backend_returns_kernel_when_available(monkeypatch, tmp_path: Path) -> None:
    import vcs_core.substrates as mod

    monkeypatch.setattr(mod, "sys", type("MockSys", (), {"platform": "linux"})())
    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    monkeypatch.setattr(mod, "_has_cap_sys_admin", lambda: True)

    fake_proc = tmp_path / "proc_filesystems"
    fake_proc.write_text("nodev\toverlay\n")
    monkeypatch.setattr(mod, "Path", lambda p: fake_proc if p == "/proc/filesystems" else Path(p))
    monkeypatch.setattr(mod.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

    assert mod.detect_overlay_backend() == "kernel"


def test_auto_detect_falls_through_without_cap_sys_admin(monkeypatch, tmp_path: Path) -> None:
    import vcs_core.substrates as mod

    monkeypatch.setattr(mod, "sys", type("MockSys", (), {"platform": "linux"})())
    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    monkeypatch.setattr(mod, "_has_cap_sys_admin", lambda: False)

    fake_proc = tmp_path / "proc_filesystems"
    fake_proc.write_text("nodev\toverlay\n")
    monkeypatch.setattr(mod, "Path", lambda p: fake_proc if p == "/proc/filesystems" else Path(p))
    monkeypatch.setattr(mod.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

    monkeypatch.setattr(
        mod,
        "Path",
        lambda p: (
            fake_proc
            if p == "/proc/filesystems"
            else type("FakePath", (), {"exists": lambda self: False})()
            if p == "/dev/fuse"
            else Path(p)
        ),
    )
    assert mod.detect_overlay_backend() is None


def test_reconciled_mode_only_change_produces_effect(tmp_path: Path) -> None:
    from vcs_core.store import Store

    store = Store(str(tmp_path / ".vcscore"))
    store.create_root_commit()

    task = store.fork(Store.GROUND_REF, "task-setup")
    store._emit_effect(
        task,
        "FileCreate",
        {"path": "script.sh"},
        workspace_changes=[("script.sh", b"#!/bin/sh\necho hi")],
        substrate="filesystem",
    )
    store.merge(task, Store.GROUND_REF)

    fs = FilesystemSubstrate(build_builtin_substrate_context(store, workspace=tmp_path))
    fs.bind_runtime(
        default_runtime_binding(
            __import__("vcs_core.recording", fromlist=["RecordingPipeline"]).RecordingPipeline(store),
            workspace=tmp_path,
        )
    )

    scope = ScopeInfo(name="ground", ref=Store.GROUND_REF, instance_id="ground", creation_oid="")

    effect = fs._reconciled_effect_for_change(
        scope,
        "script.sh",
        b"#!/bin/sh\necho hi",
        mode=0o100755,
    )
    assert effect is not None
    assert effect.effect_type == "FilePatch"
    assert len(effect.workspace_changes) == 1
    assert effect.workspace_changes[0][2] == 0o100755  # type: ignore[misc]

    no_effect = fs._reconciled_effect_for_change(
        scope,
        "script.sh",
        b"#!/bin/sh\necho hi",
        mode=0o100644,
    )
    assert no_effect is None

    no_effect2 = fs._reconciled_effect_for_change(
        scope,
        "script.sh",
        b"#!/bin/sh\necho hi",
        mode=None,
    )
    assert no_effect2 is None
