"""Publication + retention state machine for the world-storage layer.

Extracted from ``WorldStorageManager`` (260704-1410-plan.md V2.2c / D-F: a single
controller — publication and retention are symmetrically coupled, no clean layer).
Dependencies are injected (4 state attrs + 4 outbound callables); the controller holds
no back-reference to WSM except the shared journal controller wired post-construction
(the genuine mutual reference). Method bodies moved byte-for-byte; WSM keeps delegation
shims for externally-referenced methods.
"""

from __future__ import annotations

import contextlib
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pygit2

from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._incremental import ActiveLeaseIndex, Health
from vcs_core._pygit2_helpers import require_commit
from vcs_core._transition_kernel_records import (
    RetainedRef,
    RetentionPolicyRequirement,
)
from vcs_core._world_closure import ClosureEvidenceRef, ClosureHead, ClosureWorld, WorldClosure, compute_world_closure
from vcs_core._world_publication_plan import PublicationPlan
from vcs_core._world_refs import (
    world_fork_origin_receipt_ref,
    world_pin_ref,
    world_publication_lease_index_ref,
    world_publication_lease_prefix,
    world_publication_lease_ref,
    world_retention_receipt_ref,
)
from vcs_core._world_retention import (
    CHILD_WORLD_RETENTION,
    EVIDENCE_REF,
    SELECTED_HEAD_PIN,
    validate_retained_ref,
)
from vcs_core._world_storage_records import (
    DEFAULT_GROUND_REF,
    _current_ref_target,
    _ForkOriginReceipt,
    _issue,
    _ProtectedRetention,
    _read_blob_bytes,
    _read_world_fork_origin_receipt,
    _required_payload_str,
    _validate_advance_basis,
    _world_operation_id,
    _world_selected_pins_are_authoritative,
)
from vcs_core._world_store import WorldStore, WorldValidationProfile
from vcs_core._world_types import (
    StructuredIssue,
    SubstrateHead,
    WorldCommit,
    canonical_bytes,
    canonical_digest,
    load_canonical_json,
)
from vcs_core.git_store import create_commit_with_recovery, create_or_update_reference, insert_tree_entry

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vcs_core._operation_journal_controller import OperationJournalController
    from vcs_core._substrate_store import SubstrateStore
    from vcs_core._world_operation_journal import (
        OperationJournalEntry,
        OperationJournalHistory,
    )
    from vcs_core._world_storage_records import (
        WorldFsckReport,
        _AuthorityLineageSegments,
    )
    from vcs_core._world_transition_coordinator import WorldTransitionCoordinator


WORLD_RETENTION_RECEIPT_SCHEMA = "vcscore/world-retention-receipt/v1"


WORLD_RETENTION_RECEIPT_PATH = "meta/world-retention-receipt.json"


WORLD_PUBLICATION_LEASE_SCHEMA = "vcscore/world-publication-lease/v1"


WORLD_PUBLICATION_LEASE_PATH = "meta/world-publication-lease.json"


@dataclass(frozen=True)
class PreparedPublication:
    """Publication side effects prepared before the authority-ref CAS."""

    plan: PublicationPlan
    lease_refs: tuple[str, ...]


@dataclass(frozen=True)
class _PublicationLease:
    authority_ref: str
    world_store_id: str
    world_oid: str
    operation_id: str
    created_at_unix_ns: int

    def to_json(self) -> dict[str, object]:
        payload = {
            "schema": WORLD_PUBLICATION_LEASE_SCHEMA,
            "authority_ref": self.authority_ref,
            "world_store_id": self.world_store_id,
            "world_oid": self.world_oid,
            "operation_id": self.operation_id,
            "created_at_unix_ns": self.created_at_unix_ns,
        }
        return {**payload, "lease_digest": canonical_digest(payload)}


def _classify_ref(
    result: dict[str, list[str]],
    repo: pygit2.Repository,
    *,
    ref: str,
    expected_oid: str,
    published: bool,
) -> None:
    try:
        target = repo.references[ref].target
    except KeyError:
        if published:
            result["missing_for_published_world"].append(ref)
        return
    if str(target) != expected_oid:
        result["corrupt"].append(ref)
    elif published:
        result["published"].append(ref)
    else:
        result["orphaned"].append(ref)


def _closure_refs_by_ref(
    closure: WorldClosure,
    *,
    stores: Mapping[str, SubstrateStore],
    world_store_id: str,
) -> dict[str, tuple[str, str, str | None]]:
    refs: dict[str, tuple[str, str, str | None]] = {}
    for head in closure.heads:
        if head.store_id in stores:
            refs[world_pin_ref(world_store_id, head.world_oid, head.binding)] = (
                head.store_id,
                head.head,
                head.world_oid,
            )
    for world in closure.worlds:
        if world.retention_ref is not None:
            refs[world.retention_ref] = ("__world_store__", world.oid, None)
    return refs


def _world_retention_receipt_payload(
    *,
    authority_ref: str,
    world_store_id: str,
    world_oid: str,
    closure: WorldClosure,
    retained_refs: tuple[str, ...],
) -> dict[str, object]:
    payload = {
        "schema": WORLD_RETENTION_RECEIPT_SCHEMA,
        "authority_ref": authority_ref,
        "world_store_id": world_store_id,
        "world_oid": world_oid,
        "closure_mode": "publish",
        "closure_digest": _closure_digest(closure),
        "retained_refs": sorted(retained_refs),
        "retained": [
            retained.to_json()
            for retained in _expected_retained_records_for_closure(closure, world_store_id=world_store_id)
        ],
    }
    return {**payload, "receipt_digest": canonical_digest(payload)}


def _world_fork_origin_receipt_payload(
    *,
    authority_ref: str,
    world_store_id: str,
    first_world_oid: str,
    forked_from_authority_ref: str,
    forked_from_world_oid: str,
) -> dict[str, object]:
    return _ForkOriginReceipt(
        authority_ref=authority_ref,
        world_store_id=world_store_id,
        first_world_oid=first_world_oid,
        forked_from_authority_ref=forked_from_authority_ref,
        forked_from_world_oid=forked_from_world_oid,
    ).to_json()


def _write_publication_lease(repo: pygit2.Repository, lease: _PublicationLease) -> pygit2.Oid:
    meta_builder = repo.TreeBuilder()
    insert_tree_entry(
        repo,
        meta_builder,
        "world-publication-lease.json",
        repo.create_blob(canonical_bytes(lease.to_json())),
        pygit2.GIT_FILEMODE_BLOB,
    )
    root_builder = repo.TreeBuilder()
    insert_tree_entry(repo, root_builder, "meta", meta_builder.write(), pygit2.GIT_FILEMODE_TREE)
    signature = pygit2.Signature("vcs-core world publication lease", "vcs-core@example.invalid")
    return create_commit_with_recovery(
        repo,
        None,
        signature,
        signature,
        f"world publication lease {lease.world_oid}",
        root_builder.write(),
        [],
    )


def _read_publication_lease(repo: pygit2.Repository, ref: str) -> _PublicationLease:
    try:
        target = repo.references[ref].target
    except KeyError as exc:
        raise KeyError(ref) from exc
    commit = require_commit(repo, pygit2.Oid(hex=str(target)), context="world publication lease")
    payload = load_canonical_json(_read_blob_bytes(repo, commit.tree, WORLD_PUBLICATION_LEASE_PATH))
    expected_keys = {
        "schema",
        "authority_ref",
        "world_store_id",
        "world_oid",
        "operation_id",
        "created_at_unix_ns",
        "lease_digest",
    }
    extra_keys = set(payload) - expected_keys
    if extra_keys:
        raise InvalidRepositoryStateError(f"unexpected publication lease fields: {sorted(extra_keys)!r}")
    missing_keys = expected_keys - set(payload)
    if missing_keys:
        raise InvalidRepositoryStateError(f"missing publication lease fields: {sorted(missing_keys)!r}")
    if payload.get("schema") != WORLD_PUBLICATION_LEASE_SCHEMA:
        raise InvalidRepositoryStateError(f"unsupported publication lease schema: {payload.get('schema')!r}")
    lease = _PublicationLease(
        authority_ref=_required_payload_str(payload, "publication lease", "authority_ref"),
        world_store_id=_required_payload_str(payload, "publication lease", "world_store_id"),
        world_oid=_required_payload_str(payload, "publication lease", "world_oid"),
        operation_id=_required_payload_str(payload, "publication lease", "operation_id"),
        created_at_unix_ns=_required_payload_int(payload, "publication lease", "created_at_unix_ns"),
    )
    if payload.get("lease_digest") != lease.to_json()["lease_digest"]:
        raise InvalidRepositoryStateError("publication lease digest disagrees with payload")
    return lease


def _expected_retained_refs_for_closure(closure: WorldClosure, *, world_store_id: str) -> tuple[str, ...]:
    refs = [world_pin_ref(world_store_id, head.world_oid, head.binding) for head in closure.heads]
    refs.extend(world.retention_ref for world in closure.worlds if world.retention_ref is not None)
    return tuple(sorted(refs))


def _expected_retained_records_for_closure(closure: WorldClosure, *, world_store_id: str) -> tuple[RetainedRef, ...]:
    retained: list[RetainedRef] = []
    for head in closure.heads:
        retained.append(
            RetainedRef(
                kind=SELECTED_HEAD_PIN,
                ref=world_pin_ref(world_store_id, head.world_oid, head.binding),
            ),
        )
    for world in closure.worlds:
        if world.retention_ref is None:
            continue
        retained.append(RetainedRef(kind=CHILD_WORLD_RETENTION, ref=world.retention_ref))
    for evidence_ref in closure.evidence_refs:
        retained.append(RetainedRef(kind=EVIDENCE_REF, ref=evidence_ref.ref, digest=evidence_ref.evidence_digest))
    by_digest = {canonical_digest(item.to_json()): item for item in retained}
    return tuple(by_digest[key] for key in sorted(by_digest))


def _closure_digest(closure: WorldClosure) -> str:
    return canonical_digest(
        {
            "root_world_oid": closure.root_world_oid,
            "worlds": [_closure_world_json(world) for world in closure.worlds],
            "heads": [_closure_head_json(head) for head in closure.heads],
            "evidence_refs": [_closure_evidence_ref_json(ref) for ref in closure.evidence_refs],
        },
    )


def _closure_world_json(world: ClosureWorld) -> dict[str, object]:
    return {
        "oid": world.oid,
        "path": world.path,
        "edge_kind": world.edge_kind,
        "binding": world.binding,
        "retention_ref": world.retention_ref,
    }


def _closure_head_json(head: ClosureHead) -> dict[str, object]:
    return {
        "world_oid": head.world_oid,
        "path": head.path,
        "binding": head.binding,
        "store_id": head.store_id,
        "head": head.head,
    }


def _closure_evidence_ref_json(ref: ClosureEvidenceRef) -> dict[str, object]:
    return {
        "world_oid": ref.world_oid,
        "path": ref.path,
        "binding": ref.binding,
        "ref": ref.ref,
        "evidence_digest": ref.evidence_digest,
    }


def _extend_retention_receipt_issues(
    issues: list[StructuredIssue],
    repo: pygit2.Repository,
    world_oid: str,
    *,
    authority_refs: tuple[str, ...],
    world_store_id: str,
    closure: WorldClosure,
) -> None:
    for authority_ref in authority_refs:
        if not _world_is_protected_by_authority(repo, world_oid, authority_ref):
            continue
        receipt_ref = world_retention_receipt_ref(authority_ref, world_oid)
        try:
            receipt = _read_world_retention_receipt(repo, receipt_ref)
        except KeyError:
            issues.append(
                _issue(
                    "missing_retention_receipt",
                    "published world is missing retention receipt",
                    world_oid=world_oid,
                    ref=receipt_ref,
                    recovery_hint="Recreate the retention receipt after verifying selected-head pins.",
                ),
            )
            continue
        except (InvalidRepositoryStateError, TypeError, ValueError) as exc:
            issues.append(
                _issue(
                    "corrupt_retention_receipt",
                    str(exc),
                    world_oid=world_oid,
                    ref=receipt_ref,
                    recovery_hint="Do not trust the corrupted receipt; verify pins before repair.",
                ),
            )
            continue
        expected = {
            "schema": WORLD_RETENTION_RECEIPT_SCHEMA,
            "authority_ref": authority_ref,
            "world_store_id": world_store_id,
            "world_oid": world_oid,
            "closure_mode": "publish",
            "closure_digest": _closure_digest(closure),
            "retained_refs": list(_expected_retained_refs_for_closure(closure, world_store_id=world_store_id)),
            "retained": [
                retained.to_json()
                for retained in _expected_retained_records_for_closure(closure, world_store_id=world_store_id)
            ],
        }
        for key, expected_value in expected.items():
            if receipt.get(key) != expected_value:
                issues.append(
                    _issue(
                        "corrupt_retention_receipt",
                        f"retention receipt {key} disagrees with world",
                        world_oid=world_oid,
                        ref=receipt_ref,
                        recovery_hint="Regenerate the receipt from the published world closure.",
                    ),
                )
                break


def _read_world_retention_receipt(repo: pygit2.Repository, ref: str) -> dict[str, object]:
    try:
        target = repo.references[ref].target
    except KeyError as exc:
        raise KeyError(ref) from exc
    commit = require_commit(repo, pygit2.Oid(hex=str(target)), context="world retention receipt")
    payload = load_canonical_json(_read_blob_bytes(repo, commit.tree, WORLD_RETENTION_RECEIPT_PATH))
    expected_keys = {
        "schema",
        "authority_ref",
        "world_store_id",
        "world_oid",
        "closure_mode",
        "closure_digest",
        "retained_refs",
        "retained",
        "receipt_digest",
    }
    extra_keys = set(payload) - expected_keys
    if extra_keys:
        raise InvalidRepositoryStateError(f"unexpected retention receipt fields: {sorted(extra_keys)!r}")
    missing_keys = expected_keys - set(payload)
    if missing_keys:
        raise InvalidRepositoryStateError(f"missing retention receipt fields: {sorted(missing_keys)!r}")
    if payload.get("schema") != WORLD_RETENTION_RECEIPT_SCHEMA:
        raise InvalidRepositoryStateError(f"unsupported retention receipt schema: {payload.get('schema')!r}")
    retained_refs = payload.get("retained_refs")
    if not isinstance(retained_refs, list) or not all(isinstance(item, str) and item for item in retained_refs):
        raise InvalidRepositoryStateError("retention receipt retained_refs must be a string list")
    if len(set(retained_refs)) != len(retained_refs):
        raise InvalidRepositoryStateError("retention receipt retained_refs must not contain duplicates")
    retained = payload.get("retained")
    if not isinstance(retained, list):
        raise InvalidRepositoryStateError("retention receipt retained must be a list")
    retained_seen: set[str] = set()
    for item in retained:
        try:
            retained_ref = RetainedRef.from_json(item)
            validate_retained_ref(retained_ref)
        except (TypeError, ValueError, InvalidRepositoryStateError) as exc:
            raise InvalidRepositoryStateError(f"retention receipt retained entry is invalid: {exc}") from exc
        retained_digest = canonical_digest(retained_ref.to_json())
        if retained_digest in retained_seen:
            raise InvalidRepositoryStateError("retention receipt retained entries must not contain duplicates")
        retained_seen.add(retained_digest)
    receipt_digest = payload.get("receipt_digest")
    unsigned = {key: value for key, value in payload.items() if key != "receipt_digest"}
    if receipt_digest != canonical_digest(unsigned):
        raise InvalidRepositoryStateError("retention receipt digest disagrees with payload")
    return payload


def _required_payload_int(payload: Mapping[str, object], label: str, key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidRepositoryStateError(f"{label} {key} must be an integer")
    return value


def _world_is_protected_by_authorities(
    repo: pygit2.Repository,
    world_oid: str,
    authority_refs: tuple[str, ...],
) -> bool:
    return any(_world_is_protected_by_authority(repo, world_oid, authority_ref) for authority_ref in authority_refs)


def _world_is_protected_by_authority(repo: pygit2.Repository, world_oid: str, authority_ref: str) -> bool:
    try:
        target = str(repo.references[authority_ref].target)
    except KeyError:
        return False
    if target == world_oid:
        return True
    try:
        return bool(repo.descendant_of(pygit2.Oid(hex=target), pygit2.Oid(hex=world_oid)))
    except (ValueError, TypeError, pygit2.GitError):
        return False


def _authority_world_targets(repo: pygit2.Repository, authority_refs: tuple[str, ...]) -> frozenset[str]:
    targets: set[str] = set()
    for ref in authority_refs:
        try:
            targets.add(str(repo.references[ref].target))
        except KeyError:
            continue
    return frozenset(targets)


def _publish_authority_refs(ref: str, authority_refs: tuple[str, ...] | None) -> tuple[str, ...]:
    refs = (ref,) if authority_refs is None else (ref, *authority_refs)
    return tuple(dict.fromkeys(refs))


def _delete_ref_if_targets(repo: pygit2.Repository, ref: str, oid: str) -> bool:
    if not _ref_targets(repo, ref, oid):
        return False
    result = subprocess.run(
        ["git", "update-ref", "-d", ref, oid],
        cwd=repo.path,
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode == 0:
        return True
    if not _ref_targets(repo, ref, oid):
        return False
    detail = (result.stderr or result.stdout or "git update-ref -d failed").strip()
    raise InvalidRepositoryStateError(f"failed to delete orphan retention ref {ref!r}: {detail}")


def _ref_targets(repo: pygit2.Repository, ref: str, oid: str) -> bool:
    try:
        return str(repo.references[ref].target) == oid
    except KeyError:
        return False


class PublicationRetentionController:
    """Owns the world publication and retention state machine.

    Covers publication (lease/plan/publish) and retention (closure/pin/receipt).
    Constructed by :class:`WorldStorageManager`.
    """

    def __init__(
        self,
        *,
        stores: Mapping[str, SubstrateStore],
        transition_coordinator: WorldTransitionCoordinator,
        world_store: WorldStore,
        authority_lineage_segments: Callable[[str, str], _AuthorityLineageSegments],
        input_world_lineage: Callable[[str], tuple[str, ...]],
        fsck_world: Callable[..., WorldFsckReport],
        read_operation_journal: Callable[..., OperationJournalHistory],
    ) -> None:
        self._stores = stores
        self._transition_coordinator = transition_coordinator
        self._world_store = world_store
        self._authority_lineage_segments = authority_lineage_segments
        self._input_world_lineage = input_world_lineage
        self.fsck_world = fsck_world
        self.read_operation_journal = read_operation_journal
        # Wired by the composition root after the journal controller exists
        # (genuine mutual reference; journal's publication validator is ours).
        self._journal: OperationJournalController = None  # type: ignore[assignment]

    def _active_lease_index(self) -> ActiveLeaseIndex:
        return ActiveLeaseIndex(
            self._world_store.repo,
            self._world_store.world_store_id,
            rebuild_source=self._scan_active_leases,
        )

    def _active_lease_targets_via_index(self) -> frozenset[str]:
        """Leased world oids via the durable accelerator.

        Missing index → fall back to the authoritative scan and self-heal for next
        time. Corrupt index → fail closed (``read_world_oids`` raises). This is the
        boundary-bounded hot-path read that replaces the O(total-refs) scan.
        """
        index = self._active_lease_index()
        members = index.read_world_oids()
        if members is not None:
            # We trust a *present* index without re-verifying against the authority. This is sound
            # ONLY because the consumer is a GC protection set and ActiveLeaseIndex.CONTRACT declares
            # read_safety="superset" / crash_lag="index-leads": staleness can only over-protect. A
            # future *exact*-read consumer MUST NOT copy this pattern — it must verify against the
            # authority or declare a different DerivedViewContract (see _incremental/_contract.py).
            return members
        targets = self._active_publication_lease_targets()
        with contextlib.suppress(InvalidRepositoryStateError):
            index.rebuild_from_durable_history()  # best-effort self-heal; the fallback already returned correct targets
        return targets

    def _active_publication_lease_refs(self) -> tuple[str, ...]:
        prefix = world_publication_lease_prefix() + "/"
        return tuple(sorted(ref for ref in self._world_store.repo.references if ref.startswith(prefix)))

    def _active_publication_lease_targets(self) -> frozenset[str]:
        targets: set[str] = set()
        for ref in self._active_publication_lease_refs():
            try:
                targets.add(_read_publication_lease(self._world_store.repo, ref).world_oid)
            except (InvalidRepositoryStateError, KeyError, TypeError, ValueError):
                continue
        return frozenset(targets)

    def _publication_lease_is_stale(self, lease: _PublicationLease, *, abandon_journalless: bool = False) -> bool:
        if lease.world_store_id != self._world_store.world_store_id:
            return False
        authority_refs = (lease.authority_ref,)
        if _world_is_protected_by_authorities(self._world_store.repo, lease.world_oid, authority_refs):
            return self.fsck_world(lease.world_oid, authority_refs=authority_refs).ok
        try:
            world = self._world_store.read_world_commit(lease.world_oid)
            operation_id = _world_operation_id(world)
        except (InvalidRepositoryStateError, KeyError, TypeError, ValueError):
            return False
        if operation_id != lease.operation_id:
            return False
        try:
            self.read_operation_journal(operation_id, family="archived")
        except (InvalidRepositoryStateError, KeyError, TypeError, ValueError):
            pass
        else:
            return True
        try:
            history = self.read_operation_journal(operation_id)
        except (InvalidRepositoryStateError, KeyError, TypeError, ValueError):
            return abandon_journalless
        return history.tip.payload.get("status") == "failed"

    def _publish_world(
        self,
        *,
        ref: str,
        world_oid: str,
        expected_oid: str | None,
        allow_same_resource_alias: bool,
        authority_refs: tuple[str, ...] | None,
    ) -> bool:
        plan = self.build_publication_plan(
            ref=ref,
            world_oid=world_oid,
            expected_oid=expected_oid,
            input_world_oid=expected_oid,
            allow_same_resource_alias=allow_same_resource_alias,
            authority_refs=authority_refs,
        )
        prepared = self.prepare_publication(plan)
        published = self.advance_publication(prepared)
        self.complete_publication(prepared)
        return published

    def _record_lease_index(self, *, add: tuple[str, str, str] | None = None, remove: str | None = None) -> None:
        """Update the active-lease accelerator around an authoritative lease ref change.

        On add this runs BEFORE the lease ref is created, and on release/cleanup AFTER the
        ref is deleted, so the index is always a superset of the live lease set. The lease
        refs are the authority, so a corrupt or contended accelerator must never block a
        publish: on failure we best-effort reset the index to *missing*, and the read path
        then falls back to the authoritative scan and self-heals — we never leave or trust a
        stale subset.
        """
        index = self._active_lease_index()
        try:
            if add is not None:
                lease_ref, world_oid, operation_id = add
                index.add(lease_ref, world_oid=world_oid, operation_id=operation_id)
            if remove is not None:
                index.remove(remove)
        except InvalidRepositoryStateError:
            self._reset_lease_index()

    def _release_publication_leases(self, lease_refs: tuple[str, ...], *, world_oid: str) -> None:
        for lease_ref in lease_refs:
            try:
                lease_target = _current_ref_target(self._world_store.repo, lease_ref)
                if lease_target is None:
                    continue
                lease = _read_publication_lease(self._world_store.repo, lease_ref)
                if lease.world_oid != world_oid:
                    continue
                _delete_ref_if_targets(self._world_store.repo, lease_ref, lease_target)
                self._record_lease_index(remove=lease_ref)
            except InvalidRepositoryStateError:
                continue

    def _reset_lease_index(self) -> None:
        """Best-effort drop of the accelerator so the next read falls back to the authority.

        Called only to recover from an accelerator write that already failed, so it must
        never raise — otherwise a derived-view hiccup becomes a blocked publish, and the
        accelerator is never authority. An already-absent ref (``KeyError``) or a lower-level
        pygit2 / OS deletion failure is swallowed: worst case the index stays corrupt and the
        read path fails closed on it (surfaced by fsck, repaired by ``rebuild_active_lease_index``),
        but the publish on the authoritative lease refs proceeds.
        """
        ref = world_publication_lease_index_ref(self._world_store.world_store_id)
        with contextlib.suppress(KeyError, pygit2.GitError, OSError):
            self._world_store.repo.references.delete(ref)

    def _scan_active_leases(self) -> dict[str, dict[str, str]]:
        """Authoritative active-lease entries (the full ref-namespace scan; rebuild oracle)."""
        entries: dict[str, dict[str, str]] = {}
        for lease_ref in self._active_publication_lease_refs():
            try:
                lease = _read_publication_lease(self._world_store.repo, lease_ref)
            except (InvalidRepositoryStateError, KeyError, TypeError, ValueError):
                continue
            entries[lease_ref] = {"world_oid": lease.world_oid, "operation_id": lease.operation_id}
        return entries

    def _validate_publication_plan(
        self,
        plan: PublicationPlan,
        *,
        expected_world_oid: str | None = None,
        expected_authority_ref: str | None = None,
        expected_input_world_oid: str | None = None,
    ) -> None:
        if plan.world_store_id != self._world_store.world_store_id:
            raise InvalidRepositoryStateError("publication plan world_store_id disagrees with manager")
        if expected_world_oid is not None and plan.world_oid != expected_world_oid:
            raise InvalidRepositoryStateError("publication plan world_oid disagrees with operation journal")
        if expected_authority_ref is not None and plan.authority_ref != expected_authority_ref:
            raise InvalidRepositoryStateError("publication plan authority_ref disagrees with operation journal")
        if expected_input_world_oid is not None and plan.input_world_oid != expected_input_world_oid:
            raise InvalidRepositoryStateError("publication plan input_world_oid disagrees with operation journal")
        if plan.authority_refs[0] != plan.authority_ref:
            raise InvalidRepositoryStateError("publication plan authority_refs must start with authority_ref")
        if plan.authority_refs != _publish_authority_refs(plan.authority_ref, plan.authority_refs[1:]):
            raise InvalidRepositoryStateError("publication plan authority_refs are not canonical")
        world = self._world_store.read_world_commit(plan.world_oid)
        if plan.input_world_oid is None:
            if plan.expected_oid is not None:
                raise InvalidRepositoryStateError("root publication plan expected_oid must be null")
            if world.parent_oids:
                raise InvalidRepositoryStateError("root publication plan requires an unparented world")
            if world.transition.get("input_world") is not None:
                raise InvalidRepositoryStateError("root publication plan requires no input_world")
            return
        if plan.expected_oid != plan.input_world_oid:
            raise InvalidRepositoryStateError("advance publication plan expected_oid must equal input_world_oid")
        _validate_advance_basis(world, input_world_oid=plan.input_world_oid)

    def _world_is_protected_by_publication_lease(self, oid: str) -> bool:
        for leased_world_oid in self._active_publication_lease_targets():
            try:
                closure = self.compute_publish_retention_closure(leased_world_oid)
            except (InvalidRepositoryStateError, KeyError, TypeError, ValueError):
                if leased_world_oid == oid:
                    return True
                continue
            if any(world.oid == oid for world in closure.worlds):
                return True
        return False

    def _write_publication_leases(self, authority_refs: tuple[str, ...], world: WorldCommit) -> tuple[str, ...]:
        operation_id = _world_operation_id(world)
        lease_refs: list[str] = []
        for authority_ref in authority_refs:
            lease_ref = world_publication_lease_ref(authority_ref, world.oid, operation_id)
            current = _current_ref_target(self._world_store.repo, lease_ref)
            if current is not None:
                lease = _read_publication_lease(self._world_store.repo, lease_ref)
                if lease.world_oid != world.oid or lease.authority_ref != authority_ref:
                    raise InvalidRepositoryStateError("publication lease ref targets a different publication")
            # The accelerator must LEAD the authoritative lease ref on creation: a crash
            # between this index update and the ref create then leaves the index a
            # SUPERSET of the live lease set (over-protect — conservative/safe), never a
            # subset (under-protect — the corrupting direction). Releases tombstone AFTER
            # the ref delete, for the same superset reason.
            self._record_lease_index(add=(lease_ref, world.oid, operation_id))
            if current is None:
                lease_oid = _write_publication_lease(
                    self._world_store.repo,
                    _PublicationLease(
                        authority_ref=authority_ref,
                        world_store_id=self._world_store.world_store_id,
                        world_oid=world.oid,
                        operation_id=operation_id,
                        created_at_unix_ns=time.time_ns(),
                    ),
                )
                create_or_update_reference(self._world_store.repo, lease_ref, lease_oid)
            lease_refs.append(lease_ref)
        return tuple(lease_refs)

    def active_lease_index_corruption(self) -> str | None:
        """Cheap, index-only corruption check for the readiness/recovery hot path.

        Reads ONLY the index record (one blob; **no** authoritative ref scan), so it stays off the
        O(total-refs) scan the lease index exists to avoid. Returns the corruption detail if the
        present index is unreadable/corrupt, else ``None`` (missing, or present-and-self-consistent).
        Stale-vs-authority detection needs the full scan and lives in
        :meth:`verify_active_lease_index` (deep fsck only), never on readiness.
        """
        try:
            self._active_lease_index().read_world_oids()
        except InvalidRepositoryStateError as exc:
            return str(exc)
        return None

    def advance_publication(self, prepared: PreparedPublication) -> bool:
        return self._world_store._publish_ref_unchecked(
            prepared.plan.authority_ref,
            prepared.plan.world_oid,
            prepared.plan.expected_oid,
        )

    def build_advance_publication_plan(
        self,
        *,
        ref: str,
        world_oid: str,
        expected_oid: str,
        input_world_oid: str,
        allow_same_resource_alias: bool = False,
        authority_refs: tuple[str, ...] | None = None,
    ) -> PublicationPlan:
        world = self._world_store.read_world_commit(world_oid)
        _validate_advance_basis(world, input_world_oid=input_world_oid)
        if expected_oid != input_world_oid:
            raise InvalidRepositoryStateError("advance publication expected_oid must equal input_world_oid")
        return self.build_publication_plan(
            ref=ref,
            world_oid=world_oid,
            expected_oid=expected_oid,
            input_world_oid=input_world_oid,
            allow_same_resource_alias=allow_same_resource_alias,
            authority_refs=authority_refs,
        )

    def build_publication_plan(
        self,
        *,
        ref: str,
        world_oid: str,
        expected_oid: str | None,
        input_world_oid: str | None,
        allow_same_resource_alias: bool = False,
        authority_refs: tuple[str, ...] | None = None,
    ) -> PublicationPlan:
        resolved_authority_refs = _publish_authority_refs(ref, authority_refs)
        return PublicationPlan(
            authority_ref=ref,
            authority_refs=resolved_authority_refs,
            world_store_id=self._world_store.world_store_id,
            world_oid=world_oid,
            expected_oid=expected_oid,
            input_world_oid=input_world_oid,
            allow_same_resource_alias=allow_same_resource_alias,
        )

    def build_root_publication_plan(
        self,
        *,
        ref: str,
        world_oid: str,
        allow_same_resource_alias: bool = False,
        authority_refs: tuple[str, ...] | None = None,
    ) -> PublicationPlan:
        world = self._world_store.read_world_commit(world_oid)
        if world.parent_oids:
            raise InvalidRepositoryStateError("root world publication requires an unparented world")
        if world.transition.get("input_world") is not None:
            raise InvalidRepositoryStateError("root world publication requires no input_world")
        return self.build_publication_plan(
            ref=ref,
            world_oid=world_oid,
            expected_oid=None,
            input_world_oid=None,
            allow_same_resource_alias=allow_same_resource_alias,
            authority_refs=authority_refs,
        )

    def cleanup_stale_publication_leases(
        self,
        *,
        authority_refs: tuple[str, ...] = (DEFAULT_GROUND_REF,),
        abandon_journalless: bool = False,
    ) -> tuple[str, ...]:
        del authority_refs
        deleted: list[str] = []
        for lease_ref in self._active_publication_lease_refs():
            lease_target = _current_ref_target(self._world_store.repo, lease_ref)
            if lease_target is None:
                continue
            try:
                lease = _read_publication_lease(self._world_store.repo, lease_ref)
            except (InvalidRepositoryStateError, KeyError, TypeError, ValueError):
                continue
            if not self._publication_lease_is_stale(lease, abandon_journalless=abandon_journalless):
                continue
            if _delete_ref_if_targets(self._world_store.repo, lease_ref, lease_target):
                deleted.append(lease_ref)
                self._record_lease_index(remove=lease_ref)
        return tuple(deleted)

    def complete_publication(self, prepared: PreparedPublication) -> None:
        self._release_publication_leases(prepared.lease_refs, world_oid=prepared.plan.world_oid)

    def compute_publish_retention_closure(self, oid: str) -> WorldClosure:
        return compute_world_closure(
            self._world_store,
            oid,
            self._stores,
            closure_mode="publish",
        )

    def prepare_publication(self, plan: PublicationPlan) -> PreparedPublication:
        self._validate_publication_plan(plan)
        # Trust-by-default (260623-0640-plan.md, Part A): the prior-lineage retention
        # re-validation (_validate_authority_retention_preflight) is OFF the publish hot path.
        # It re-walked every prior world's publish closure on each publish (2N-1 closure
        # computations, Sigma = N^2) and was redundant with the durable pins/receipts written
        # below. The detector survives and now runs on demand in fsck_world(mode="deep") (Part B).
        closure = self.validate_publish_closure(
            plan.world_oid,
            authority_refs=plan.authority_refs,
            allow_same_resource_alias=plan.allow_same_resource_alias,
        )
        world = self._world_store.read_world_commit(plan.world_oid)
        lease_refs = self._write_publication_leases((plan.authority_ref,), world)
        retained_refs = self.pin_world_closure(closure)
        self.write_world_retention_receipt(
            authority_ref=plan.authority_ref,
            world_oid=plan.world_oid,
            closure=closure,
            retained_refs=retained_refs,
        )
        return PreparedPublication(plan=plan, lease_refs=lease_refs)

    def publish_root_world(
        self,
        *,
        ref: str,
        world_oid: str,
        allow_same_resource_alias: bool = False,
        authority_refs: tuple[str, ...] | None = None,
    ) -> bool:
        plan = self.build_root_publication_plan(
            ref=ref,
            world_oid=world_oid,
            allow_same_resource_alias=allow_same_resource_alias,
            authority_refs=authority_refs,
        )
        prepared = self.prepare_publication(plan)
        published = self.advance_publication(prepared)
        self.complete_publication(prepared)
        return published

    def rebuild_active_lease_index(self) -> None:
        """Rebuild the active-lease accelerator from the authoritative lease refs (recovery self-heal)."""
        self._active_lease_index().rebuild_from_durable_history()

    def record_operation_published(
        self,
        operation_id: str,
        *,
        world_oid: str,
    ) -> OperationJournalEntry:
        return self._journal.record_operation_published(operation_id, world_oid=world_oid)

    def record_operation_publishing(
        self,
        operation_id: str,
        *,
        world_oid: str,
        publication_plan: PublicationPlan,
    ) -> OperationJournalEntry:
        return self._journal.record_operation_publishing(
            operation_id,
            world_oid=world_oid,
            publication_plan=publication_plan,
        )

    def validate_publish_closure(
        self,
        oid: str,
        *,
        authority_refs: tuple[str, ...] = (),
        allow_same_resource_alias: bool = False,
    ) -> WorldClosure:
        """Validate the semantic closure needed to publish one new world."""
        closure = self.compute_publish_retention_closure(oid)
        pin_classification = self.classify_world_closure_retention(closure, authority_refs=authority_refs)
        protected_retention = self._protected_retention(authority_refs)
        for world in closure.worlds:
            selected_pins_are_authoritative = _world_selected_pins_are_authoritative(
                closure,
                world_store_id=self._world_store.world_store_id,
                world_oid=world.oid,
                protected_world_oids=protected_retention.world_oids,
                pin_classification=pin_classification,
            )
            self._world_store.validate_world_commit(
                world.oid,
                self._stores,
                allow_same_resource_alias=allow_same_resource_alias,
                require_selected_candidate_refs=not selected_pins_are_authoritative,
                validate_input_worlds=False,
                profile=WorldValidationProfile.DEEP,
            )
        return closure

    def verify_active_lease_index(self) -> Health:
        """Deep health (fsck only): is the accelerator consistent with the authoritative lease refs?

        ``fresh`` iff the live index reproduces the full-scan authority bit-for-bit;
        ``missing`` (fallback exists, not a blocker), ``corrupt``, or ``stale`` otherwise.
        Performs the authoritative full ref scan, so it must NOT run on the readiness/recovery
        hot path — use :meth:`active_lease_index_corruption` there. Never mutates.
        """
        return self._active_lease_index().verify_against_authority()

    def _expected_refs_for_closure(self, closure: WorldClosure) -> dict[str, tuple[str, str, str | None]]:
        refs = _closure_refs_by_ref(closure, stores=self._stores, world_store_id=self._world_store.world_store_id)
        for world in closure.worlds:
            semantic = self.compute_world_closure(world.oid)
            refs.update(
                _closure_refs_by_ref(semantic, stores=self._stores, world_store_id=self._world_store.world_store_id),
            )
        return refs

    def _extend_authority_lineage_retention_receipt_issues(
        self,
        issues: list[StructuredIssue],
        oid: str,
        *,
        authority_refs: tuple[str, ...],
    ) -> None:
        for authority_ref in authority_refs:
            try:
                authority_target = _current_ref_target(self._world_store.repo, authority_ref)
                if authority_target is None or oid not in self._input_world_lineage(authority_target):
                    continue
                self._extend_retention_receipt_issues_for_authority_lineage(
                    issues,
                    oid,
                    authority_ref=authority_ref,
                    seen=frozenset(),
                )
            except (InvalidRepositoryStateError, KeyError, TypeError, ValueError) as exc:
                issues.append(_issue("retention_receipt_check_failed", str(exc), world_oid=oid))

    def _extend_retention_receipt_issues_for_authority_lineage(
        self,
        issues: list[StructuredIssue],
        oid: str,
        *,
        authority_ref: str,
        seen: frozenset[tuple[str, str]],
    ) -> None:
        lineage_key = (authority_ref, oid)
        if lineage_key in seen:
            issues.append(
                _issue("corrupt_fork_origin_receipt", "authority fork lineage contains a cycle", world_oid=oid),
            )
            return
        seen = seen | {lineage_key}
        lineage = self._authority_lineage_segments(authority_ref, oid)
        if lineage.corrupt_fork_origin is not None:
            issues.append(
                _issue(
                    "corrupt_fork_origin_receipt",
                    lineage.corrupt_fork_origin,
                    world_oid=oid,
                    ref=world_fork_origin_receipt_ref(authority_ref),
                ),
            )
            return
        for closure in tuple(
            self.compute_publish_retention_closure(world_oid) for world_oid in lineage.local_world_oids
        ):
            _extend_retention_receipt_issues(
                issues,
                self._world_store.repo,
                closure.root_world_oid,
                authority_refs=(authority_ref,),
                world_store_id=self._world_store.world_store_id,
                closure=closure,
            )
        inherited = lineage.fork_origin
        if inherited is None:
            return
        inherited_oid = inherited.forked_from_world_oid
        if not _world_is_protected_by_authority(
            self._world_store.repo,
            inherited_oid,
            inherited.forked_from_authority_ref,
        ):
            issues.append(
                _issue(
                    "corrupt_fork_origin_receipt",
                    "fork origin authority no longer protects inherited world",
                    world_oid=oid,
                    ref=world_fork_origin_receipt_ref(authority_ref),
                ),
            )
            return
        self._extend_retention_receipt_issues_for_authority_lineage(
            issues,
            inherited_oid,
            authority_ref=inherited.forked_from_authority_ref,
            seen=seen,
        )

    def _protected_retention(self, authority_refs: tuple[str, ...]) -> _ProtectedRetention:
        world_oids: set[str] = set()
        refs: set[str] = set()
        for world_oid in _authority_world_targets(self._world_store.repo, authority_refs):
            closure = self.compute_resume_retention_closure(world_oid)
            world_oids.update(world.oid for world in closure.worlds)
        for world_oid in self._active_lease_targets_via_index():
            closure = self.compute_publish_retention_closure(world_oid)
            world_oids.update(world.oid for world in closure.worlds)
            refs.update(_expected_retained_refs_for_closure(closure, world_store_id=self._world_store.world_store_id))
        pending = list(world_oids)
        while pending:
            world_oid = pending.pop()
            semantic = self.compute_world_closure(world_oid)
            for world in semantic.worlds:
                if world.oid not in world_oids:
                    world_oids.add(world.oid)
                    pending.append(world.oid)
                if world.retention_ref is not None:
                    refs.add(world.retention_ref)
        return _ProtectedRetention(world_oids=frozenset(world_oids), refs=frozenset(refs))

    def _selection_retention_policy_requirements(
        self,
        head: SubstrateHead,
        *,
        explicit_requirements: tuple[RetentionPolicyRequirement, ...] = (),
    ) -> tuple[RetentionPolicyRequirement, ...]:
        return self._transition_coordinator.selection_retention_policy_requirements(
            head,
            explicit_requirements=explicit_requirements,
        )

    def _validate_authority_lineage_retention(
        self,
        authority_ref: str,
        world_oid: str,
        *,
        allow_same_resource_alias: bool,
        seen: frozenset[tuple[str, str]],
    ) -> None:
        lineage_key = (authority_ref, world_oid)
        if lineage_key in seen:
            raise InvalidRepositoryStateError("authority fork lineage contains a cycle")
        seen = seen | {lineage_key}
        lineage = self._authority_lineage_segments(authority_ref, world_oid)
        if lineage.corrupt_fork_origin is not None:
            raise InvalidRepositoryStateError(lineage.corrupt_fork_origin)
        for lineage_world_oid in lineage.local_world_oids:
            closure = self.compute_publish_retention_closure(lineage_world_oid)
            self._validate_retained_refs_exist(
                closure,
                allow_same_resource_alias=allow_same_resource_alias,
                authority_ref=authority_ref,
                validate_worlds=True,
            )
        issues: list[StructuredIssue] = []
        for lineage_world_oid in lineage.local_world_oids:
            closure = self.compute_publish_retention_closure(lineage_world_oid)
            _extend_retention_receipt_issues(
                issues,
                self._world_store.repo,
                lineage_world_oid,
                authority_refs=(authority_ref,),
                world_store_id=self._world_store.world_store_id,
                closure=closure,
            )
        if issues:
            raise InvalidRepositoryStateError(issues[0].message)
        inherited = lineage.fork_origin
        if inherited is None:
            return
        inherited_oid = inherited.forked_from_world_oid
        if not _world_is_protected_by_authority(
            self._world_store.repo,
            inherited_oid,
            inherited.forked_from_authority_ref,
        ):
            raise InvalidRepositoryStateError("fork origin authority no longer protects inherited world")
        self._validate_authority_lineage_retention(
            inherited.forked_from_authority_ref,
            inherited_oid,
            allow_same_resource_alias=allow_same_resource_alias,
            seen=seen,
        )

    def _validate_authority_retention_preflight(
        self,
        authority_refs: tuple[str, ...],
        *,
        allow_same_resource_alias: bool,
    ) -> None:
        if not authority_refs:
            return
        seen_worlds: set[str] = set()
        for authority_ref in authority_refs:
            world_oid = _current_ref_target(self._world_store.repo, authority_ref)
            if world_oid is None or world_oid in seen_worlds:
                continue
            seen_worlds.add(world_oid)
            try:
                self._validate_authority_lineage_retention(
                    authority_ref,
                    world_oid,
                    allow_same_resource_alias=allow_same_resource_alias,
                    seen=frozenset(),
                )
            except (InvalidRepositoryStateError, KeyError, TypeError, ValueError) as exc:
                raise InvalidRepositoryStateError(
                    f"authority retention preflight failed for {authority_ref!r}: {exc}",
                ) from exc

    def _validate_retained_refs_exist(
        self,
        closure: WorldClosure,
        *,
        allow_same_resource_alias: bool,
        authority_ref: str,
        validate_worlds: bool = True,
    ) -> None:
        try:
            if validate_worlds:
                for world in closure.worlds:
                    self._world_store.validate_world_commit(
                        world.oid,
                        self._stores,
                        allow_same_resource_alias=allow_same_resource_alias,
                        require_selected_candidate_refs=False,
                        validate_input_worlds=False,
                        profile=WorldValidationProfile.DEEP,
                    )
            for ref, (owner_id, expected_oid, _world_oid) in self._expected_refs_for_closure(closure).items():
                repo = self._world_store.repo if owner_id == "__world_store__" else self._stores[owner_id].repo
                target = _current_ref_target(repo, ref)
                if target is None:
                    raise InvalidRepositoryStateError("published world is missing retained refs")
                if target != expected_oid:
                    raise InvalidRepositoryStateError("published world has corrupt retained refs")
        except (InvalidRepositoryStateError, KeyError, TypeError, ValueError) as exc:
            raise InvalidRepositoryStateError(
                f"authority retention preflight failed for {authority_ref!r}: {exc}",
            ) from exc

    def classify_world_closure_retention(
        self,
        closure: WorldClosure,
        *,
        authority_refs: tuple[str, ...] = (DEFAULT_GROUND_REF,),
    ) -> dict[str, tuple[str, ...]]:
        protected_retention = self._protected_retention(authority_refs)
        expected_refs = self._expected_refs_for_closure(closure)
        result: dict[str, list[str]] = {
            "published": [],
            "orphaned": [],
            "missing_for_published_world": [],
            "corrupt": [],
        }
        for ref, (owner_id, expected_oid, world_oid) in expected_refs.items():
            repo = self._world_store.repo if owner_id == "__world_store__" else self._stores[owner_id].repo
            published = (
                ref in protected_retention.refs
                if owner_id == "__world_store__"
                else world_oid in protected_retention.world_oids
            )
            _classify_ref(
                result,
                repo,
                ref=ref,
                expected_oid=expected_oid,
                published=published,
            )
        return {key: tuple(values) for key, values in result.items()}

    def cleanup_orphan_pins(
        self,
        oid: str,
        *,
        authority_refs: tuple[str, ...] = (DEFAULT_GROUND_REF,),
    ) -> tuple[str, ...]:
        closure = self.compute_publish_retention_closure(oid)
        classification = self.classify_world_closure_retention(closure, authority_refs=authority_refs)
        refs_by_ref = _closure_refs_by_ref(
            closure,
            stores=self._stores,
            world_store_id=self._world_store.world_store_id,
        )
        deleted: list[str] = []
        for ref in classification["orphaned"]:
            owner = refs_by_ref.get(ref)
            if owner is None:
                continue
            owner_id, expected_oid, _world_oid = owner
            repo = self._world_store.repo if owner_id == "__world_store__" else self._stores[owner_id].repo
            if _delete_ref_if_targets(repo, ref, expected_oid):
                deleted.append(ref)
        if not _world_is_protected_by_authorities(
            self._world_store.repo,
            oid,
            authority_refs,
        ) and not self._world_is_protected_by_publication_lease(oid):
            for authority_ref in authority_refs:
                receipt_ref = world_retention_receipt_ref(authority_ref, oid)
                target = _current_ref_target(self._world_store.repo, receipt_ref)
                if target is not None and _delete_ref_if_targets(self._world_store.repo, receipt_ref, target):
                    deleted.append(receipt_ref)
                fork_ref = world_fork_origin_receipt_ref(authority_ref)
                fork_target = _current_ref_target(self._world_store.repo, fork_ref)
                if fork_target is None:
                    continue
                try:
                    fork_origin = _read_world_fork_origin_receipt(self._world_store.repo, fork_ref)
                except (InvalidRepositoryStateError, KeyError, TypeError, ValueError):
                    continue
                if fork_origin.first_world_oid == oid and _delete_ref_if_targets(
                    self._world_store.repo,
                    fork_ref,
                    fork_target,
                ):
                    deleted.append(fork_ref)
        return tuple(deleted)

    def cleanup_stale_terminal_operation_open_ref(self, operation_id: str, *, terminal_family: str) -> bool:
        return self._journal.cleanup_stale_terminal_operation_open_ref(operation_id, terminal_family=terminal_family)

    def compute_resume_retention_closure(self, oid: str) -> WorldClosure:
        return compute_world_closure(
            self._world_store,
            oid,
            self._stores,
            closure_mode="authority",
        )

    def compute_world_closure(self, oid: str) -> WorldClosure:
        return compute_world_closure(self._world_store, oid, self._stores)

    def pin_resume_retention_closure(self, closure: WorldClosure) -> tuple[str, ...]:
        retained = list(self.pin_world_closure(closure))
        seen_refs: set[str] = set(retained)
        for world in closure.worlds:
            semantic = self.compute_world_closure(world.oid)
            for semantic_world in semantic.worlds:
                if semantic_world.retention_ref is None or semantic_world.retention_ref in seen_refs:
                    continue
                create_or_update_reference(
                    self._world_store.repo,
                    semantic_world.retention_ref,
                    pygit2.Oid(hex=semantic_world.oid),
                    force=True,
                )
                retained.append(semantic_world.retention_ref)
                seen_refs.add(semantic_world.retention_ref)
        return tuple(retained)

    def pin_world_closure(self, closure: WorldClosure) -> tuple[str, ...]:
        retained: list[str] = []
        for head in closure.heads:
            store = self._stores[head.store_id]
            retained.append(
                store.pin_world_head(
                    world_store_id=self._world_store.world_store_id,
                    world_oid=head.world_oid,
                    binding=head.binding,
                    head=head.head,
                ),
            )
        for world in closure.worlds:
            if world.retention_ref is None:
                continue
            create_or_update_reference(
                self._world_store.repo,
                world.retention_ref,
                pygit2.Oid(hex=world.oid),
                force=True,
            )
            retained.append(world.retention_ref)
        return tuple(retained)

    def repin_world_retention(self, oid: str) -> tuple[str, ...]:
        """Repair a published world's retention by re-pinning its authority closure.

        The trust-by-default on-demand repair (260623-0640-plan.md, Part B) for a broken
        prior-lineage pin that deep fsck flagged as ``missing_selected_head_pins``: re-derive the
        world's authority closure (which transitively includes the ancestor lineage) and re-pin every
        head and retention ref from the immutable world commits. It does not repair an authority
        rewrite (a moved fork-origin parent, ``corrupt_fork_origin_receipt``) — that is a separate,
        higher recovery, not a missing pin.
        """
        closure = self.compute_resume_retention_closure(oid)
        return self.pin_resume_retention_closure(closure)

    def validate_world_closure(
        self,
        oid: str,
        *,
        authority_refs: tuple[str, ...] = (),
        allow_same_resource_alias: bool = False,
    ) -> WorldClosure:
        """Validate every world reachable through the root's required recursive closure."""
        closure = self.compute_resume_retention_closure(oid)
        pin_classification = self.classify_world_closure_retention(closure, authority_refs=authority_refs)
        protected_retention = self._protected_retention(authority_refs)
        for world in closure.worlds:
            selected_pins_are_authoritative = _world_selected_pins_are_authoritative(
                closure,
                world_store_id=self._world_store.world_store_id,
                world_oid=world.oid,
                protected_world_oids=protected_retention.world_oids,
                pin_classification=pin_classification,
            )
            self._world_store.validate_world_commit(
                world.oid,
                self._stores,
                allow_same_resource_alias=allow_same_resource_alias,
                require_selected_candidate_refs=not selected_pins_are_authoritative,
                validate_input_worlds=False,
                profile=WorldValidationProfile.DEEP,
            )
        return closure

    def write_world_fork_origin_receipt(
        self,
        *,
        authority_ref: str,
        first_world_oid: str,
        forked_from_authority_ref: str,
        forked_from_world_oid: str,
    ) -> str:
        ref = world_fork_origin_receipt_ref(authority_ref)
        payload = _world_fork_origin_receipt_payload(
            authority_ref=authority_ref,
            world_store_id=self._world_store.world_store_id,
            first_world_oid=first_world_oid,
            forked_from_authority_ref=forked_from_authority_ref,
            forked_from_world_oid=forked_from_world_oid,
        )
        try:
            existing = _read_world_fork_origin_receipt(self._world_store.repo, ref)
        except KeyError:
            pass
        else:
            if existing.to_json() != payload:
                raise InvalidRepositoryStateError("fork origin receipt already exists for a different origin")
            return ref
        meta_builder = self._world_store.repo.TreeBuilder()
        insert_tree_entry(
            self._world_store.repo,
            meta_builder,
            "world-fork-origin-receipt.json",
            self._world_store.repo.create_blob(canonical_bytes(payload)),
            pygit2.GIT_FILEMODE_BLOB,
        )
        root_builder = self._world_store.repo.TreeBuilder()
        insert_tree_entry(self._world_store.repo, root_builder, "meta", meta_builder.write(), pygit2.GIT_FILEMODE_TREE)
        signature = pygit2.Signature("vcs-core world fork", "vcs-core@example.invalid")
        receipt_oid = create_commit_with_recovery(
            self._world_store.repo,
            None,
            signature,
            signature,
            f"world fork origin receipt {authority_ref}",
            root_builder.write(),
            [],
        )
        create_or_update_reference(self._world_store.repo, ref, receipt_oid, force=True)
        return ref

    def write_world_retention_receipt(
        self,
        *,
        authority_ref: str,
        world_oid: str,
        closure: WorldClosure,
        retained_refs: tuple[str, ...],
    ) -> str:
        expected_refs = _expected_retained_refs_for_closure(
            closure,
            world_store_id=self._world_store.world_store_id,
        )
        if tuple(sorted(retained_refs)) != expected_refs:
            raise InvalidRepositoryStateError("retention receipt retained refs disagree with publish closure")
        ref = world_retention_receipt_ref(authority_ref, world_oid)
        payload = _world_retention_receipt_payload(
            authority_ref=authority_ref,
            world_store_id=self._world_store.world_store_id,
            world_oid=world_oid,
            closure=closure,
            retained_refs=retained_refs,
        )
        meta_builder = self._world_store.repo.TreeBuilder()
        insert_tree_entry(
            self._world_store.repo,
            meta_builder,
            "world-retention-receipt.json",
            self._world_store.repo.create_blob(canonical_bytes(payload)),
            pygit2.GIT_FILEMODE_BLOB,
        )
        root_builder = self._world_store.repo.TreeBuilder()
        insert_tree_entry(self._world_store.repo, root_builder, "meta", meta_builder.write(), pygit2.GIT_FILEMODE_TREE)
        signature = pygit2.Signature("vcs-core world retention", "vcs-core@example.invalid")
        receipt_oid = create_commit_with_recovery(
            self._world_store.repo,
            None,
            signature,
            signature,
            f"world retention receipt {world_oid}",
            root_builder.write(),
            [],
        )
        create_or_update_reference(self._world_store.repo, ref, receipt_oid, force=True)
        return ref
