"""Internal durable projection storage for narrow Git-backed read accelerators."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Literal, cast

import pygit2

from vcs_core._pygit2_helpers import lookup_path, require_blob, require_commit, sorted_tree_entries, topological_commits
from vcs_core.git_store import (
    build_tree,
    create_commit_with_recovery,
    create_or_update_reference,
    create_signature,
    set_reference_target,
)

ProjectionCarrierKind = Literal["archived_operation_ref", "discarded_world_ref"]

PROJECTION_REF_PREFIX = "refs/vcscore/projections"
ARCHIVED_OPERATIONS_BY_ID_FAMILY = "archived-operations-by-id"
ARCHIVED_OPERATIONS_BY_ID_CURRENT_REF = f"{PROJECTION_REF_PREFIX}/{ARCHIVED_OPERATIONS_BY_ID_FAMILY}/current"
ARCHIVED_OPERATIONS_BY_ID_VERSION = 1
SCOPE_REGISTRY_FAMILY = "scope-registry"
SCOPE_REGISTRY_CURRENT_REF = f"{PROJECTION_REF_PREFIX}/{SCOPE_REGISTRY_FAMILY}/current"
SCOPE_REGISTRY_VERSION = 1


@dataclass(frozen=True)
class ProjectionCarrier:
    ref: str
    tip_oid: str
    carrier_kind: ProjectionCarrierKind


@dataclass(frozen=True)
class ArchivedOperationCandidate:
    operation_id: str
    carrier_ref: str
    carrier_tip_oid: str
    carrier_kind: ProjectionCarrierKind


@dataclass(frozen=True)
class ArchivedOperationsByIdSnapshot:
    head_oid: str
    source_digest: str
    carriers: tuple[ProjectionCarrier, ...]
    carriers_by_ref: dict[str, ProjectionCarrier]
    entries_by_id: dict[str, ArchivedOperationCandidate]


ScopeRegistryStatus = Literal["live", "merged", "retained", "discarded"]
# A scope status participates in multiple orthogonal classifications:
#   ref-owning   — the status intentionally keeps a scope ref on disk
#   runtime-open — the status represents live child work / a runtime handle
#   terminal     — the scope ref has been reclaimed; the scope is finished
# `retained` is deliberately ref-owning but not runtime-open. Recovery code must not
# treat it as abandoned live work just because its ref is valid.
REF_OWNING_SCOPE_STATUSES: frozenset[ScopeRegistryStatus] = frozenset({"live", "retained"})
RUNTIME_OPEN_SCOPE_STATUSES: frozenset[ScopeRegistryStatus] = frozenset({"live"})
TERMINAL_SCOPE_STATUSES: frozenset[ScopeRegistryStatus] = frozenset({"merged", "discarded"})


def scope_status_owns_ref(status: ScopeRegistryStatus) -> bool:
    return status in REF_OWNING_SCOPE_STATUSES


def scope_status_is_runtime_open(status: ScopeRegistryStatus) -> bool:
    return status in RUNTIME_OPEN_SCOPE_STATUSES


def scope_status_is_terminal(status: ScopeRegistryStatus) -> bool:
    return status in TERMINAL_SCOPE_STATUSES


ScopeRegistryIsolationMode = Literal["shared", "isolated"]
ScopeRegistryMismatchKind = Literal[
    "ref_exists_registry_non_live",
    "registry_live_ref_missing",
    "parentage_disagrees",
    "registry_format_unreadable",
]


@dataclass(frozen=True)
class ScopeRegistrySourceRef:
    ref: str
    tip_oid: str


@dataclass(frozen=True)
class ScopeRegistryEntry:
    name: str
    ref: str
    instance_id: str
    creation_oid: str
    parent_ref: str
    world_id: str
    isolation_mode: ScopeRegistryIsolationMode
    status: ScopeRegistryStatus


@dataclass(frozen=True)
class ScopeRegistrySnapshot:
    head_oid: str
    source_digest: str
    source_refs: tuple[ScopeRegistrySourceRef, ...]
    entries: tuple[ScopeRegistryEntry, ...]
    entries_by_name: dict[str, ScopeRegistryEntry]
    entries_by_ref: dict[str, ScopeRegistryEntry]


@dataclass(frozen=True)
class ScopeRegistryMismatch:
    kind: ScopeRegistryMismatchKind
    ref: str
    scope_name: str | None
    detail: str


def archived_operation_projection_current_head(repo: pygit2.Repository) -> str | None:
    if ARCHIVED_OPERATIONS_BY_ID_CURRENT_REF not in repo.references:
        return None
    return str(repo.references[ARCHIVED_OPERATIONS_BY_ID_CURRENT_REF].peel(pygit2.Commit).id)


def archived_operation_projection_frontier(repo: pygit2.Repository) -> tuple[ProjectionCarrier, ...]:
    carriers: list[ProjectionCarrier] = []
    for ref in repo.references:
        carrier_kind = _carrier_kind_for_ref(ref)
        if carrier_kind is None:
            continue
        carriers.append(
            ProjectionCarrier(
                ref=ref,
                tip_oid=str(repo.references[ref].peel(pygit2.Commit).id),
                carrier_kind=carrier_kind,
            )
        )
    carriers.sort(key=lambda item: (item.ref, item.tip_oid, item.carrier_kind))
    return tuple(carriers)


def archived_operation_projection_digest(carriers: tuple[ProjectionCarrier, ...]) -> str:
    payload = [{"ref": item.ref, "tip_oid": item.tip_oid, "carrier_kind": item.carrier_kind} for item in carriers]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def archived_operation_projection_is_fresh(
    repo: pygit2.Repository,
    snapshot: ArchivedOperationsByIdSnapshot,
) -> bool:
    return snapshot.source_digest == archived_operation_projection_digest(archived_operation_projection_frontier(repo))


def load_archived_operations_by_id_snapshot(
    repo: pygit2.Repository,
) -> ArchivedOperationsByIdSnapshot | None:
    if ARCHIVED_OPERATIONS_BY_ID_CURRENT_REF not in repo.references:
        return None
    commit = repo.references[ARCHIVED_OPERATIONS_BY_ID_CURRENT_REF].peel(pygit2.Commit)
    manifest = _read_json_blob(repo, commit.tree, "meta/projection.json")
    if not isinstance(manifest, dict):
        return None
    if manifest.get("family") != ARCHIVED_OPERATIONS_BY_ID_FAMILY:
        return None
    if manifest.get("version") != ARCHIVED_OPERATIONS_BY_ID_VERSION:
        return None
    if manifest.get("completeness") != "complete":
        return None
    source = manifest.get("source")
    if not isinstance(source, list):
        return None
    carriers: list[ProjectionCarrier] = []
    carriers_by_ref: dict[str, ProjectionCarrier] = {}
    for raw in source:
        carrier = _projection_carrier_from_json(raw)
        if carrier is None:
            return None
        if carrier.ref in carriers_by_ref:
            return None
        carriers.append(carrier)
        carriers_by_ref[carrier.ref] = carrier
    carriers_tuple = tuple(sorted(carriers, key=lambda item: (item.ref, item.tip_oid, item.carrier_kind)))
    manifest_digest = manifest.get("source_digest")
    if not isinstance(manifest_digest, str) or not manifest_digest:
        return None
    if archived_operation_projection_digest(carriers_tuple) != manifest_digest:
        return None

    entries: dict[str, ArchivedOperationCandidate] = {}
    shard_entries = _iter_shard_entries(repo, commit.tree)
    if shard_entries is None:
        return None
    for raw in shard_entries:
        candidate = _archived_operation_candidate_from_json(raw)
        if candidate is None:
            return None
        manifest_carrier = carriers_by_ref.get(candidate.carrier_ref)
        if manifest_carrier is None:
            return None
        if manifest_carrier.carrier_kind != candidate.carrier_kind:
            return None
        if manifest_carrier.tip_oid != candidate.carrier_tip_oid:
            return None
        if candidate.operation_id in entries:
            return None
        entries[candidate.operation_id] = candidate
    return ArchivedOperationsByIdSnapshot(
        head_oid=str(commit.id),
        source_digest=manifest_digest,
        carriers=carriers_tuple,
        carriers_by_ref=carriers_by_ref,
        entries_by_id=entries,
    )


def publish_archived_operations_by_id_snapshot(
    repo: pygit2.Repository,
    *,
    expected_head_oid: str | None,
    expected_source_digest: str,
    carriers: tuple[ProjectionCarrier, ...],
    entries: tuple[ArchivedOperationCandidate, ...],
) -> bool:
    commit_oid = _write_archived_operations_by_id_snapshot(repo, carriers=carriers, entries=entries)
    current_head_oid = archived_operation_projection_current_head(repo)
    if current_head_oid != expected_head_oid:
        return False
    current_source_digest = archived_operation_projection_digest(archived_operation_projection_frontier(repo))
    if current_source_digest != expected_source_digest:
        return False
    if ARCHIVED_OPERATIONS_BY_ID_CURRENT_REF in repo.references:
        set_reference_target(repo, ARCHIVED_OPERATIONS_BY_ID_CURRENT_REF, commit_oid)
    else:
        create_or_update_reference(repo, ARCHIVED_OPERATIONS_BY_ID_CURRENT_REF, commit_oid)
    return True


def publish_archived_operations_by_id_additions(
    repo: pygit2.Repository,
    *,
    previous: ArchivedOperationsByIdSnapshot,
    added_carriers: tuple[ProjectionCarrier, ...],
    added_entries: tuple[ArchivedOperationCandidate, ...],
) -> ArchivedOperationsByIdSnapshot | None:
    """Publish an append-only archived-operation projection update.

    This reuses the previous projection tree and rewrites only the manifest plus
    shard files touched by new operation ids. It returns None when the append
    path cannot prove consistency, allowing callers to fall back to a canonical
    rebuild.
    """
    if archived_operation_projection_current_head(repo) != previous.head_oid:
        return None

    carriers_by_ref = dict(previous.carriers_by_ref)
    for carrier in added_carriers:
        if carrier.ref in carriers_by_ref:
            return None
        if carrier.ref not in repo.references:
            return None
        current_tip = str(repo.references[carrier.ref].peel(pygit2.Commit).id)
        if current_tip != carrier.tip_oid:
            return None
        carriers_by_ref[carrier.ref] = carrier

    entries_by_id = dict(previous.entries_by_id)
    added_ids: set[str] = set()
    for candidate in added_entries:
        if candidate.operation_id in entries_by_id or candidate.operation_id in added_ids:
            return None
        manifest_carrier = carriers_by_ref.get(candidate.carrier_ref)
        if manifest_carrier is None:
            return None
        if (
            manifest_carrier.tip_oid != candidate.carrier_tip_oid
            or manifest_carrier.carrier_kind != candidate.carrier_kind
        ):
            return None
        entries_by_id[candidate.operation_id] = candidate
        added_ids.add(candidate.operation_id)

    previous_commit = require_commit(
        repo,
        pygit2.Oid(hex=previous.head_oid),
        context="previous archived-operation projection",
    )
    carriers = tuple(sorted(carriers_by_ref.values(), key=lambda item: (item.ref, item.tip_oid, item.carrier_kind)))
    source_digest = archived_operation_projection_digest(carriers)

    changes: list[tuple[str, bytes | None] | tuple[str, bytes | None, int]] = [
        ("meta/projection.json", json.dumps(_archived_operations_manifest(carriers), sort_keys=True).encode("utf-8"))
    ]

    added_by_shard: dict[str, list[ArchivedOperationCandidate]] = {}
    for candidate in added_entries:
        added_by_shard.setdefault(candidate.operation_id[:2], []).append(candidate)
    for shard, shard_additions in sorted(added_by_shard.items()):
        existing = _read_archived_operation_shard(repo, previous_commit.tree, shard)
        if existing is None:
            return None
        shard_entries_by_id = {candidate.operation_id: candidate for candidate in existing}
        if len(shard_entries_by_id) != len(existing):
            return None
        for candidate in shard_additions:
            if candidate.operation_id in shard_entries_by_id:
                return None
            shard_entries_by_id[candidate.operation_id] = candidate
        sorted_shard_entries = sorted(shard_entries_by_id.values(), key=lambda item: item.operation_id)
        encoded = json.dumps(
            [_archived_operation_candidate_to_json(candidate) for candidate in sorted_shard_entries],
            sort_keys=True,
        ).encode("utf-8")
        changes.append((f"data/shards/{shard}.json", encoded))

    tree_oid = build_tree(repo, previous_commit.tree.id, changes)
    sig = create_signature("projection")
    commit_oid = create_commit_with_recovery(
        repo,
        None,
        sig,
        sig,
        f"projection:{ARCHIVED_OPERATIONS_BY_ID_FAMILY}",
        tree_oid,
        [previous_commit.id],
    )
    if archived_operation_projection_current_head(repo) != previous.head_oid:
        return None
    set_reference_target(repo, ARCHIVED_OPERATIONS_BY_ID_CURRENT_REF, commit_oid)
    return ArchivedOperationsByIdSnapshot(
        head_oid=str(commit_oid),
        source_digest=source_digest,
        carriers=carriers,
        carriers_by_ref=carriers_by_ref,
        entries_by_id=entries_by_id,
    )


def scope_registry_current_head(repo: pygit2.Repository) -> str | None:
    if SCOPE_REGISTRY_CURRENT_REF not in repo.references:
        return None
    return str(repo.references[SCOPE_REGISTRY_CURRENT_REF].peel(pygit2.Commit).id)


def scope_registry_frontier(repo: pygit2.Repository) -> tuple[ScopeRegistrySourceRef, ...]:
    source_refs = [
        ScopeRegistrySourceRef(
            ref=ref,
            tip_oid=str(repo.references[ref].peel(pygit2.Commit).id),
        )
        for ref in repo.references
        if ref.startswith("refs/vcscore/scopes/")
    ]
    source_refs.sort(key=lambda item: (item.ref, item.tip_oid))
    return tuple(source_refs)


def scope_registry_digest(source_refs: tuple[ScopeRegistrySourceRef, ...]) -> str:
    payload = [{"ref": item.ref, "tip_oid": item.tip_oid} for item in source_refs]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def scope_registry_is_fresh(repo: pygit2.Repository, snapshot: ScopeRegistrySnapshot) -> bool:
    return snapshot.source_digest == scope_registry_digest(scope_registry_frontier(repo))


def load_scope_registry_snapshot(repo: pygit2.Repository) -> ScopeRegistrySnapshot | None:
    if SCOPE_REGISTRY_CURRENT_REF not in repo.references:
        return None
    commit = repo.references[SCOPE_REGISTRY_CURRENT_REF].peel(pygit2.Commit)
    manifest = _read_json_blob(repo, commit.tree, "meta/projection.json")
    if not isinstance(manifest, dict):
        return None
    if manifest.get("family") != SCOPE_REGISTRY_FAMILY:
        return None
    if manifest.get("version") != SCOPE_REGISTRY_VERSION:
        return None
    if manifest.get("completeness") != "complete":
        return None
    source = manifest.get("source")
    if not isinstance(source, list):
        return None
    source_refs: list[ScopeRegistrySourceRef] = []
    seen_source_refs: set[str] = set()
    for raw in source:
        source_ref = _scope_registry_source_ref_from_json(raw)
        if source_ref is None or source_ref.ref in seen_source_refs:
            return None
        source_refs.append(source_ref)
        seen_source_refs.add(source_ref.ref)
    source_refs_tuple = tuple(sorted(source_refs, key=lambda item: (item.ref, item.tip_oid)))
    manifest_digest = manifest.get("source_digest")
    if not isinstance(manifest_digest, str) or not manifest_digest:
        return None
    if scope_registry_digest(source_refs_tuple) != manifest_digest:
        return None

    raw_entries = _read_json_blob(repo, commit.tree, "data/scopes.json")
    if raw_entries is None:
        raw_entries = []
    if not isinstance(raw_entries, list):
        return None
    entries: list[ScopeRegistryEntry] = []
    entries_by_name: dict[str, ScopeRegistryEntry] = {}
    entries_by_ref: dict[str, ScopeRegistryEntry] = {}
    for raw in raw_entries:
        entry = _scope_registry_entry_from_json(raw)
        if entry is None:
            return None
        if entry.name in entries_by_name or entry.ref in entries_by_ref:
            return None
        entries.append(entry)
        entries_by_name[entry.name] = entry
        entries_by_ref[entry.ref] = entry
    entries_tuple = tuple(sorted(entries, key=lambda item: (item.name, item.ref, item.instance_id)))
    return ScopeRegistrySnapshot(
        head_oid=str(commit.id),
        source_digest=manifest_digest,
        source_refs=source_refs_tuple,
        entries=entries_tuple,
        entries_by_name=entries_by_name,
        entries_by_ref=entries_by_ref,
    )


def publish_scope_registry_snapshot(
    repo: pygit2.Repository,
    *,
    expected_head_oid: str | None,
    expected_source_digest: str,
    source_refs: tuple[ScopeRegistrySourceRef, ...],
    entries: tuple[ScopeRegistryEntry, ...],
) -> bool:
    if scope_registry_digest(source_refs) != expected_source_digest:
        msg = "Scope registry source digest does not match the provided frontier."
        raise ValueError(msg)
    commit_oid = _write_scope_registry_snapshot(repo, source_refs=source_refs, entries=entries)
    current_head_oid = scope_registry_current_head(repo)
    if current_head_oid != expected_head_oid:
        return False
    current_source_digest = scope_registry_digest(scope_registry_frontier(repo))
    if current_source_digest != expected_source_digest:
        return False
    if SCOPE_REGISTRY_CURRENT_REF in repo.references:
        set_reference_target(repo, SCOPE_REGISTRY_CURRENT_REF, commit_oid)
    else:
        create_or_update_reference(repo, SCOPE_REGISTRY_CURRENT_REF, commit_oid)
    return True


def scope_registry_mismatches(repo: pygit2.Repository) -> tuple[ScopeRegistryMismatch, ...]:
    if SCOPE_REGISTRY_CURRENT_REF not in repo.references:
        return ()
    snapshot = load_scope_registry_snapshot(repo)
    if snapshot is None:
        return (
            ScopeRegistryMismatch(
                kind="registry_format_unreadable",
                ref=SCOPE_REGISTRY_CURRENT_REF,
                scope_name=None,
                detail="Scope registry projection is unreadable or version-mismatched.",
            ),
        )

    frontier = {item.ref: item.tip_oid for item in scope_registry_frontier(repo)}
    mismatches: list[ScopeRegistryMismatch] = []
    for ref in sorted(frontier):
        entry = snapshot.entries_by_ref.get(ref)
        if entry is None or not scope_status_owns_ref(entry.status):
            scope_name = entry.name if entry is not None else ref.rsplit("/", 1)[-1]
            if entry is None:
                detail = "Scope ref exists but the registry has no matching entry."
            else:
                detail = f"Scope ref exists but registry status {entry.status!r} does not own a ref."
            mismatches.append(
                ScopeRegistryMismatch(
                    kind="ref_exists_registry_non_live",
                    ref=ref,
                    scope_name=scope_name,
                    detail=detail,
                )
            )

    for entry in snapshot.entries:
        if not scope_status_owns_ref(entry.status):
            continue
        if entry.ref not in frontier:
            mismatches.append(
                ScopeRegistryMismatch(
                    kind="registry_live_ref_missing",
                    ref=entry.ref,
                    scope_name=entry.name,
                    detail=(
                        f"Registry marks scope {entry.name!r} as {entry.status!r} "
                        "and ref-owning, but its ref is missing."
                    ),
                )
            )
            continue
        if not _scope_registry_parentage_matches(repo, entry, child_tip_oid=frontier[entry.ref]):
            mismatches.append(
                ScopeRegistryMismatch(
                    kind="parentage_disagrees",
                    ref=entry.ref,
                    scope_name=entry.name,
                    detail=f"Registry parent linkage disagrees with the {entry.status!r} scope ref topology.",
                )
            )

    return tuple(mismatches)


def _write_archived_operations_by_id_snapshot(
    repo: pygit2.Repository,
    *,
    carriers: tuple[ProjectionCarrier, ...],
    entries: tuple[ArchivedOperationCandidate, ...],
) -> pygit2.Oid:
    shard_entries: dict[str, list[dict[str, Any]]] = {}
    for entry in sorted(entries, key=lambda item: item.operation_id):
        shard_entries.setdefault(entry.operation_id[:2], []).append(_archived_operation_candidate_to_json(entry))

    changes: list[tuple[str, bytes | None] | tuple[str, bytes | None, int]] = [
        ("meta/projection.json", json.dumps(_archived_operations_manifest(carriers), sort_keys=True).encode("utf-8"))
    ]
    for shard, shard_payload in sorted(shard_entries.items()):
        path = f"data/shards/{shard}.json"
        encoded = json.dumps(shard_payload, sort_keys=True).encode("utf-8")
        changes.append((path, encoded))

    tree_oid = build_tree(repo, None, changes)
    parents: list[pygit2.Oid] = []
    if ARCHIVED_OPERATIONS_BY_ID_CURRENT_REF in repo.references:
        parents.append(repo.references[ARCHIVED_OPERATIONS_BY_ID_CURRENT_REF].peel(pygit2.Commit).id)
    sig = create_signature("projection")
    return create_commit_with_recovery(
        repo,
        None,
        sig,
        sig,
        f"projection:{ARCHIVED_OPERATIONS_BY_ID_FAMILY}",
        tree_oid,
        parents,
    )


def _write_scope_registry_snapshot(
    repo: pygit2.Repository,
    *,
    source_refs: tuple[ScopeRegistrySourceRef, ...],
    entries: tuple[ScopeRegistryEntry, ...],
) -> pygit2.Oid:
    source_digest = scope_registry_digest(source_refs)
    manifest = {
        "family": SCOPE_REGISTRY_FAMILY,
        "version": SCOPE_REGISTRY_VERSION,
        "built_at": time.time(),
        "completeness": "complete",
        "source": [{"ref": item.ref, "tip_oid": item.tip_oid} for item in source_refs],
        "source_digest": source_digest,
    }
    scope_entries = [
        {
            "name": entry.name,
            "ref": entry.ref,
            "instance_id": entry.instance_id,
            "creation_oid": entry.creation_oid,
            "parent_ref": entry.parent_ref,
            "world_id": entry.world_id,
            "isolation_mode": entry.isolation_mode,
            "status": entry.status,
        }
        for entry in sorted(entries, key=lambda item: (item.name, item.ref, item.instance_id))
    ]
    changes: list[tuple[str, bytes | None] | tuple[str, bytes | None, int]] = [
        ("meta/projection.json", json.dumps(manifest, sort_keys=True).encode("utf-8")),
        ("data/scopes.json", json.dumps(scope_entries, sort_keys=True).encode("utf-8")),
    ]
    tree_oid = build_tree(repo, None, changes)
    parents: list[pygit2.Oid] = []
    if SCOPE_REGISTRY_CURRENT_REF in repo.references:
        parents.append(repo.references[SCOPE_REGISTRY_CURRENT_REF].peel(pygit2.Commit).id)
    sig = create_signature("projection")
    return create_commit_with_recovery(
        repo,
        None,
        sig,
        sig,
        f"projection:{SCOPE_REGISTRY_FAMILY}",
        tree_oid,
        parents,
    )


def _projection_carrier_from_json(raw: object) -> ProjectionCarrier | None:
    if not isinstance(raw, dict):
        return None
    ref = raw.get("ref")
    tip_oid = raw.get("tip_oid")
    carrier_kind = raw.get("carrier_kind")
    if not isinstance(ref, str) or not ref:
        return None
    if not isinstance(tip_oid, str) or not tip_oid:
        return None
    if carrier_kind not in ("archived_operation_ref", "discarded_world_ref"):
        return None
    return ProjectionCarrier(ref=ref, tip_oid=tip_oid, carrier_kind=carrier_kind)


def _scope_registry_source_ref_from_json(raw: object) -> ScopeRegistrySourceRef | None:
    if not isinstance(raw, dict):
        return None
    ref = raw.get("ref")
    tip_oid = raw.get("tip_oid")
    if not isinstance(ref, str) or not ref.startswith("refs/vcscore/scopes/"):
        return None
    if not isinstance(tip_oid, str) or not tip_oid:
        return None
    return ScopeRegistrySourceRef(ref=ref, tip_oid=tip_oid)


def _scope_registry_entry_from_json(raw: object) -> ScopeRegistryEntry | None:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    ref = raw.get("ref")
    instance_id = raw.get("instance_id")
    creation_oid = raw.get("creation_oid")
    parent_ref = raw.get("parent_ref")
    world_id = raw.get("world_id")
    isolation_mode = raw.get("isolation_mode")
    status = raw.get("status")
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(ref, str) or ref != f"refs/vcscore/scopes/{name}":
        return None
    if not isinstance(instance_id, str) or not instance_id:
        return None
    if not isinstance(creation_oid, str) or not creation_oid:
        return None
    if not isinstance(parent_ref, str) or not parent_ref:
        return None
    if not isinstance(world_id, str) or not world_id:
        return None
    if isolation_mode not in ("shared", "isolated"):
        return None
    if status not in ("live", "merged", "retained", "discarded"):
        return None
    return ScopeRegistryEntry(
        name=name,
        ref=ref,
        instance_id=instance_id,
        creation_oid=creation_oid,
        parent_ref=parent_ref,
        world_id=world_id,
        isolation_mode=isolation_mode,
        status=status,
    )


def _archived_operation_candidate_from_json(raw: object) -> ArchivedOperationCandidate | None:
    if not isinstance(raw, dict):
        return None
    operation_id = raw.get("operation_id")
    carrier_ref = raw.get("carrier_ref")
    carrier_tip_oid = raw.get("carrier_tip_oid")
    carrier_kind = raw.get("carrier_kind")
    if not isinstance(operation_id, str) or not operation_id:
        return None
    if not isinstance(carrier_ref, str) or not carrier_ref:
        return None
    if not isinstance(carrier_tip_oid, str) or not carrier_tip_oid:
        return None
    if carrier_kind not in ("archived_operation_ref", "discarded_world_ref"):
        return None
    return ArchivedOperationCandidate(
        operation_id=operation_id,
        carrier_ref=carrier_ref,
        carrier_tip_oid=carrier_tip_oid,
        carrier_kind=carrier_kind,
    )


def _archived_operation_candidate_to_json(entry: ArchivedOperationCandidate) -> dict[str, str]:
    return {
        "operation_id": entry.operation_id,
        "carrier_ref": entry.carrier_ref,
        "carrier_tip_oid": entry.carrier_tip_oid,
        "carrier_kind": entry.carrier_kind,
    }


def _archived_operations_manifest(carriers: tuple[ProjectionCarrier, ...]) -> dict[str, object]:
    return {
        "family": ARCHIVED_OPERATIONS_BY_ID_FAMILY,
        "version": ARCHIVED_OPERATIONS_BY_ID_VERSION,
        "built_at": time.time(),
        "completeness": "complete",
        "source": [{"ref": item.ref, "tip_oid": item.tip_oid, "carrier_kind": item.carrier_kind} for item in carriers],
        "source_digest": archived_operation_projection_digest(carriers),
    }


def _read_archived_operation_shard(
    repo: pygit2.Repository,
    root_tree: pygit2.Tree,
    shard: str,
) -> list[ArchivedOperationCandidate] | None:
    obj = lookup_path(repo, root_tree, f"data/shards/{shard}.json")
    if obj is None:
        return []
    if not isinstance(obj, pygit2.Blob):
        return None
    try:
        payload = json.loads(obj.data.decode("utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    candidates: list[ArchivedOperationCandidate] = []
    for raw in payload:
        candidate = _archived_operation_candidate_from_json(raw)
        if candidate is None or candidate.operation_id[:2] != shard:
            return None
        candidates.append(candidate)
    return candidates


def _iter_shard_entries(repo: pygit2.Repository, root_tree: pygit2.Tree) -> list[object] | None:
    shards_tree = _tree_at_path(repo, root_tree, "data/shards")
    if shards_tree is None:
        return []
    if not isinstance(shards_tree, pygit2.Tree):
        return None
    raw_entries: list[object] = []
    for entry in sorted_tree_entries(shards_tree):
        blob = require_blob(repo, entry.id, context=f"projection shard {entry.name}")
        try:
            payload = json.loads(blob.data.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, list):
            return None
        raw_entries.extend(payload)
    return raw_entries


def _scope_registry_parentage_matches(
    repo: pygit2.Repository,
    entry: ScopeRegistryEntry,
    *,
    child_tip_oid: str,
) -> bool:
    if not _is_ancestor(repo, entry.creation_oid, child_tip_oid):
        return False
    if entry.parent_ref not in repo.references:
        return False
    parent_tip_oid = str(repo.references[entry.parent_ref].peel(pygit2.Commit).id)
    return parent_tip_oid == entry.creation_oid or _is_ancestor(repo, entry.creation_oid, parent_tip_oid)


def _is_ancestor(repo: pygit2.Repository, ancestor_oid: str, descendant_oid: str) -> bool:
    try:
        for commit in topological_commits(repo, descendant_oid):
            if str(commit.id) == ancestor_oid:
                return True
    except (KeyError, ValueError, pygit2.GitError):
        return False
    return False


def _read_json_blob(repo: pygit2.Repository, root_tree: pygit2.Tree, path: str) -> object | None:
    obj = lookup_path(repo, root_tree, path)
    if not isinstance(obj, pygit2.Blob):
        return None
    try:
        return cast("object", json.loads(obj.data.decode("utf-8")))
    except json.JSONDecodeError:
        return None


def _tree_at_path(repo: pygit2.Repository, root_tree: pygit2.Tree, path: str) -> pygit2.Object | None:
    return lookup_path(repo, root_tree, path)


def _carrier_kind_for_ref(ref: str) -> ProjectionCarrierKind | None:
    if ref.startswith("refs/vcscore/archive/ops/"):
        return "archived_operation_ref"
    if ref.startswith("refs/vcscore/archive/") and not ref.startswith("refs/vcscore/archive/ground-reset-"):
        return "discarded_world_ref"
    return None
