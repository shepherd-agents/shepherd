"""Retained-output custody smoke tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from vcs_core import InvalidRepositoryStateError, Store, VcsCore, build_builtin_substrate_context
from vcs_core._seal_handoff import read_seal_handoff
from vcs_core._substrate_tree_read import read_substrate_workspace_file
from vcs_core.runtime_substrate import TaskTraceSubstrateDriver
from vcs_core.substrates import FilesystemSubstrate, MarkerSubstrate
from vcs_core.types import RetainedOutputIdentity, RetainedOutputQueryResult, ScopeInfo

from ...support.overlays import MockOverlayBackend


def _make_mg(root: Path) -> VcsCore:
    root.mkdir(parents=True, exist_ok=True)
    store = Store(str(root / ".vcscore"))
    context = build_builtin_substrate_context(store, workspace=root, config={})
    vcscore = VcsCore(
        str(root),
        substrates=[
            MarkerSubstrate(context),
            FilesystemSubstrate(context, backend=MockOverlayBackend()),
            TaskTraceSubstrateDriver(),
        ],
        store=store,
    )
    vcscore.activate()
    return vcscore


def _produce_child_workspace_output(mg: VcsCore) -> tuple[ScopeInfo, ScopeInfo]:
    mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
    parent = mg.fork(mg.ground, "retained-parent")
    child = mg.fork(parent, "retained-child")
    mg.exec("filesystem", "write", scope=child, path="child.txt", content=b"child output\n")
    return parent, child


def _retained_identity(row: RetainedOutputQueryResult) -> RetainedOutputIdentity:
    return RetainedOutputIdentity(
        scope_name=row.scope_name,
        scope_ref=row.scope_ref,
        scope_instance_id=row.scope_instance_id,
        parent_ref=row.parent_ref,
        parent_scope_name=row.parent_scope_name,
        parent_scope_instance_id=row.parent_scope_instance_id,
        binding=row.binding or "",
        output_world_oid=row.output_world_oid or "",
        handoff_ref=row.handoff_ref or "",
        parent_basis_world_oid=row.parent_basis_world_oid or "",
        store_id=row.store_id or "",
        resource_id=row.resource_id or "",
        candidate_id=row.candidate_id or "",
        candidate_ref=row.candidate_ref or "",
        candidate_head=row.candidate_head or "",
    )


def _read_world_workspace_file(mg: VcsCore, world_oid: str, path: str) -> tuple[bytes, int] | None:
    manager = mg._world_storage()
    world = manager.read_world(world_oid)
    head = world.snapshot.head_for("workspace")
    substrate = manager.store(head.store_id)
    return read_substrate_workspace_file(substrate.repo, head.head, path)


def test_seal_read_and_select_retained_output_round_trip(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mg = _make_mg(workspace)
    try:
        parent, child = _produce_child_workspace_output(mg)
        child_world = mg.world_oid(child)
        assert child_world is not None
        assert _read_world_workspace_file(mg, child_world, "child.txt") == (b"child output\n", 0o100644)

        seal_result = mg.seal(child)

        assert seal_result.scope == child
        assert seal_result.parent == parent
        assert seal_result.handoff.output_world_oid == child_world
        assert "child.txt" in seal_result.handoff.changed_paths
        assert mg.lookup_scope(child.name) is None
        assert mg.store.scope_registry_entry(child.name, status="retained") is not None
        assert read_seal_handoff(mg.store, child).handoff == seal_result.handoff
        assert mg.retained_workspace_handle(child.name).output_world_oid == child_world
        assert mg.read_retained_workspace_file(child.name, "child.txt") == (b"child output\n", 0o100644)
        (retained_row,) = mg.list_retained_outputs(parent=parent, state="unconsumed")
        identity = _retained_identity(retained_row)
        assert mg.get_retained_output(identity) == retained_row
        with pytest.raises(InvalidRepositoryStateError, match="candidate_head"):
            mg.get_retained_output(replace(identity, candidate_head="other-head"))

        selection = mg.select_retained_output(child.name, parent=parent)

        assert selection.scope == child
        assert selection.parent == parent
        assert selection.output_world_oid == child_world
        assert selection.settlement.action == "selected"
        assert _read_world_workspace_file(mg, selection.parent_world_after, "child.txt") == (
            b"child output\n",
            0o100644,
        )
        assert mg.store.read_workspace_file(parent.ref, "child.txt") is None
        assert tuple(row.scope_name for row in mg.list_retained_outputs(parent=parent, state="selected")) == (
            child.name,
        )
    finally:
        mg.deactivate(warn_on_open_scopes=False)

    fresh = _make_mg(workspace)
    try:
        assert fresh.retained_workspace_handle("retained-child").output_world_oid == child_world
        assert fresh.read_retained_workspace_file("retained-child", "child.txt") == (b"child output\n", 0o100644)
    finally:
        fresh.deactivate(warn_on_open_scopes=False)
