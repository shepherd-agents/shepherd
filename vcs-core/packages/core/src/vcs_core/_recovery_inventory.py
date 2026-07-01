"""Private inventory probes for existing vcs-core recovery blockers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from vcs_core._materialization_recovery import probe_materialization_recovery_state
from vcs_core._projection_store import (
    ScopeRegistryMismatchKind,
    scope_status_is_runtime_open,
    scope_status_owns_ref,
)
from vcs_core._query_inventory import (
    ACTIVE_LEASE_INDEX_CORRUPT,
    RECOVERY_DIRTY_PUSH,
    RECOVERY_DIRTY_PUSH_CORRUPT,
    RECOVERY_MATERIALIZATION_RUN,
    RECOVERY_MATERIALIZATION_RUN_CORRUPT,
    RECOVERY_ORPHANED_OPERATION_REF,
    RECOVERY_ORPHANED_SCOPE_REF,
    RECOVERY_SCOPE_REGISTRY_MISMATCH,
    RECOVERY_SIBLING_GROUP_BLOCKER,
    Health,
    HealthIssue,
    InventoryIssue,
    InventoryItem,
    InventorySnapshot,
    issue_id,
    present_invalid,
)
from vcs_core._sibling_groups import BLOCKING_SIBLING_GROUP_STATUSES as _BLOCKING_SIBLING_GROUP_STATUSES
from vcs_core._world_refs import world_publication_lease_index_ref

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vcs_core._projection_store import ScopeRegistryMismatch
    from vcs_core._sibling_groups import SiblingGroupListing
    from vcs_core.store import Store
    from vcs_core.types import OperationSummary
    from vcs_core.vcscore import VcsCore


RECLAIMABLE_SCOPE_REF_MISMATCH_KINDS: frozenset[ScopeRegistryMismatchKind] = frozenset(
    {"ref_exists_registry_non_live", "retained_requires_seal_and_select"}
)


@dataclass(frozen=True)
class ScopeRefRecoveryClassification:
    """Registry-derived scope-ref recovery facts for legacy and cleanup callers."""

    orphaned_scope_refs: tuple[str, ...]
    protected_ref_owning_refs: frozenset[str]
    reclaimable_mismatch_item_ids: frozenset[str]
    non_reclaimable_mismatch_item_ids: frozenset[str]


class _OperationRefLike(Protocol):
    @property
    def ref(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def scope_ref(self) -> str: ...

    @property
    def scope_instance_id(self) -> str: ...

    @property
    def durable_id(self) -> str: ...

    @property
    def display_label(self) -> str: ...

    @property
    def session_id(self) -> str | None: ...


def recovery_inventory_snapshot(owner: VcsCore) -> InventorySnapshot:
    """Return descriptive inventory for current recovery/debug state."""
    base = recovery_inventory_snapshot_for_store(owner._repo_path, owner._store)
    runtime_items = (
        *_orphaned_scope_items(owner._store, list(owner._orphaned_refs)),
        *_scope_registry_mismatch_items(owner._store, tuple(owner._scope_registry_mismatches)),
        *_orphaned_operation_items(owner._store, list(owner._orphaned_operations)),
    )
    items = _dedupe_items((*base.items, *runtime_items))
    issues = tuple(issue for item in items for issue in item.issues)
    return InventorySnapshot.create(items=items, issues=issues)


def recovery_inventory_snapshot_for_store(repo_path: str | Path, store: Store) -> InventorySnapshot:
    """Return recovery inventory from durable/projection state without activation."""
    mismatches = store.scope_registry_projection_mismatches()
    items = (
        *_orphaned_scope_items(store, orphaned_scope_refs_from_store(store, repo_path, mismatches=mismatches)),
        *_scope_registry_mismatch_items(store, mismatches),
        *_orphaned_operation_items(store, store.list_open_operations()),
        *_sibling_group_items(store, store.list_sibling_groups()),
        *_materialization_items(repo_path),
        *_active_lease_index_items(repo_path),
    )
    issues = tuple(issue for item in items for issue in item.issues)
    return InventorySnapshot.create(items=items, issues=issues)


def _dedupe_items(items: tuple[InventoryItem, ...]) -> tuple[InventoryItem, ...]:
    seen: set[str] = set()
    deduped: list[InventoryItem] = []
    for item in items:
        if item.id in seen:
            continue
        seen.add(item.id)
        deduped.append(item)
    return tuple(deduped)


def orphaned_scope_refs_from_store(
    store: Store,
    repo_path: str | Path,
    *,
    mismatches: tuple[ScopeRegistryMismatch, ...] | None = None,
) -> list[str]:
    """Derive orphaned scope refs from durable refs, projection state, and v2 authority refs."""
    return list(scope_ref_recovery_classification(store, repo_path, mismatches=mismatches).orphaned_scope_refs)


def scope_ref_recovery_classification(
    store: Store,
    repo_path: str | Path,
    *,
    mismatches: tuple[ScopeRegistryMismatch, ...] | None = None,
) -> ScopeRefRecoveryClassification:
    """Classify scope refs without conflating retained candidates with live orphans."""
    projection_mismatches = mismatches if mismatches is not None else store.scope_registry_projection_mismatches()
    snapshot = store.load_scope_registry_projection()
    if snapshot is None:
        orphaned_refs = store.list_scope_refs()
        unprojected_seen_refs = set(orphaned_refs)
        return ScopeRefRecoveryClassification(
            orphaned_scope_refs=(
                *orphaned_refs,
                *[ref for ref in _v2_scope_authority_refs(repo_path) if ref not in unprojected_seen_refs],
            ),
            protected_ref_owning_refs=frozenset(),
            reclaimable_mismatch_item_ids=frozenset(),
            non_reclaimable_mismatch_item_ids=frozenset(),
        )

    orphaned_refs = []
    seen_refs: set[str] = set()
    protected_ref_owning_refs: set[str] = set()
    mismatches_by_ref: dict[str, list[ScopeRegistryMismatch]] = {}
    for mismatch in projection_mismatches:
        mismatches_by_ref.setdefault(mismatch.ref, []).append(mismatch)
    mismatched_refs = set(mismatches_by_ref)
    reclaimable_mismatch_item_ids: set[str] = set()
    non_reclaimable_mismatch_item_ids: set[str] = set()

    def append_orphaned_ref(ref: str) -> None:
        if ref in seen_refs:
            return
        orphaned_refs.append(ref)
        seen_refs.add(ref)

    for entry in snapshot.entries:
        if scope_status_is_runtime_open(entry.status):
            append_orphaned_ref(entry.ref)
            continue
        if scope_status_owns_ref(entry.status) and entry.ref not in mismatched_refs and store.ref_exists(entry.ref):
            protected_ref_owning_refs.add(entry.ref)

    for ref, ref_mismatches in sorted(mismatches_by_ref.items()):
        if ref in seen_refs:
            continue
        ref_kinds = {mismatch.kind for mismatch in ref_mismatches}
        mismatch_item_ids = {_scope_registry_mismatch_item_id(mismatch) for mismatch in ref_mismatches}
        if ref_kinds <= RECLAIMABLE_SCOPE_REF_MISMATCH_KINDS and store.ref_exists(ref):
            append_orphaned_ref(ref)
            reclaimable_mismatch_item_ids.update(mismatch_item_ids)
            continue
        non_reclaimable_mismatch_item_ids.update(mismatch_item_ids)

    for ref in _v2_scope_authority_refs(repo_path):
        if ref in seen_refs:
            continue
        registry_entry = snapshot.entries_by_ref.get(ref)
        if (
            registry_entry is not None
            and scope_status_owns_ref(registry_entry.status)
            and ref not in mismatched_refs
        ):
            protected_ref_owning_refs.add(ref)
            continue
        append_orphaned_ref(ref)
    return ScopeRefRecoveryClassification(
        orphaned_scope_refs=tuple(orphaned_refs),
        protected_ref_owning_refs=frozenset(protected_ref_owning_refs),
        reclaimable_mismatch_item_ids=frozenset(reclaimable_mismatch_item_ids),
        non_reclaimable_mismatch_item_ids=frozenset(non_reclaimable_mismatch_item_ids),
    )


def _scope_registry_mismatch_item_id(mismatch: ScopeRegistryMismatch) -> str:
    subject = mismatch.scope_name or mismatch.ref
    return f"recovery:scope_registry_mismatch:{subject}:{mismatch.kind}:{mismatch.ref}"


def recovery_orphaned_scope_refs(snapshot: InventorySnapshot) -> tuple[str, ...]:
    """Project inventory back to the legacy RecoverySnapshot shape."""
    return tuple(
        str(item.locator)
        for item in snapshot.items
        if item.domain == "recovery" and item.kind == "orphaned_scope_ref" and item.locator is not None
    )


def recovery_orphaned_operation_ids(snapshot: InventorySnapshot) -> tuple[str, ...]:
    return tuple(
        str(item.fields["operation_id"])
        for item in snapshot.items
        if item.domain == "recovery"
        and item.kind == "orphaned_operation_ref"
        and isinstance(item.fields.get("operation_id"), str)
    )


def recovery_orphaned_operation_items(snapshot: InventorySnapshot) -> tuple[InventoryItem, ...]:
    """Project orphaned-operation inventory items for legacy summary hydration."""
    return tuple(item for item in snapshot.items if item.domain == "recovery" and item.kind == "orphaned_operation_ref")


def _orphaned_scope_items(store: Store, refs: list[str]) -> tuple[InventoryItem, ...]:
    return tuple(
        _recovery_item(
            item_id=f"recovery:orphaned_scope:{ref}",
            kind="orphaned_scope_ref",
            locator=ref,
            source_kind="git_ref",
            health_status="recovery_required",
            issue_code=RECOVERY_ORPHANED_SCOPE_REF,
            message=f"orphaned scope ref requires recovery: {ref}",
            fields={"scope_ref": ref, "scope_name": ref.rsplit("/", 1)[-1]},
            source_identity=_ref_identity(store, ref),
        )
        for ref in refs
    )


def _scope_registry_mismatch_items(
    store: Store,
    mismatches: tuple[ScopeRegistryMismatch, ...],
) -> tuple[InventoryItem, ...]:
    items: list[InventoryItem] = []
    for mismatch in mismatches:
        fields: dict[str, object] = {
            "mismatch_kind": mismatch.kind,
            "ref": mismatch.ref,
            "detail": mismatch.detail,
        }
        if mismatch.scope_name is not None:
            fields["scope_name"] = mismatch.scope_name
        items.append(
            _recovery_item(
                item_id=_scope_registry_mismatch_item_id(mismatch),
                kind="scope_registry_mismatch",
                locator=mismatch.ref,
                source_kind="projection",
                health_status="projection_mismatch",
                issue_code=RECOVERY_SCOPE_REGISTRY_MISMATCH,
                message=mismatch.detail,
                fields=fields,
                source_identity=_ref_identity(store, mismatch.ref),
            )
        )
    return tuple(items)


def _orphaned_operation_items(store: Store, operations: Sequence[_OperationRefLike]) -> tuple[InventoryItem, ...]:
    return tuple(_orphaned_operation_item(store, operation) for operation in operations)


def _orphaned_operation_item(
    store: Store,
    operation: _OperationRefLike,
) -> InventoryItem:
    summary = _operation_summary(store, operation)
    operation_id = summary.operation_id if summary is not None else operation.durable_id
    fields: dict[str, object] = {
        "operation_id": operation_id,
        "operation_label": operation.display_label,
        "operation_kind": operation.kind,
        "scope_ref": operation.scope_ref,
        "scope_instance_id": operation.scope_instance_id,
        "session_id": operation.session_id,
    }
    if summary is not None:
        fields.update(
            {
                "status": summary.status,
                "visibility": summary.visibility,
                "world_id": summary.world_id,
                "world_name": summary.world_name,
            }
        )
    return _recovery_item(
        item_id=f"recovery:orphaned_operation:{operation.ref}",
        kind="orphaned_operation_ref",
        locator=operation.ref,
        source_kind="runtime_state",
        health_status="recovery_required",
        issue_code=RECOVERY_ORPHANED_OPERATION_REF,
        message=f"orphaned operation ref requires recovery: {operation.ref}",
        fields=fields,
        source_identity=_ref_identity(store, operation.ref),
    )


def _operation_summary(store: Store, operation: _OperationRefLike) -> OperationSummary | None:
    try:
        if store.ref_exists(operation.ref):
            return store.read_operation_history(operation.ref).summary
    except Exception:  # noqa: BLE001
        return None
    return None


def _sibling_group_items(store: Store, listing: SiblingGroupListing) -> tuple[InventoryItem, ...]:
    items: list[InventoryItem] = []
    for snapshot in listing.groups:
        record = snapshot.record
        if record.status not in _BLOCKING_SIBLING_GROUP_STATUSES:
            continue
        label = f"{record.group_id} ({record.status})"
        ref = store.sibling_group_ref(record.group_id)
        items.append(
            _recovery_item(
                item_id=f"recovery:sibling_group:{record.group_id}",
                kind="sibling_group_blocker",
                locator=ref,
                source_kind="git_ref",
                health_status="recovery_required",
                issue_code=RECOVERY_SIBLING_GROUP_BLOCKER,
                message=f"sibling group requires recovery before mutation: {label}",
                fields={"label": label, "group_id": record.group_id, "status": record.status},
                source_identity=_ref_identity(store, ref),
            )
        )
    for unreadable in listing.unreadable:
        label = f"{unreadable.group_id} (unreadable)"
        items.append(
            _recovery_item(
                item_id=f"recovery:sibling_group:{unreadable.group_id}",
                kind="sibling_group_blocker",
                locator=unreadable.ref,
                source_kind="git_ref",
                health_status="present_corrupt",
                issue_code=RECOVERY_SIBLING_GROUP_BLOCKER,
                message=f"sibling group requires recovery before mutation: {label}",
                fields={
                    "label": label,
                    "group_id": unreadable.group_id,
                    "status": "unreadable",
                    "reason": unreadable.reason,
                },
                source_identity=_ref_identity(store, unreadable.ref),
                primary_issue="corrupt",
            )
        )
    return tuple(items)


def _materialization_items(repo_path: str | Path) -> tuple[InventoryItem, ...]:
    repo_path = str(repo_path)
    repo_dir = Path(repo_path)
    items: list[InventoryItem] = []
    run_path = repo_dir / "materialization-run.json"
    state = probe_materialization_recovery_state(repo_path)
    if state.run.presence == "present" and state.run.validity == "corrupt":
        items.append(
            _recovery_item(
                item_id="recovery:materialization_run",
                kind="materialization_run",
                locator=str(run_path),
                source_kind="filesystem_file",
                health_status="present_corrupt",
                issue_code=RECOVERY_MATERIALIZATION_RUN_CORRUPT,
                message=f"materialization run ledger is unreadable: {state.run.error}",
                fields={},
                source_identity=_file_identity(run_path),
                primary_issue="corrupt",
            )
        )
    elif state.run.run is not None:
        run = state.run.run
        items.append(
            _recovery_item(
                item_id=f"recovery:materialization_run:{run.run_id}",
                kind="materialization_run",
                locator=str(run_path),
                source_kind="filesystem_file",
                health_status="recovery_required",
                issue_code=RECOVERY_MATERIALIZATION_RUN,
                message=f"materialization run {run.run_id!r} requires verification or cleanup",
                fields={
                    "session_id": run.session_id,
                    "run_id": run.run_id,
                    "planned_unit_count": len(run.planned_unit_ids),
                    "completed_unit_count": len(run.completed_unit_ids),
                },
                source_identity=_file_identity(run_path),
            )
        )
    dirty_path = repo_dir / "dirty"
    if state.dirty.presence == "present" and state.dirty.validity == "corrupt":
        items.append(
            _recovery_item(
                item_id="recovery:dirty_push",
                kind="dirty_push",
                locator=str(dirty_path),
                source_kind="filesystem_file",
                health_status="present_corrupt",
                issue_code=RECOVERY_DIRTY_PUSH_CORRUPT,
                message=f"dirty push flag is unreadable: {state.dirty.error}",
                fields={},
                source_identity=_file_identity(dirty_path),
                primary_issue="corrupt",
            )
        )
    elif state.dirty.presence == "present":
        session_id = state.dirty.session_id or ""
        timestamp = state.dirty.timestamp or 0.0
        items.append(
            _recovery_item(
                item_id=f"recovery:dirty_push:{session_id}",
                kind="dirty_push",
                locator=str(dirty_path),
                source_kind="filesystem_file",
                health_status="recovery_required",
                issue_code=RECOVERY_DIRTY_PUSH,
                message=f"dirty push flag from session {session_id!r} requires recovery",
                fields={"session_id": session_id, "timestamp": timestamp},
                source_identity=_file_identity(dirty_path),
            )
        )
    return tuple(items)


def _recovery_item(
    *,
    item_id: str,
    kind: str,
    locator: str | None,
    source_kind: str,
    health_status: str,
    issue_code: str,
    message: str,
    fields: dict[str, object],
    source_identity: dict[str, object],
    primary_issue: HealthIssue = "dangling_dependency",
) -> InventoryItem:
    recovery_hint = "Use the matching recovery command before mutating or materializing."
    if kind == "dirty_push":
        recovery_hint = "Run `vcs-core recover-materialization --mode repair` before mutating or materializing."
    elif kind == "materialization_run":
        recovery_hint = "Run `vcs-core recover-materialization --mode verify`, `repair`, or `force`."
    issue = InventoryIssue(
        id=issue_id(item_id, issue_code),
        code=issue_code,
        message=message,
        subject_id=item_id,
        locator=locator,
        recovery_hint=recovery_hint,
    )
    return InventoryItem(
        id=item_id,
        domain="recovery",
        kind=kind,
        locator=locator,
        source_kind=source_kind,
        source_store="coordinator",
        health=present_invalid(
            primary_issue=primary_issue,
            issue_codes=(issue_code,),
            lifecycle="recoverable",
            authority_role="projection",
            status=health_status,
        ),
        role=("recovery", "blocker"),
        fields=fields,
        source_identity=source_identity,
        issues=(issue,),
        # Tier-2: RECOVERABLE — the RecoveryKind set (orphaned ops/scopes, dirty push, materialization
        # run, sibling group), actionable + targetable through normal recovery selection. Declared
        # here; targeting/blocking stays driven by the legacy recovery-domain rules (unchanged).
        disposition="recoverable",
    )


_ACTIVE_LEASE_INDEX_HINT = (
    "Reconcile the accelerator with `WorldStorageManager.rebuild_active_lease_index()` "
    "(recovery also rebuilds it after stale-lease cleanup); the authoritative lease refs are unaffected."
)


def _active_lease_index_items(repo_path: str | Path) -> tuple[InventoryItem, ...]:
    """Surface active-lease accelerator health on the recovery snapshot — **cheaply**.

    The lease index is a fail-closed *derived view* over the authoritative lease refs, never
    authority itself. This probe runs on the readiness/recovery path, so it reads ONLY the index
    record (one blob, via ``active_lease_index_corruption``) and surfaces the *corrupt* case — it
    does **not** scan the authoritative lease refs. Stale-vs-authority verification needs the full
    O(total-refs) scan and is deferred to deep fsck (``fsck_world(mode="deep")``), keeping the scan
    the lease index exists to avoid off this path. ``missing`` self-heals via the fallback (benign)
    and ``fresh`` is healthy — neither is surfaced.

    The corrupt item is a *visible error* with a rebuild hint, but NOT tagged a readiness
    ``"blocker"`` and ``kind="active_lease_index"`` is deliberately NOT a ``RecoveryKind``: the
    runtime fail-closed read already blocks exactly the operations that read the index (publishes),
    correctly scoped. Promoting it to a ``RecoveryKind`` would route it through ``_ALL_RECOVERY_KINDS``
    and over-block unrelated commands (``vcscore.materialize``, and the recover commands that
    *rebuild* it). Probing must never break the snapshot, so any open/probe failure yields no items.
    """
    from vcs_core._world_storage_installation import default_world_storage_exists, open_existing_default_world_storage

    if not default_world_storage_exists(repo_path):
        return ()
    try:
        manager = open_existing_default_world_storage(repo_path)
        corruption_detail = manager.active_lease_index_corruption()  # cheap: index-only, no authority scan
        store_id = manager.world_store.world_store_id
    except Exception:  # noqa: BLE001
        return ()
    if corruption_detail is None:
        return ()  # missing/fresh; the cheap probe cannot see stale — that is deep fsck's job
    return (
        _active_lease_index_item(
            store_id=store_id,
            ref=world_publication_lease_index_ref(store_id),
            health=present_invalid(
                primary_issue="corrupt",
                issue_codes=(ACTIVE_LEASE_INDEX_CORRUPT,),
                lifecycle="recoverable",
                authority_role="projection",
                status="present_corrupt",
            ),
            issue_code=ACTIVE_LEASE_INDEX_CORRUPT,
            # Visible error, not a readiness "blocker": the runtime fail-closed read scopes the
            # block to publishes; tagging it a blocker (or a RecoveryKind) would over-promise.
            role=("recovery",),
            message=(
                "active-lease index is corrupt; publication-retention reads fail closed until it "
                f"is rebuilt: {corruption_detail}"
            ),
        ),
    )


def _active_lease_index_item(
    *,
    store_id: str,
    ref: str,
    health: Health,
    issue_code: str,
    role: tuple[str, ...],
    message: str,
) -> InventoryItem:
    item_id = f"recovery:active_lease_index:{store_id}"
    issue = InventoryIssue(
        id=issue_id(item_id, issue_code),
        code=issue_code,
        message=message,
        subject_id=item_id,
        locator=ref,
        recovery_hint=_ACTIVE_LEASE_INDEX_HINT,
    )
    return InventoryItem(
        id=item_id,
        domain="recovery",
        kind="active_lease_index",
        locator=ref,
        source_kind="git_ref",
        source_store="coordinator",
        health=health,
        role=role,
        fields={"world_store_id": store_id},
        source_identity={"ref": ref},
        issues=(issue,),
        # Tier-2: DIAGNOSTIC — a visible error that is neither a blocker (kind="active_lease_index"
        # is deliberately not a RecoveryKind; the runtime fail-closed read scopes the block to the
        # publish reader) nor auto-recoverable. Declared here; behavior is unchanged.
        disposition="diagnostic",
    )


def _ref_identity(store: Store, ref: str) -> dict[str, object]:
    identity: dict[str, object] = {"ref": ref, "exists": store.ref_exists(ref)}
    try:
        commit = store.resolve_to_commit(ref)
    except Exception:  # noqa: BLE001
        return identity
    if commit is not None:
        identity["ref_target_oid"] = str(commit.id)
    return identity


def _v2_scope_authority_refs(repo_path: str | Path) -> tuple[str, ...]:
    from vcs_core._world_storage_installation import default_world_storage_exists, open_existing_default_world_storage

    if not default_world_storage_exists(repo_path):
        return ()
    try:
        manager = open_existing_default_world_storage(repo_path)
    except Exception:  # noqa: BLE001
        return ()
    return tuple(sorted(ref for ref in manager.world_store.repo.references if ref.startswith("refs/vcscore/scopes/")))


def _file_identity(path: Path) -> dict[str, object]:
    identity: dict[str, object] = {"path": str(path)}
    try:
        stat = path.stat()
    except OSError:
        return identity
    identity.update({"file_size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    return identity
