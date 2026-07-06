# under-test: vcs_core._world_storage_manager
"""Lease-level validation: the active-lease accelerator stays equivalent to the
authoritative ref scan across the real publish path, and the manager wiring is
fail-closed (missing -> fallback + self-heal; corrupt -> raise).

Reuses the world-storage manager test helpers for a real published world + leases.
"""

from __future__ import annotations

import pygit2
import pytest
from vcs_core import InvalidRepositoryStateError, canonical_bytes
from vcs_core._world_refs import world_publication_lease_index_ref
from vcs_core._world_storage_manager import DEFAULT_GROUND_REF

from .test_world_storage_manager import _manager, _published_workspace_world, _workspace_advance_world


def _index_ref(manager) -> str:
    return world_publication_lease_index_ref(manager.world_store.world_store_id)


def _corrupt_index(manager) -> None:
    repo = manager.world_store.repo
    bad = {"schema": "vcscore/active-lease-index/v1", "index_digest": "sha256:deadbeef", "entries": {}}
    meta = repo.TreeBuilder()
    meta.insert("active-lease-index.json", repo.create_blob(canonical_bytes(bad)), pygit2.GIT_FILEMODE_BLOB)
    root = repo.TreeBuilder()
    root.insert("meta", meta.write(), pygit2.GIT_FILEMODE_TREE)
    sig = pygit2.Signature("t", "t@e.invalid")
    commit = repo.create_commit(None, sig, sig, "corrupt", root.write(), [])
    repo.references.create(_index_ref(manager), commit, force=True)


def test_index_tracks_leases_and_matches_full_scan(tmp_path):
    manager = _manager(tmp_path)
    _, world_oid = _published_workspace_world(manager)

    lease_refs = manager._write_publication_leases((DEFAULT_GROUND_REF,), manager.read_world(world_oid))

    # the index path and the authoritative full scan agree, and the index is fresh
    assert manager._active_lease_targets_via_index() == {world_oid}
    assert manager._active_lease_targets_via_index() == manager._active_publication_lease_targets()
    assert manager.verify_active_lease_index().ok

    # release tombstones the index; both views go empty and stay equivalent
    manager._release_publication_leases(lease_refs, world_oid=world_oid)
    assert manager._active_lease_targets_via_index() == frozenset()
    assert manager._active_lease_targets_via_index() == manager._active_publication_lease_targets()
    assert manager.verify_active_lease_index().ok


def test_index_equivalent_under_multiple_leases(tmp_path):
    manager = _manager(tmp_path)
    workspace, w1 = _published_workspace_world(manager)
    w2 = _workspace_advance_world(
        manager, parent_world_oid=w1, parent_workspace_head=workspace, operation_id="op-second"
    )

    manager._write_publication_leases((DEFAULT_GROUND_REF,), manager.read_world(w1))
    manager._write_publication_leases((DEFAULT_GROUND_REF,), manager.read_world(w2))

    assert manager._active_lease_targets_via_index() == {w1, w2}
    assert manager._active_lease_targets_via_index() == manager._active_publication_lease_targets()
    assert manager.verify_active_lease_index().ok


def test_missing_index_falls_back_and_self_heals(tmp_path):
    manager = _manager(tmp_path)
    _, world_oid = _published_workspace_world(manager)
    manager._write_publication_leases((DEFAULT_GROUND_REF,), manager.read_world(world_oid))

    # delete the accelerator: the read must fall back to the authoritative scan...
    manager.world_store.repo.references.delete(_index_ref(manager))
    assert manager._active_lease_targets_via_index() == {world_oid}
    # ...and self-heal, so the record exists again for next time
    assert _index_ref(manager) in set(manager.world_store.repo.references)
    assert manager.verify_active_lease_index().ok


def test_corrupt_index_fails_closed_on_read(tmp_path):
    manager = _manager(tmp_path)
    _, world_oid = _published_workspace_world(manager)
    manager._write_publication_leases((DEFAULT_GROUND_REF,), manager.read_world(world_oid))

    _corrupt_index(manager)
    with pytest.raises(InvalidRepositoryStateError):
        manager._active_lease_targets_via_index()
    assert manager.verify_active_lease_index().status == "corrupt"


def test_hot_read_does_not_scan_lease_ref_namespace(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    _, world_oid = _published_workspace_world(manager)
    manager._write_publication_leases((DEFAULT_GROUND_REF,), manager.read_world(world_oid))

    # with the index present, the hot read must answer from the record, never the
    # authoritative O(total-refs) scan
    def boom() -> tuple[str, ...]:
        raise AssertionError("hot read must not scan the lease ref namespace")

    monkeypatch.setattr(manager, "_active_publication_lease_refs", boom)
    assert manager._active_lease_targets_via_index() == {world_oid}


def test_index_leads_authority_so_a_crash_yields_a_superset(tmp_path, monkeypatch):
    """The index updates BEFORE the authoritative lease ref is created, so a crash there
    leaves the index a SUPERSET of the authority (over-protect), never a subset."""
    # V2.2c: _write_publication_leases and its create_or_update_reference import moved to the
    # publication/retention controller module; patch the name where it now lives.
    import vcs_core._publication_retention_controller as pubret_mod

    manager = _manager(tmp_path)
    _, world_oid = _published_workspace_world(manager)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated crash before the lease ref is created")

    monkeypatch.setattr(pubret_mod, "create_or_update_reference", _boom)
    with pytest.raises(RuntimeError):
        manager._write_publication_leases((DEFAULT_GROUND_REF,), manager.read_world(world_oid))

    # the index leads: it holds the entry even though the authoritative ref was never
    # created — a superset (safe to over-protect), never a subset (would under-protect)
    assert world_oid in manager._active_lease_targets_via_index()
    assert manager._active_publication_lease_targets() == frozenset()


def test_release_over_missing_index_keeps_other_leases_protected(tmp_path):
    """Round-2 repro at the manager level: with two live leases and the index deleted,
    releasing one must not drop the other from the protection set (extend over a missing
    index must rebuild from the authority, not write an empty/subset record)."""
    manager = _manager(tmp_path)
    workspace, w1 = _published_workspace_world(manager)
    w2 = _workspace_advance_world(
        manager, parent_world_oid=w1, parent_workspace_head=workspace, operation_id="op-second"
    )
    l1 = manager._write_publication_leases((DEFAULT_GROUND_REF,), manager.read_world(w1))
    manager._write_publication_leases((DEFAULT_GROUND_REF,), manager.read_world(w2))

    # the accelerator goes missing (a reset, or authority that predates the index)
    manager.world_store.repo.references.delete(_index_ref(manager))

    # releasing w1 tombstones over the missing index; w2 must stay protected
    manager._release_publication_leases(l1, world_oid=w1)
    targets = manager._active_lease_targets_via_index()
    assert w2 in targets  # superset preserved — not an empty record that drops w2
    assert targets == manager._active_publication_lease_targets()


def test_add_over_missing_index_keeps_prior_leases_protected(tmp_path):
    """With a live lease and the index deleted, publishing a second world (an add over the
    missing index) must retain the first world's protection, not write a delta-only subset."""
    manager = _manager(tmp_path)
    workspace, w1 = _published_workspace_world(manager)
    w2 = _workspace_advance_world(
        manager, parent_world_oid=w1, parent_workspace_head=workspace, operation_id="op-second"
    )
    manager._write_publication_leases((DEFAULT_GROUND_REF,), manager.read_world(w1))

    manager.world_store.repo.references.delete(_index_ref(manager))
    manager._write_publication_leases((DEFAULT_GROUND_REF,), manager.read_world(w2))

    targets = manager._active_lease_targets_via_index()
    assert {w1, w2} <= targets  # prior lease retained — not a delta-only {w2}
    assert targets == manager._active_publication_lease_targets()


def test_declared_contract_is_superset_index_leads():
    """The lease index declares its read-safety / crash-lag policy, so the call-site
    ordering it depends on is a checked contract rather than a comment."""
    from vcs_core._incremental import ActiveLeaseIndex

    assert ActiveLeaseIndex.CONTRACT.read_safety == "superset"
    assert ActiveLeaseIndex.CONTRACT.crash_lag == "index-leads"


def test_release_crash_after_ref_delete_leaves_superset(tmp_path, monkeypatch):
    """Mirror of the add-path crash test, honoring the declared ``index-leads`` contract:
    on release the index TRAILS the authority (tombstone AFTER the ref delete), so a crash
    in that window leaves the index a SUPERSET (still protecting a world whose lease is
    gone), never a subset (which would under-protect a still-live world)."""
    manager = _manager(tmp_path)
    _, world_oid = _published_workspace_world(manager)
    lease_refs = manager._write_publication_leases((DEFAULT_GROUND_REF,), manager.read_world(world_oid))

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated crash after the lease ref is deleted")

    # V2.2c: caller and _record_lease_index both moved to the controller; patch it there.
    monkeypatch.setattr(manager._pubret, "_record_lease_index", _boom)
    with pytest.raises(RuntimeError):
        manager._release_publication_leases(lease_refs, world_oid=world_oid)

    # authority dropped the lease (ref deleted) but the index still holds it: a superset
    assert manager._active_publication_lease_targets() == frozenset()
    assert world_oid in manager._active_lease_targets_via_index()


def test_reset_lease_index_swallows_ref_deletion_failure(tmp_path, monkeypatch):
    """_reset_lease_index is recovery after an accelerator write already failed; a
    lower-level (non-KeyError) ref-deletion failure must be swallowed, never propagate to
    block a publish — the lease refs are the authority, the index is only a derived view."""
    manager = _manager(tmp_path)

    class _BoomRefs:
        def delete(self, _ref: str) -> None:
            raise pygit2.GitError("simulated locked-ref deletion failure")

    class _Repo:
        references = _BoomRefs()

    class _Store:
        world_store_id = "store"
        repo = _Repo()

    monkeypatch.setattr(manager, "_world_store", _Store())
    manager._reset_lease_index()  # must not raise despite the GitError from delete


def test_lease_entry_missing_world_oid_fails_closed(tmp_path):
    """A digest-valid record whose entry lacks world_oid is corrupt, not a wrong answer."""
    from vcs_core._incremental._git_record import with_self_digest, write_record

    manager = _manager(tmp_path)
    _published_workspace_world(manager)
    repo = manager.world_store.repo
    bad = with_self_digest(
        {
            "schema": "vcscore/active-lease-index/v1",
            "generation": 1,
            "base_segment_ref": None,
            "entries": {"r": {"operation_id": "op"}},  # missing world_oid
            "delta_added": {},
            "delta_removed": [],
        },
        digest_field="index_digest",
    )
    commit = write_record(repo, meta_name="active-lease-index.json", payload=bad, message="malformed")
    repo.references.create(_index_ref(manager), pygit2.Oid(hex=commit), force=True)

    with pytest.raises(InvalidRepositoryStateError):
        manager._active_lease_targets_via_index()
    assert manager.verify_active_lease_index().status == "corrupt"


def test_active_lease_index_corruption_surfaces_on_recovery_snapshot(tmp_path, monkeypatch):
    """The cheap (index-only) recovery probe surfaces a corrupt index as a visible error — NOT a
    readiness blocker. Missing/fresh and the stale verdict (which needs the authority scan) are not
    surfaced here; stale is a deep-fsck concern."""
    import vcs_core._world_storage_installation as wsi
    from vcs_core._app_readiness_projection import _recovery_blocker
    from vcs_core._query_inventory import ACTIVE_LEASE_INDEX_CORRUPT, severity_for
    from vcs_core._recovery_inventory import _active_lease_index_items

    class _FakeStore:
        world_store_id = "store_world_test"

    class _FakeManager:
        world_store = _FakeStore()

        def __init__(self, detail: str | None) -> None:
            self._detail = detail

        def active_lease_index_corruption(self) -> str | None:
            return self._detail

    def _items_for(detail: str | None) -> tuple:
        monkeypatch.setattr(wsi, "default_world_storage_exists", lambda _p: True)
        monkeypatch.setattr(wsi, "open_existing_default_world_storage", lambda _p: _FakeManager(detail))
        return _active_lease_index_items(tmp_path)

    (corrupt_item,) = _items_for("self-digest mismatch")
    assert corrupt_item.kind == "active_lease_index"
    assert corrupt_item.issues[0].code == ACTIVE_LEASE_INDEX_CORRUPT
    assert severity_for(corrupt_item.health) == "error"  # corrupt is a visible error...
    assert "blocker" not in corrupt_item.role  # ...but not a readiness blocker (scoped to the runtime read)
    assert _recovery_blocker(corrupt_item, corrupt_item.issues[0]) is None  # not a RecoveryKind -> not an app-blocker

    assert _items_for(None) == ()  # missing / fresh / (uncheap) stale -> nothing on the cheap path


def test_active_lease_index_corruption_probe_is_scan_free(tmp_path, monkeypatch):
    """The cheap corruption probe reads only the index record — it must NOT enumerate the lease ref
    namespace (the O(total-refs) scan the index exists to avoid, and the reviewer's regression)."""
    manager = _manager(tmp_path)
    _, world_oid = _published_workspace_world(manager)
    manager._write_publication_leases((DEFAULT_GROUND_REF,), manager.read_world(world_oid))

    def boom() -> tuple[str, ...]:
        raise AssertionError("the cheap corruption probe must not scan the lease ref namespace")

    monkeypatch.setattr(manager, "_active_publication_lease_refs", boom)
    assert manager.active_lease_index_corruption() is None  # present-valid index, no scan

    _corrupt_index(manager)
    assert manager.active_lease_index_corruption() is not None  # corrupt detected, still no scan


def test_fsck_world_deep_surfaces_stale_lease_index(tmp_path, monkeypatch):
    """Stale-vs-authority verification (the full scan) lives on the explicit deep-fsck path, not the
    readiness probe — a stale verdict surfaces as a deep-fsck issue."""
    from vcs_core._incremental import Health

    manager = _manager(tmp_path)
    _, world_oid = _published_workspace_world(manager)
    manager._write_publication_leases((DEFAULT_GROUND_REF,), manager.read_world(world_oid))

    # V2.3: fsck_world_deep moved to WorldFsckController and calls verify_active_lease_index on
    # the pub/ret controller directly; patch it there (the WSM shim is bypassed).
    monkeypatch.setattr(
        manager._pubret, "verify_active_lease_index", lambda: Health("stale", "index has 0; authority has 1")
    )
    report = manager.fsck_world(world_oid, mode="deep")
    assert "active_lease_index_stale" in {issue.code for issue in report.issue_details}


def test_active_lease_index_probe_is_silent_without_default_world_storage(tmp_path, monkeypatch):
    import vcs_core._world_storage_installation as wsi
    from vcs_core._recovery_inventory import _active_lease_index_items

    monkeypatch.setattr(wsi, "default_world_storage_exists", lambda _p: False)
    assert _active_lease_index_items(tmp_path) == ()
