"""Tests for Python-level substrate interception."""

from __future__ import annotations

import builtins
import os
import shutil
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest
from vcs_core._active_surface_profiles import read_only_filesystem_surface
from vcs_core._errors import UnscopedMutationError
from vcs_core._substrate_driver import SurfacePolicyError
from vcs_core._substrate_runtime import build_builtin_substrate_context
from vcs_core.types import ScopeInfo
from vcs_core.vcscore import VcsCore

from ..support.builders import make_store, make_vcscore


def _operation_id(entry) -> object:  # type: ignore[no-untyped-def]
    return entry.metadata["mg"]["operation"]["id"]


@contextmanager
def _active_mg(workspace: Path):
    store = make_store(workspace)
    from vcs_core.substrates import DeclarativeFilesystemSubstrate

    fs = DeclarativeFilesystemSubstrate(build_builtin_substrate_context(store))
    mg = make_vcscore(workspace, substrates=[fs], store=store, activate=True)
    try:
        yield mg
    finally:
        with suppress(RuntimeError):
            mg.deactivate()


def _file_effects(mg: VcsCore, scope: ScopeInfo) -> list[dict[str, object]]:
    return [entry.metadata for entry in mg.filter_effects(substrate="filesystem", ref=scope.ref, max_count=100)]


def _skip_without_dir_fd(func: object) -> None:
    if func not in os.supports_dir_fd:
        pytest.skip(f"{func!r} does not support dir_fd on this platform")


def test_open_write_is_recorded(workspace: Path) -> None:
    with _active_mg(workspace) as mg:
        task = mg.fork(mg.ground, "task-open-write")
        target = workspace / "patched.txt"

        with target.open("w", encoding="utf-8") as handle:
            handle.write("payload")

        effects = _file_effects(mg, task)
        assert any(effect.get("type") == "FileCreate" and effect.get("path") == "patched.txt" for effect in effects)
        assert target.read_text() == "payload"


def test_open_write_without_scope_raises(workspace: Path) -> None:
    with _active_mg(workspace):
        target = workspace / "unscoped.txt"

        with (
            pytest.raises(UnscopedMutationError, match=r"builtins\.open"),
            target.open("w", encoding="utf-8") as handle,
        ):
            handle.write("payload")

        assert not target.exists()


def test_open_write_read_only_active_surface_denies_before_original_fn(workspace: Path) -> None:
    with _active_mg(workspace) as mg:
        mg.fork(mg.ground, "task-read-only-open-write")
        target = workspace / "blocked.txt"

        with (
            pytest.raises(SurfacePolicyError, match="python-runtime:write"),
            mg._use_active_surface(read_only_filesystem_surface()),
            target.open("w", encoding="utf-8") as handle,
        ):
            handle.write("blocked")

        assert not target.exists()


def test_open_read_is_recorded(workspace: Path) -> None:
    target = workspace / "readme.txt"
    target.write_text("payload")

    with _active_mg(workspace) as mg:
        task = mg.fork(mg.ground, "task-open-read")

        with open(target, encoding="utf-8") as handle:
            assert handle.read() == "payload"

        effects = _file_effects(mg, task)
        assert any(effect.get("type") == "FileRead" and effect.get("path") == "readme.txt" for effect in effects)


def test_os_remove_is_recorded(workspace: Path) -> None:
    with _active_mg(workspace) as mg:
        task = mg.fork(mg.ground, "task-remove")
        target = workspace / "remove.txt"

        with target.open("w", encoding="utf-8") as handle:
            handle.write("payload")

        os.remove(target)

        effects = _file_effects(mg, task)
        assert any(effect.get("type") == "FileDelete" and effect.get("path") == "remove.txt" for effect in effects)


def test_os_remove_without_scope_raises(workspace: Path) -> None:
    target = workspace / "remove.txt"
    target.write_text("payload")

    with _active_mg(workspace):
        with pytest.raises(UnscopedMutationError, match=r"os\.remove"):
            os.remove(target)

        assert target.exists()


def test_os_unlink_dir_fd_outside_workspace_does_not_require_scope(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_without_dir_fd(os.unlink)
    outside = workspace.parent / f"{workspace.name}-outside-unlink"
    outside.mkdir()
    target = outside / "victim.txt"
    target.write_text("payload")
    monkeypatch.chdir(workspace)

    with _active_mg(workspace):
        fd = os.open(outside, os.O_RDONLY)
        try:
            os.unlink("victim.txt", dir_fd=fd)
        finally:
            os.close(fd)

    assert not target.exists()


def test_os_unlink_dir_fd_inside_workspace_without_scope_raises(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_without_dir_fd(os.unlink)
    nested = workspace / "nested"
    nested.mkdir()
    target = nested / "victim.txt"
    target.write_text("payload")
    outside_cwd = workspace.parent / f"{workspace.name}-outside-cwd"
    outside_cwd.mkdir()
    monkeypatch.chdir(outside_cwd)

    with _active_mg(workspace):
        fd = os.open(nested, os.O_RDONLY)
        try:
            with pytest.raises(UnscopedMutationError, match=r"os\.unlink"):
                os.unlink("victim.txt", dir_fd=fd)
        finally:
            os.close(fd)

    assert target.exists()


def test_os_unlink_dir_fd_inside_workspace_is_recorded(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _skip_without_dir_fd(os.unlink)
    nested = workspace / "nested"
    nested.mkdir()
    target = nested / "victim.txt"
    outside_cwd = workspace.parent / f"{workspace.name}-outside-cwd"
    outside_cwd.mkdir()
    monkeypatch.chdir(outside_cwd)

    with _active_mg(workspace) as mg:
        task = mg.fork(mg.ground, "task-unlink-dir-fd")
        target.write_text("payload")
        fd = os.open(nested, os.O_RDONLY)
        try:
            os.unlink("victim.txt", dir_fd=fd)
        finally:
            os.close(fd)

        effects = _file_effects(mg, task)
        assert any(
            effect.get("type") == "FileDelete" and effect.get("path") == "nested/victim.txt" for effect in effects
        )

    assert not target.exists()


def test_os_rename_dir_fd_inside_workspace_is_recorded(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _skip_without_dir_fd(os.rename)
    nested = workspace / "nested"
    nested.mkdir()
    source = nested / "source.txt"
    destination = nested / "destination.txt"
    source.write_text("payload")
    outside_cwd = workspace.parent / f"{workspace.name}-outside-cwd"
    outside_cwd.mkdir()
    monkeypatch.chdir(outside_cwd)

    with _active_mg(workspace) as mg:
        task = mg.fork(mg.ground, "task-rename-dir-fd")
        fd = os.open(nested, os.O_RDONLY)
        try:
            os.rename("source.txt", "destination.txt", src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            os.close(fd)

        effects = _file_effects(mg, task)
        assert any(
            effect.get("type") == "FileDelete" and effect.get("path") == "nested/source.txt" for effect in effects
        )
        assert any(
            effect.get("type") == "FileCreate" and effect.get("path") == "nested/destination.txt" for effect in effects
        )

    assert not source.exists()
    assert destination.read_text() == "payload"


def test_os_chmod_mode_only_change_is_recorded(workspace: Path) -> None:
    with _active_mg(workspace) as mg:
        task = mg.fork(mg.ground, "task-chmod")
        target = workspace / "script.sh"

        target.write_text("#!/bin/sh\necho payload\n")
        os.chmod(target, 0o755)

        effects = _file_effects(mg, task)
        assert any(effect.get("type") == "FilePatch" and effect.get("path") == "script.sh" for effect in effects)
        assert mg.store.workspace_file_mode(task.ref, "script.sh") == 0o100755


def test_shutil_copyfile_is_recorded(workspace: Path) -> None:
    source = workspace / "source.txt"
    source.write_text("payload")

    with _active_mg(workspace) as mg:
        task = mg.fork(mg.ground, "task-copy")
        destination = workspace / "copied.txt"

        shutil.copyfile(source, destination)

        effects = _file_effects(mg, task)
        assert any(effect.get("type") == "FileCreate" and effect.get("path") == "copied.txt" for effect in effects)
        assert destination.read_text() == "payload"


def test_shutil_copy2_records_executable_mode(workspace: Path) -> None:
    source = workspace / "source.sh"
    source.write_text("#!/bin/sh\necho payload\n")
    source.chmod(0o755)

    with _active_mg(workspace) as mg:
        task = mg.fork(mg.ground, "task-copy-mode")
        destination = workspace / "copied.sh"

        shutil.copy2(source, destination)

        assert destination.read_text() == "#!/bin/sh\necho payload\n"
        assert mg.store.workspace_file_mode(task.ref, "copied.sh") == 0o100755


def test_shutil_copyfile_to_outside_workspace_does_not_require_scope(workspace: Path) -> None:
    source = workspace / "source.txt"
    source.write_text("payload")
    outside = workspace.parent / f"{workspace.name}-outside"
    outside.mkdir()
    destination = outside / "copied.txt"

    with _active_mg(workspace):
        shutil.copyfile(source, destination)
        assert destination.read_text() == "payload"


def test_shutil_copyfile_to_outside_workspace_records_no_effects(workspace: Path) -> None:
    source = workspace / "source.txt"
    source.write_text("payload")
    outside = workspace.parent / f"{workspace.name}-outside"
    outside.mkdir()
    destination = outside / "copied.txt"

    with _active_mg(workspace) as mg:
        task = mg.fork(mg.ground, "task-copy-outside")
        shutil.copyfile(source, destination)
        assert destination.read_text() == "payload"
        effects = _file_effects(mg, task)
        assert not any(effect.get("type") in {"FileCreate", "FilePatch", "FileDelete"} for effect in effects)


def test_shutil_move_records_delete_and_write(workspace: Path) -> None:
    with _active_mg(workspace) as mg:
        task = mg.fork(mg.ground, "task-move")
        source = workspace / "move-me.txt"
        destination = workspace / "moved.txt"

        with source.open("w", encoding="utf-8") as handle:
            handle.write("payload")

        shutil.move(source, destination)

        effects = _file_effects(mg, task)
        assert any(effect.get("type") == "FileDelete" and effect.get("path") == "move-me.txt" for effect in effects)
        assert any(effect.get("type") == "FileCreate" and effect.get("path") == "moved.txt" for effect in effects)


def test_shutil_move_groups_effects_under_one_operation(workspace: Path) -> None:
    source = workspace / "move-me.txt"
    source.write_text("payload")

    with _active_mg(workspace) as mg:
        task = mg.fork(mg.ground, "task-move-op")
        destination = workspace / "moved.txt"

        shutil.move(source, destination)

        entries = mg.log(ref=task.ref, max_count=6)
        started = next(entry for entry in entries if entry.metadata.get("type") == "OperationStarted")
        completed = next(entry for entry in entries if entry.metadata.get("type") == "OperationCompleted")
        file_entries = [entry for entry in entries if entry.metadata.get("type") in {"FileCreate", "FileDelete"}]
        delete_entries = [entry for entry in file_entries if entry.metadata.get("type") == "FileDelete"]
        created_paths = {
            entry.metadata.get("path") for entry in file_entries if entry.metadata.get("type") == "FileCreate"
        }

        assert started.metadata["mg"]["operation"]["kind"] == "filesystem.move"
        assert len(delete_entries) == 1
        assert created_paths == {"move-me.txt", "moved.txt"}
        assert {_operation_id(entry) for entry in file_entries} == {_operation_id(started)}
        assert _operation_id(completed) == _operation_id(started)


def test_shutil_move_directory_records_per_file_delete_and_create(workspace: Path) -> None:
    source_dir = workspace / "dir"
    source_dir.mkdir()
    (source_dir / "a.txt").write_text("a")

    with _active_mg(workspace) as mg:
        task = mg.fork(mg.ground, "task-move-dir")
        destination = workspace / "moved"

        shutil.move(source_dir, destination)

        effects = _file_effects(mg, task)
        assert any(effect.get("type") == "FileDelete" and effect.get("path") == "dir/a.txt" for effect in effects)
        assert any(effect.get("type") == "FileCreate" and effect.get("path") == "moved/a.txt" for effect in effects)
        assert not source_dir.exists()
        assert (destination / "a.txt").read_text() == "a"


def test_shutil_rmtree_records_nested_deletes(workspace: Path) -> None:
    with _active_mg(workspace) as mg:
        task = mg.fork(mg.ground, "task-rmtree")
        nested = workspace / "tree"
        nested.mkdir()
        (nested / "a.txt").write_text("a")
        (nested / "b.txt").write_text("b")

        shutil.rmtree(nested)

        effects = _file_effects(mg, task)
        assert any(effect.get("type") == "FileDelete" and effect.get("path") == "tree/a.txt" for effect in effects)
        assert any(effect.get("type") == "FileDelete" and effect.get("path") == "tree/b.txt" for effect in effects)


def test_shutil_rmtree_outside_workspace_does_not_require_scope(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = workspace.parent / f"{workspace.name}-outside-rmtree"
    outside.mkdir()
    target = outside / "victim.txt"
    target.write_text("payload")
    monkeypatch.chdir(workspace)

    with _active_mg(workspace):
        shutil.rmtree(outside)

    assert not outside.exists()


def test_shutil_rmtree_inside_workspace_without_scope_raises(workspace: Path) -> None:
    nested = workspace / "tree"
    nested.mkdir()
    target = nested / "victim.txt"
    target.write_text("payload")

    with _active_mg(workspace), pytest.raises(UnscopedMutationError, match=r"shutil\.rmtree"):
        shutil.rmtree(nested)

    assert target.exists()


def test_shutil_rmtree_records_nested_deletes_for_preexisting_tree(workspace: Path) -> None:
    nested = workspace / "tree"
    nested.mkdir()
    (nested / "a.txt").write_text("a")
    (nested / "b.txt").write_text("b")

    with _active_mg(workspace) as mg:
        task = mg.fork(mg.ground, "task-rmtree-preexisting")

        shutil.rmtree(nested)

        effects = _file_effects(mg, task)
        assert any(effect.get("type") == "FileDelete" and effect.get("path") == "tree/a.txt" for effect in effects)
        assert any(effect.get("type") == "FileDelete" and effect.get("path") == "tree/b.txt" for effect in effects)
        assert not nested.exists()


def test_shutil_rmtree_groups_nested_deletes_under_one_operation(workspace: Path) -> None:
    nested = workspace / "tree"
    nested.mkdir()
    (nested / "a.txt").write_text("a")
    (nested / "b.txt").write_text("b")

    with _active_mg(workspace) as mg:
        task = mg.fork(mg.ground, "task-rmtree-op")

        shutil.rmtree(nested)

        entries = mg.log(ref=task.ref, max_count=6)
        started = next(entry for entry in entries if entry.metadata.get("type") == "OperationStarted")
        completed = next(entry for entry in entries if entry.metadata.get("type") == "OperationCompleted")
        file_entries = [entry for entry in entries if entry.metadata.get("type") in {"FileCreate", "FileDelete"}]
        delete_entries = [entry for entry in file_entries if entry.metadata.get("type") == "FileDelete"]

        assert started.metadata["mg"]["operation"]["kind"] == "filesystem.rmtree"
        assert len(delete_entries) == 2
        assert {_operation_id(entry) for entry in file_entries} == {_operation_id(started)}
        assert _operation_id(completed) == _operation_id(started)


def test_deactivate_uninstalls_python_patches(workspace: Path) -> None:
    with _active_mg(workspace) as mg:
        task = mg.fork(mg.ground, "task-deactivate")
        target = workspace / "before.txt"

        with target.open("w", encoding="utf-8") as handle:
            handle.write("one")

        before = len(_file_effects(mg, task))
        mg.discard(task)

    with target.open("w", encoding="utf-8") as handle:
        handle.write("two")

    with _active_mg(workspace) as mg2:
        task2 = mg2.fork(mg2.ground, "task-after")
        after = len(_file_effects(mg2, task2))

        assert before >= 1
        assert after == 0


@pytest.mark.xfail(
    reason=(
        "PR#4 (portable copy carrier + auto backend) collateral: the always-on carrier now writes "
        "snapshot bookkeeping under another workspace's .vcscore/runtime/snapshots/, which the "
        "active python-patch mutation monitor flags as an unscoped mutation across sessions. The "
        ".vcscore exclusion in _patch_paths.workspace_relative is per-active-workspace, so a foreign "
        "workspace's carrier internals are not covered. Reproduces in the public checkout at 25ebce0 "
        "(public CI does not run the vcs-core suite). Needs a focused fix (exclude carrier-internal "
        "runtime paths from cross-session monitoring); see the divergence ledger known-public-bugs."
    ),
    strict=True,
)
def test_python_patches_remain_active_for_other_workspace_sessions(workspace: Path, tmp_path: Path) -> None:
    original_open = builtins.open
    other_workspace = tmp_path / "other-workspace"
    other_workspace.mkdir()

    with _active_mg(workspace) as mg1, _active_mg(other_workspace) as mg2:
        task1 = mg1.fork(mg1.ground, "task-one")
        task2 = mg2.fork(mg2.ground, "task-two")

        with open(workspace / "one.txt", "w", encoding="utf-8") as handle:
            handle.write("one")
        with open(other_workspace / "two.txt", "w", encoding="utf-8") as handle:
            handle.write("two")

        assert len(_file_effects(mg1, task1)) == 1
        assert len(_file_effects(mg2, task2)) == 1

        mg1.deactivate()

        with open(other_workspace / "three.txt", "w", encoding="utf-8") as handle:
            handle.write("three")

        assert len(_file_effects(mg2, task2)) == 2

    assert builtins.open is original_open
