"""Part B: the admission tier is bounded-when-present behind a test-enforced boundary.

Splits the one overloaded readiness source into a bounded index-backed *admission* source (the only
operation_journal source a mutating policy observes) and a scanning *status* source. The headline is
the count-contract: with the index present, the real mutation gate does ZERO operation-journal
namespace enumerations. See ``260622-admission-tier-open-ops-index.md`` (Part B / Part D).
"""

from __future__ import annotations

import pygit2
import vcs_core._operation_journal_inventory as oji
import vcs_core._query_readiness as qr
from vcs_core._operation_journal_inventory import probe_operation_journals
from vcs_core._query_readiness import ReadinessRequest, _admission_operation_journal_items
from vcs_core._world_refs import operation_journal_ref, world_open_operation_journal_index_ref
from vcs_core._world_storage_installation import open_or_init_default_world_storage
from vcs_core._world_storage_manager import WorldStorageManager
from vcs_core.vcscore import VcsCore

_OPEN_A = operation_journal_ref("open", "op-a")
_OPEN_B = operation_journal_ref("open", "op-b")


def _runtime_request(scope_ref: str) -> ReadinessRequest:
    return ReadinessRequest.create(
        command="vcscore.runtime", scope=scope_ref, requested_freshness="locked", allow_best_effort=False
    )


def _open_valid_journal(manager: WorldStorageManager, operation_id: str) -> None:
    manager.open_operation_journal(
        operation_id=operation_id, operation_kind="shepherd.task", target_ref="refs/vcscore/ground", input_world_oid=None
    )


def test_runtime_admission_does_not_enumerate_journal_namespace(mg: VcsCore, monkeypatch) -> None:
    """The headline boundary (count-contract): with the index present, the REAL mutation gate
    (vcscore.runtime) performs ZERO operation-journal-namespace enumerations — it reads the bounded
    index and probes only those refs, never scanning."""
    manager = open_or_init_default_world_storage(mg._repo_path)
    _open_valid_journal(manager, "op-a")  # co-written into the index

    def _boom(*_args, **_kwargs):
        raise AssertionError("admission must not enumerate the operation-journal ref namespace")

    # Guard the ROUTING boundary, not just the bounded source: patch BOTH the source-module binding
    # (the bounded fallback uses it) AND the binding imported into _query_readiness at import time
    # (the scanning STATUS source uses that one). A regression routing a mutating policy to the status
    # scan would call the _query_readiness binding and otherwise slip past a source-only patch.
    monkeypatch.setattr(oji, "probe_operation_journals", _boom)
    monkeypatch.setattr(qr, "probe_operation_journals", _boom)
    result = mg.query_readiness(_runtime_request(mg.ground.ref))

    # the gate completed without tripping the namespace-scan boom, and the bounded source still saw
    # the open journal (a journal-domain blocker), so the contract is not vacuous.
    assert not result.allowed
    assert any(blocker.kind == "operation_journal" for blocker in result.blockers)


def test_bounded_admission_equals_scan_over_mixed_states(mg: VcsCore) -> None:
    """Equivalence: with a fresh index, the bounded admission source returns exactly the scanning
    source's items over a mix of open and terminal journals."""
    manager = open_or_init_default_world_storage(mg._repo_path)
    for operation_id in ("op-a", "op-b", "op-c"):
        _open_valid_journal(manager, operation_id)
    manager.fail_operation_journal("op-c", error="boom")
    manager.archive_operation_journal("op-c")  # tombstones op-c in the index; terminal excluded from both

    bounded = {item.locator for item in _admission_operation_journal_items(mg._repo_path)}
    scan = {item.locator for item in probe_operation_journals(manager.world_store.repo, family="open")}

    assert bounded == scan == {_OPEN_A, _OPEN_B}


def test_missing_index_falls_back_read_only_then_heals_on_next_write(mg: VcsCore) -> None:
    """Missing index → a fallback scan returns the correct set, and admission is READ-ONLY (no
    rebuild side effect on the gate). The index self-heals on the next co-write, not on the read."""
    manager = open_or_init_default_world_storage(mg._repo_path)
    _open_valid_journal(manager, "op-a")
    manager.world_store.repo.references.delete(world_open_operation_journal_index_ref(manager.world_store.world_store_id))
    assert manager.read_open_operation_journal_index() is None  # missing

    bounded = {item.locator for item in _admission_operation_journal_items(mg._repo_path)}

    assert _OPEN_A in bounded  # fallback scan still correct
    assert manager.read_open_operation_journal_index() is None  # admission did NOT rebuild (strictly read-only)

    _open_valid_journal(manager, "op-b")  # the next co-write folds the index back from authority + the new ref
    assert manager.read_open_operation_journal_index() == {_OPEN_A, _OPEN_B}


def test_corrupt_index_blocks_ordinary_mutation(mg: VcsCore) -> None:
    """Corrupt index → admission fails closed with a blocking fact (never silently scans), so an
    ordinary mutating command is blocked."""
    manager = open_or_init_default_world_storage(mg._repo_path)
    _open_valid_journal(manager, "op-a")
    repo = manager.world_store.repo
    index_ref = world_open_operation_journal_index_ref(manager.world_store.world_store_id)
    sig = pygit2.Signature("t", "t@e.invalid")
    corrupt = repo.create_commit(None, sig, sig, "corrupt", repo.TreeBuilder().write(), [])
    repo.references.create(index_ref, corrupt, force=True)

    result = mg.query_readiness(_runtime_request(mg.ground.ref))

    assert not result.allowed
    assert any(blocker.item_id.startswith("open_operation_journal_index:") for blocker in result.blockers)


def _corrupt_open_journal_index(manager: WorldStorageManager) -> None:
    repo = manager.world_store.repo
    index_ref = world_open_operation_journal_index_ref(manager.world_store.world_store_id)
    sig = pygit2.Signature("t", "t@e.invalid")
    corrupt = repo.create_commit(None, sig, sig, "corrupt", repo.TreeBuilder().write(), [])
    repo.references.create(index_ref, corrupt, force=True)


def test_corrupt_index_is_recoverable_while_others_stay_blocked(mg: VcsCore) -> None:
    """Fork 1 (wired): a corrupt index blocks unrelated mutating commands, yet targeted
    `vcscore.recover` is EXEMPTED (the corrupt fact is a recovery target) and rebuilds it."""
    manager = open_or_init_default_world_storage(mg._repo_path)
    _open_valid_journal(manager, "op-a")
    _corrupt_open_journal_index(manager)

    # an unrelated mutating command is blocked by the corrupt-index fact
    blocked = mg.query_readiness(_runtime_request(mg.ground.ref))
    assert not blocked.allowed
    assert any(blocker.item_id.startswith("open_operation_journal_index:") for blocker in blocked.blockers)

    # targeted recovery is admitted (not blocked) and rebuilds the accelerator
    assert mg.recover_open_operation_journal_index() is True

    # healed: the corrupt-index blocker is gone and the rebuilt index sees the authority's open ref
    assert manager.open_operation_journal_index_corruption() is None
    assert manager.read_open_operation_journal_index() == {_OPEN_A}
    healed = mg.query_readiness(_runtime_request(mg.ground.ref))
    assert not any(blocker.item_id.startswith("open_operation_journal_index:") for blocker in healed.blockers)


def test_recover_open_operation_journal_index_is_noop_when_healthy(mg: VcsCore) -> None:
    """No corruption → targeted recovery is a no-op (no spurious missing-target blocker)."""
    manager = open_or_init_default_world_storage(mg._repo_path)
    _open_valid_journal(manager, "op-a")
    assert mg.recover_open_operation_journal_index() is False


# --- Part D: the perf CONTRACT (machine-independent count, not timing) ---


def _inflate_namespace_with_closed_refs(manager: WorldStorageManager, count: int) -> None:
    """Grow the total ref namespace with `count` cheap terminal refs (NOT indexed, NOT open)."""
    repo = manager.world_store.repo
    sig = pygit2.Signature("t", "t@e.invalid")
    filler = repo.create_commit(None, sig, sig, "filler", repo.TreeBuilder().write(), [])
    for index in range(count):
        repo.references.create(operation_journal_ref("closed", f"op-closed-{index}"), filler)


def test_admission_probe_count_is_bounded_by_open_set_not_total_refs(mg: VcsCore, monkeypatch) -> None:
    """The perf contract (machine-independent): with the index present, admission performs exactly
    O(open) per-ref probes and ZERO namespace enumerations — independent of the total ref count, so
    the bound is the open set, not O(total refs). (The status/fallback scan stays O(total-refs) where
    it legitimately runs — recorded, not gated.)"""
    manager = open_or_init_default_world_storage(mg._repo_path)
    _open_valid_journal(manager, "op-x")
    _open_valid_journal(manager, "op-y")
    _inflate_namespace_with_closed_refs(manager, count=40)  # 40 terminal refs that must NOT be probed
    open_x, open_y = operation_journal_ref("open", "op-x"), operation_journal_ref("open", "op-y")

    probed: list[str] = []
    real_probe_ref = oji.probe_operation_journal_ref

    def _counting_probe_ref(repo, ref, **kwargs):
        probed.append(ref)
        return real_probe_ref(repo, ref, **kwargs)

    def _boom(*_args, **_kwargs):
        raise AssertionError("admission must not enumerate the operation-journal ref namespace")

    monkeypatch.setattr(oji, "probe_operation_journal_ref", _counting_probe_ref)
    monkeypatch.setattr(oji, "probe_operation_journals", _boom)
    items = _admission_operation_journal_items(mg._repo_path)

    assert sorted(probed) == sorted([open_x, open_y])  # exactly the 2 open refs, not the 40 closed
    assert {item.locator for item in items} == {open_x, open_y}


def test_stale_over_reporting_index_is_recoverable(mg: VcsCore) -> None:
    """Review fix: a stale OVER-reporting index (lists a ref the authority lacks, from an out-of-model
    manual deletion) blocks admission with a phantom missing-ref fact — and the PUBLIC recover method
    repairs it. Previously recover only handled corrupt *records*, so it returned False for stale
    drift and left the operation wedged (only the lower-level manager rebuild could fix it)."""
    manager = open_or_init_default_world_storage(mg._repo_path)
    _open_valid_journal(manager, "op-a")
    # out-of-model: delete the open ref WITHOUT tombstoning the index -> the index over-reports
    manager.world_store.repo.references.delete(_OPEN_A)
    assert manager.read_open_operation_journal_index() == {_OPEN_A}  # index still lists the gone ref
    assert manager.verify_open_operation_journal_index().status == "stale"
    assert manager.open_operation_journal_index_corruption() is None  # the RECORD is valid, just stale

    # admission blocks on the phantom (an absent operation_journal fact for the listed-but-missing ref)
    blocked = mg.query_readiness(_runtime_request(mg.ground.ref))
    assert not blocked.allowed
    assert any(blocker.kind == "operation_journal" for blocker in blocked.blockers)

    # the public recover method now repairs stale drift, not just corrupt records
    assert mg.recover_open_operation_journal_index() is True
    assert manager.read_open_operation_journal_index() == frozenset()  # phantom reconciled away
    healed = mg.query_readiness(_runtime_request(mg.ground.ref))
    assert all(blocker.kind != "operation_journal" for blocker in healed.blockers)
