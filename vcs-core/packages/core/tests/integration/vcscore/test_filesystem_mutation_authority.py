"""Pre-side-effect filesystem mutation authority tests."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
from vcs_core._errors import SiblingGroupRecoveryRequiredError
from vcs_core._sibling_groups import (
    CarrierLeaseRecord,
    SiblingGroupRecord,
    SiblingHandleRecord,
    sibling_machine_scope_name,
)
from vcs_core.store import Store
from vcs_core.types import ScopeInfo
from vcs_core.vcscore import VcsCore

GROUP_ID = "sg-aaaa00000000"


def _parent_oid(store: Store) -> str:
    return store.log(ref=Store.GROUND_REF, max_count=1)[0].oid


def _sibling(store: Store, *, group_id: str, ordinal: int) -> SiblingHandleRecord:
    machine_scope_name = sibling_machine_scope_name(group_id, ordinal)
    return SiblingHandleRecord(
        world_id=f"{group_id}-world-{ordinal}",
        machine_scope_name=machine_scope_name,
        display_label=f"attempt-{ordinal}",
        scope_ref=f"refs/vcscore/scopes/{machine_scope_name}",
        parent_ref=Store.GROUND_REF,
        creation_oid=_parent_oid(store),
        state="admitted",
        instance_id=f"inst-{ordinal}",
    )


def _group_record(store: Store, *, group_id: str = GROUP_ID) -> SiblingGroupRecord:
    siblings = (_sibling(store, group_id=group_id, ordinal=0), _sibling(store, group_id=group_id, ordinal=1))
    return SiblingGroupRecord(
        group_id=group_id,
        parent_ref=Store.GROUND_REF,
        parent_world_id="ground-world",
        admitted_parent_oid=_parent_oid(store),
        status="running",
        siblings=siblings,
        leases=(
            CarrierLeaseRecord(
                lease_id=f"{group_id}-lease-0",
                world_id=siblings[0].world_id,
                substrate="filesystem",
                target_id="workspace",
                mode="writable_carrier",
                resource_key="workspace",
                state="planned",
                carrier_ref=siblings[0].scope_ref,
            ),
        ),
        created_at=1.0,
        updated_at=2.0,
    )


def _publish_blocker(mg: VcsCore) -> None:
    assert mg.store._publish_sibling_group_for_recovery_test(_group_record(mg.store), expected_head_oid=None)


def _write_text(mg: VcsCore, path: Path, content: str) -> None:
    with mg._patch_manager.guard():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def _read_text(mg: VcsCore, path: Path) -> str:
    with mg._patch_manager.guard():
        return path.read_text()


def _blocked_scope(mg: VcsCore) -> ScopeInfo:
    task = mg.fork(mg.ground, "task-filesystem-authority")
    _publish_blocker(mg)
    return task


def _assert_blocked_before_side_effect(mg: VcsCore, operation: Callable[[], object]) -> None:
    task = _blocked_scope(mg)
    with pytest.raises(SiblingGroupRecoveryRequiredError, match=GROUP_ID), mg._lock, mg._scoped(task):
        operation()


def test_writable_open_blocks_before_truncating(mg: VcsCore, workspace: Path) -> None:
    target = workspace / "tracked.txt"
    _write_text(mg, target, "before")

    def operation() -> None:
        with open(target, "w") as handle:
            handle.write("after")

    _assert_blocked_before_side_effect(mg, operation)

    assert _read_text(mg, target) == "before"


def test_path_write_text_blocks_before_truncating(mg: VcsCore, workspace: Path) -> None:
    target = workspace / "tracked.txt"
    _write_text(mg, target, "before")

    _assert_blocked_before_side_effect(mg, lambda: target.write_text("after"))

    assert _read_text(mg, target) == "before"


@pytest.mark.parametrize("remove_name", ["remove", "unlink"], ids=["os.remove", "os.unlink"])
def test_remove_and_unlink_block_before_deleting(mg: VcsCore, workspace: Path, remove_name: str) -> None:
    target = workspace / "tracked.txt"
    _write_text(mg, target, "before")

    _assert_blocked_before_side_effect(mg, lambda: getattr(os, remove_name)(target))

    assert _read_text(mg, target) == "before"


@pytest.mark.parametrize("chmod_name", ["os.chmod", "Path.chmod"])
def test_chmod_blocks_before_mode_change(mg: VcsCore, workspace: Path, chmod_name: str) -> None:
    target = workspace / "tracked.txt"
    _write_text(mg, target, "before")
    with mg._patch_manager.guard():
        target.chmod(0o644)

    if chmod_name == "os.chmod":
        os_chmod = getattr(os, chmod_name.removeprefix("os."))
        _assert_blocked_before_side_effect(mg, lambda: os_chmod(target, 0o600))
    else:
        _assert_blocked_before_side_effect(mg, lambda: target.chmod(0o600))

    assert stat.S_IMODE(target.stat().st_mode) == 0o644


@pytest.mark.parametrize("rename_name", ["rename", "replace"], ids=["os.rename", "os.replace"])
def test_rename_and_replace_block_before_moving(
    mg: VcsCore,
    workspace: Path,
    rename_name: str,
) -> None:
    source = workspace / "source.txt"
    destination = workspace / "destination.txt"
    _write_text(mg, source, "source")
    _write_text(mg, destination, "destination")

    _assert_blocked_before_side_effect(mg, lambda: getattr(os, rename_name)(source, destination))

    assert _read_text(mg, source) == "source"
    assert _read_text(mg, destination) == "destination"


@pytest.mark.parametrize("copy_name", ["copyfile", "copy2"], ids=["copyfile", "copy2"])
def test_copy_blocks_before_creating_destination(
    mg: VcsCore,
    workspace: Path,
    copy_name: str,
) -> None:
    source = workspace / "source.txt"
    destination = workspace / "destination.txt"
    _write_text(mg, source, "source")

    _assert_blocked_before_side_effect(mg, lambda: getattr(shutil, copy_name)(source, destination))

    assert _read_text(mg, source) == "source"
    assert not destination.exists()


def test_move_blocks_before_moving(mg: VcsCore, workspace: Path) -> None:
    source = workspace / "source.txt"
    destination = workspace / "destination.txt"
    _write_text(mg, source, "source")

    _assert_blocked_before_side_effect(mg, lambda: shutil.move(source, destination))

    assert _read_text(mg, source) == "source"
    assert not destination.exists()


def test_rmtree_blocks_before_deleting_tree(mg: VcsCore, workspace: Path) -> None:
    target = workspace / "tree"
    child = target / "child.txt"
    _write_text(mg, child, "child")

    _assert_blocked_before_side_effect(mg, lambda: shutil.rmtree(target))

    assert _read_text(mg, child) == "child"
