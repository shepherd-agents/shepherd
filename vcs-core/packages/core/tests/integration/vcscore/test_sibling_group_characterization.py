"""Sequential lifecycle characterization with deferred sibling-group blockers."""

from __future__ import annotations

import pytest
from vcs_core._errors import MergePreconditionError, ScopeAdmissionError, SiblingGroupRecoveryRequiredError
from vcs_core._sibling_groups import (
    CarrierLeaseRecord,
    SiblingGroupRecord,
    SiblingHandleRecord,
    sibling_machine_scope_name,
)
from vcs_core.git_store import build_tree, create_signature
from vcs_core.store import Store
from vcs_core.vcscore import VcsCore


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


def _group_record(store: Store, *, group_id: str, status: str) -> SiblingGroupRecord:
    siblings = (_sibling(store, group_id=group_id, ordinal=0), _sibling(store, group_id=group_id, ordinal=1))
    return SiblingGroupRecord(
        group_id=group_id,
        parent_ref=Store.GROUND_REF,
        parent_world_id="ground-world",
        admitted_parent_oid=_parent_oid(store),
        status=status,  # type: ignore[arg-type]
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


def _write_raw_sibling_group_payload(store: Store, *, group_id: str, payload: bytes) -> None:
    tree_oid = build_tree(store._repo, None, [("meta/sibling-group.json", payload)])
    sig = create_signature("sibling-group")
    commit_oid = store._repo.create_commit(
        None,
        sig,
        sig,
        f"sibling-group:{group_id}",
        tree_oid,
        [],
    )
    store._repo.references.create(Store.sibling_group_ref(group_id), commit_oid)


def test_ordinary_fork_still_rejects_second_live_child(mg: VcsCore) -> None:
    first = mg.fork(mg.ground, "task-one")

    with pytest.raises(ScopeAdmissionError, match="already has live child scope 'task-one'"):
        mg.fork(mg.ground, "task-two")

    mg.discard(first)


def test_store_primitive_siblings_remain_outside_product_admission(store: Store) -> None:
    first = store.fork(Store.GROUND_REF, sibling_machine_scope_name("sg-111111111111", 0))
    second = store.fork(Store.GROUND_REF, sibling_machine_scope_name("sg-111111111111", 1))

    assert first.ref in store._repo.references
    assert second.ref in store._repo.references

    mismatches = store.scope_registry_projection_mismatches()
    assert [mismatch.ref for mismatch in mismatches] == [first.ref, second.ref]
    assert {mismatch.kind for mismatch in mismatches} == {"ref_exists_registry_non_live"}


def test_store_primitive_sibling_refs_restore_as_orphans_not_admitted_groups(workspace) -> None:
    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    first = store.fork(Store.GROUND_REF, sibling_machine_scope_name("sg-222222222222", 0))
    second = store.fork(Store.GROUND_REF, sibling_machine_scope_name("sg-222222222222", 1))
    vcscore = VcsCore(str(workspace), store=store)

    try:
        vcscore.activate()

        assert set(vcscore.list_orphaned_scope_refs()) == {first.ref, second.ref}
        assert vcscore.store.list_sibling_groups().groups == ()
    finally:
        vcscore.deactivate(warn_on_open_scopes=False)


def test_generated_machine_scope_names_satisfy_store_flat_ref_constraints(store: Store) -> None:
    first = store.fork(Store.GROUND_REF, sibling_machine_scope_name("sg-333333333333", 0))
    second = store.fork(Store.GROUND_REF, sibling_machine_scope_name("sg-333333333333", 1))

    assert first.name == "sib-333333333333-0"
    assert second.name == "sib-333333333333-1"


def test_parent_advanced_merge_and_rebase_remain_fail_closed(store: Store) -> None:
    first = store.fork(Store.GROUND_REF, "task-one")
    second = store.fork(Store.GROUND_REF, "task-two")
    store._emit_effect(first, "WorkOne", {}, substrate="test")
    store._emit_effect(second, "WorkTwo", {}, substrate="test")
    store.merge(first, Store.GROUND_REF)

    with pytest.raises(MergePreconditionError, match="sequential live-child policy"):
        store.merge(second, Store.GROUND_REF)

    with pytest.raises(NotImplementedError, match="three-way merge"):
        store.rebase(second, Store.GROUND_REF)


def test_unfinished_sibling_group_blocks_vcscore_lifecycle_mutations(workspace) -> None:
    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    group = _group_record(store, group_id="sg-444444444444", status="admitted")
    assert store._publish_sibling_group_for_recovery_test(group, expected_head_oid=None)
    vcscore = VcsCore(str(workspace), store=store)

    try:
        vcscore.activate()

        assert vcscore.list_sibling_group_blockers() == ("sg-444444444444 (admitted)",)
        with pytest.raises(SiblingGroupRecoveryRequiredError, match="sg-444444444444"):
            vcscore.fork(vcscore.ground, "task-blocked")
        with pytest.raises(SiblingGroupRecoveryRequiredError, match="sg-444444444444"):
            vcscore.push(dry_run=True)
        with pytest.raises(SiblingGroupRecoveryRequiredError, match="sg-444444444444"):
            vcscore.archive_orphaned_scopes()
    finally:
        vcscore.deactivate(warn_on_open_scopes=False)


def test_unfinished_sibling_group_blocks_direct_runtime_mutations(mg: VcsCore) -> None:
    group = _group_record(mg.store, group_id="sg-777777777777", status="admitted")
    assert mg.store._publish_sibling_group_for_recovery_test(group, expected_head_oid=None)

    assert mg.list_sibling_group_blockers() == ("sg-777777777777 (admitted)",)
    with pytest.raises(SiblingGroupRecoveryRequiredError, match="sg-777777777777"):
        mg.exec("marker", "mark", scope=mg.ground, label="blocked")
    with (
        pytest.raises(SiblingGroupRecoveryRequiredError, match="sg-777777777777"),
        mg.runtime_activity(
            scope=mg.ground,
            operation_label="blocked",
            operation_kind="test.blocked",
        ),
    ):
        pytest.fail("runtime activity should be blocked before yielding")


def test_unfinished_sibling_group_blocks_substrate_runtime_effects(mg: VcsCore) -> None:
    group = _group_record(mg.store, group_id="sg-888888888888", status="running")
    assert mg.store._publish_sibling_group_for_recovery_test(group, expected_head_oid=None)
    marker = mg.resolve_binding("marker").instance

    with pytest.raises(SiblingGroupRecoveryRequiredError, match="sg-888888888888"):
        marker.mark("blocked", scope=mg.ground)


def test_unreadable_sibling_group_blocks_direct_runtime_mutations(mg: VcsCore) -> None:
    _write_raw_sibling_group_payload(mg.store, group_id="sg-999999999999", payload=b"not json")

    assert mg.list_sibling_group_blockers() == ("sg-999999999999 (unreadable)",)
    with pytest.raises(SiblingGroupRecoveryRequiredError, match="sg-999999999999"):
        mg.exec("marker", "mark", scope=mg.ground, label="blocked")


def test_lifecycle_recovery_internal_writes_ignore_sibling_group_runtime_blocker(workspace) -> None:
    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    vcscore = VcsCore(str(workspace), store=store)
    vcscore.activate()
    try:
        task = vcscore.fork(vcscore.ground, "task-recovery-allowed")
        parent = vcscore.ground
        with vcscore._lock:
            vcscore._begin_lifecycle_run(
                operation="discard", phase="prepare_discard_effects", scope=task, parent=parent
            )
        group = _group_record(store, group_id="sg-aaaaaaaaaaaa", status="admitted")
        assert store._publish_sibling_group_for_recovery_test(group, expected_head_oid=None)
    finally:
        vcscore.deactivate(warn_on_open_scopes=False)

    recovered = VcsCore(str(workspace), store=store)
    try:
        recovered.activate(recover_lifecycle="resume")

        assert not recovered.store.ref_exists("refs/vcscore/scopes/task-recovery-allowed")
        assert recovered.list_sibling_group_blockers() == ("sg-aaaaaaaaaaaa (admitted)",)
    finally:
        recovered.deactivate(warn_on_open_scopes=False)


def test_terminal_sibling_group_does_not_block_lifecycle_mutations(workspace) -> None:
    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    group = _group_record(store, group_id="sg-555555555555", status="merged")
    assert store._publish_sibling_group_for_recovery_test(group, expected_head_oid=None)
    vcscore = VcsCore(str(workspace), store=store)

    try:
        vcscore.activate()

        assert vcscore.list_sibling_group_blockers() == ()
        task = vcscore.fork(vcscore.ground, "task-allowed")
        vcscore.discard(task)
    finally:
        vcscore.deactivate(warn_on_open_scopes=False)


def test_unreadable_sibling_group_blocks_vcscore_lifecycle_mutations(workspace) -> None:
    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    _write_raw_sibling_group_payload(store, group_id="sg-666666666666", payload=b"not json")
    vcscore = VcsCore(str(workspace), store=store)

    try:
        vcscore.activate()

        assert vcscore.list_sibling_group_blockers() == ("sg-666666666666 (unreadable)",)
        with pytest.raises(SiblingGroupRecoveryRequiredError, match="sg-666666666666"):
            vcscore.fork(vcscore.ground, "task-blocked")
    finally:
        vcscore.deactivate(warn_on_open_scopes=False)
