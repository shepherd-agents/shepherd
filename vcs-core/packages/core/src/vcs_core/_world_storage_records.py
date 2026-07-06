"""Pure record/issue/candidate helpers for the world-storage layer.

Shared by ``WorldStorageManager`` and ``OperationJournalController``.
Extracted from ``_world_storage_manager`` (V2.2b / P4 rider 3) so the two modules
depend on this leaf instead of on each other — retiring the transitional
controller<->manager import pair recorded in the V2.1 shim ledger. Pure functions
and frozen records only; imports nothing from ``_world_storage_manager`` or
``_operation_journal_controller`` (leaf discipline).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pygit2

from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._pygit2_helpers import require_blob, require_commit
from vcs_core._world_refs import (
    world_pin_ref,
)
from vcs_core._world_types import (
    OPERATION_FINAL_SCHEMA,
    CandidateRevision,
    StructuredIssue,
    SubstrateHead,
    WorldCommit,
    canonical_digest,
    load_canonical_json,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vcs_core._substrate_store import SubstrateStore
    from vcs_core._world_closure import WorldClosure
    from vcs_core._world_operation_builder import (
        PreparedCandidateTupleRecord,
        PreparedWorldOperation,
    )
    from vcs_core._world_types import CandidateRevision, SubstrateHead, WorldCommit


@dataclass(frozen=True)
class OperationJournalFsckReport:
    """Validation report for one operation journal."""

    operation_id: str
    issue_details: tuple[StructuredIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issue_details

    @property
    def issues(self) -> tuple[str, ...]:
        return tuple(issue.message for issue in self.issue_details)


@dataclass(frozen=True)
class OperationFinalEvidence:
    """Final operation evidence derived from an immutable world commit."""

    operation_id: str
    operation_final_digest: str
    selected: dict[str, str]
    candidate_outcomes: tuple[dict[str, object], ...]


def _candidate_revision_to_json(candidate: CandidateRevision) -> dict[str, object]:
    return {
        "operation_id": candidate.operation_id,
        "binding": candidate.binding,
        "candidate_id": candidate.candidate_id,
        "store_id": candidate.store_id,
        "resource_id": candidate.resource_id,
        "head": candidate.head,
        "ref": candidate.ref,
    }


def _prepared_operation_from_json(value: Mapping[str, object]) -> PreparedWorldOperation:
    from vcs_core._world_operation_builder import PreparedWorldOperation

    return PreparedWorldOperation.from_json(value)


def _operation_final_evidence_from_world(world: WorldCommit) -> OperationFinalEvidence:
    operation_id = world.operation_final.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id:
        raise InvalidRepositoryStateError("world operation-final operation_id is required")
    if world.operation_final.get("schema") != OPERATION_FINAL_SCHEMA:
        raise InvalidRepositoryStateError(
            f"unsupported world operation-final schema: {world.operation_final.get('schema')!r}",
        )
    transition_operation_id = world.transition.get("operation_id")
    if operation_id != transition_operation_id:
        raise InvalidRepositoryStateError("world operation-final operation_id disagrees with transition")
    transition_final = world.transition.get("operation_final")
    if not isinstance(transition_final, dict):
        raise InvalidRepositoryStateError("world transition operation_final is required")
    digest = transition_final.get("digest")
    if not isinstance(digest, str) or not digest:
        raise InvalidRepositoryStateError("world transition operation_final.digest is required")
    return OperationFinalEvidence(
        operation_id=operation_id,
        operation_final_digest=digest,
        selected=_string_map(world.operation_final.get("selected"), "operation-final selected"),
        candidate_outcomes=tuple(
            _object_list(world.operation_final.get("candidate_outcomes"), "operation-final candidate_outcomes"),
        ),
    )


def _candidate_tuple_matches_head(
    candidate_tuple: PreparedCandidateTupleRecord,
    head: SubstrateHead,
    *,
    producer_operation_id: str,
    candidate_id: str,
) -> bool:
    candidate = candidate_tuple.candidate
    return (
        candidate.operation_id == producer_operation_id
        and candidate.binding == head.binding
        and candidate.store_id == head.store_id
        and candidate.resource_id == head.resource_id
        and candidate.head == head.head
        and candidate.candidate_id == candidate_id
    )


def _extend_final_evidence_issues(
    issues: list[StructuredIssue],
    journal_tip: Mapping[str, object],
    world: WorldCommit,
    *,
    operation_id: str,
) -> None:
    try:
        evidence = _operation_final_evidence_from_world(world)
    except (InvalidRepositoryStateError, KeyError, TypeError, ValueError) as exc:
        issues.append(_issue("journal_world_invalid", str(exc), operation_id=operation_id, world_oid=world.oid))
        return
    if evidence.operation_id != operation_id:
        issues.append(
            _issue(
                "journal_operation_id_mismatch",
                "operation journal operation_id disagrees with world operation-final",
                operation_id=operation_id,
                world_oid=world.oid,
            ),
        )
    if journal_tip.get("operation_final_digest") != evidence.operation_final_digest:
        issues.append(
            _issue(
                "journal_final_digest_mismatch",
                "operation journal final digest disagrees with world transition",
                operation_id=operation_id,
                world_oid=world.oid,
            ),
        )
    if journal_tip.get("selected") != evidence.selected:
        issues.append(
            _issue(
                "journal_selected_mismatch",
                "operation journal selected heads disagree with world operation-final",
                operation_id=operation_id,
                world_oid=world.oid,
            ),
        )
    if journal_tip.get("candidate_outcomes") != list(evidence.candidate_outcomes):
        issues.append(
            _issue(
                "journal_candidate_outcomes_mismatch",
                "operation journal candidate outcomes disagree with world operation-final",
                operation_id=operation_id,
                world_oid=world.oid,
            ),
        )


def _issue(
    code: str,
    message: str,
    *,
    world_oid: str | None = None,
    operation_id: str | None = None,
    store_id: str | None = None,
    binding: str | None = None,
    ref: str | None = None,
    recovery_hint: str | None = None,
) -> StructuredIssue:
    return StructuredIssue(
        code=code,
        message=message,
        world_oid=world_oid,
        operation_id=operation_id,
        store_id=store_id,
        binding=binding,
        ref=ref,
        recovery_hint=recovery_hint,
    )


def _extend_candidate_ref_issues(
    issues: list[StructuredIssue],
    candidate_refs: object,
    *,
    stores: Mapping[str, SubstrateStore],
) -> None:
    if not isinstance(candidate_refs, list):
        issues.append(_issue("journal_candidate_refs_malformed", "operation journal candidate_refs must be a list"))
        return
    for candidate in candidate_refs:
        if not isinstance(candidate, dict):
            issues.append(
                _issue("journal_candidate_refs_malformed", "operation journal candidate_refs entries must be objects"),
            )
            continue
        store_id = candidate.get("store_id")
        ref = candidate.get("ref")
        head = candidate.get("head")
        if not isinstance(store_id, str) or not isinstance(ref, str) or not isinstance(head, str):
            issues.append(
                _issue("journal_candidate_ref_malformed", "operation journal candidate ref entry is malformed"),
            )
            continue
        store = stores.get(store_id)
        if store is None:
            issues.append(
                _issue(
                    "journal_unknown_store",
                    f"operation journal candidate ref names unknown store {store_id!r}",
                    store_id=store_id,
                    ref=ref,
                ),
            )
            continue
        try:
            target = store.repo.references[ref].target
        except KeyError:
            issues.append(
                _issue(
                    "journal_missing_candidate_ref",
                    f"operation journal candidate ref is missing: {ref}",
                    store_id=store_id,
                    ref=ref,
                    recovery_hint="Restore the candidate ref or archive the failed operation.",
                ),
            )
            continue
        if str(target) != head:
            issues.append(
                _issue(
                    "journal_candidate_ref_mismatch",
                    f"operation journal candidate ref target disagrees with record: {ref}",
                    store_id=store_id,
                    ref=ref,
                ),
            )


def _required_payload_str(payload: Mapping[str, object], label: str, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise InvalidRepositoryStateError(f"{label} {key} must be a non-empty string")
    return value


def _optional_payload_str(payload: Mapping[str, object], label: str, key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise InvalidRepositoryStateError(f"{label} {key} must be a non-empty string when present")
    return value


def _string_map(value: object, name: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise InvalidRepositoryStateError(f"{name} must be a string map")
    return dict(value)


def _object_list(value: object, name: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise InvalidRepositoryStateError(f"{name} must be an object list")
    return [dict(item) for item in value]


WORLD_FORK_ORIGIN_RECEIPT_SCHEMA = "vcscore/world-fork-origin-receipt/v1"
WORLD_FORK_ORIGIN_RECEIPT_PATH = "meta/world-fork-origin-receipt.json"


def _read_blob_bytes(repo: pygit2.Repository, tree: pygit2.Tree, path: str) -> bytes:
    obj: pygit2.Object = tree
    for component in path.split("/"):
        if not isinstance(obj, pygit2.Tree):
            raise TypeError(f"{path!r} did not resolve to a blob")
        obj = repo[obj[component].id]
    blob = require_blob(repo, obj.id, context=path)
    return bytes(blob.data)


# --- shared ref/receipt helpers hoisted from WSM (V2.2c) ---
DEFAULT_GROUND_REF = "refs/vcscore/ground"


@dataclass(frozen=True)
class _ProtectedRetention:
    world_oids: frozenset[str]
    refs: frozenset[str]


@dataclass(frozen=True)
class _ForkOriginReceipt:
    authority_ref: str
    world_store_id: str
    first_world_oid: str
    forked_from_authority_ref: str
    forked_from_world_oid: str

    def to_json(self) -> dict[str, object]:
        payload = {
            "schema": WORLD_FORK_ORIGIN_RECEIPT_SCHEMA,
            "authority_ref": self.authority_ref,
            "world_store_id": self.world_store_id,
            "first_world_oid": self.first_world_oid,
            "forked_from_authority_ref": self.forked_from_authority_ref,
            "forked_from_world_oid": self.forked_from_world_oid,
        }
        return {**payload, "receipt_digest": canonical_digest(payload)}


def _world_operation_id(world: WorldCommit) -> str:
    operation_id = world.operation_final.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id:
        raise InvalidRepositoryStateError("world operation-final operation_id is required")
    transition_operation_id = world.transition.get("operation_id")
    if operation_id != transition_operation_id:
        raise InvalidRepositoryStateError("world operation-final operation_id disagrees with transition")
    return operation_id


def _read_world_fork_origin_receipt(repo: pygit2.Repository, ref: str) -> _ForkOriginReceipt:
    try:
        target = repo.references[ref].target
    except KeyError as exc:
        raise KeyError(ref) from exc
    commit = require_commit(repo, pygit2.Oid(hex=str(target)), context="world fork origin receipt")
    payload = load_canonical_json(_read_blob_bytes(repo, commit.tree, WORLD_FORK_ORIGIN_RECEIPT_PATH))
    expected_keys = {
        "schema",
        "authority_ref",
        "world_store_id",
        "first_world_oid",
        "forked_from_authority_ref",
        "forked_from_world_oid",
        "receipt_digest",
    }
    extra_keys = set(payload) - expected_keys
    if extra_keys:
        raise InvalidRepositoryStateError(f"unexpected fork origin receipt fields: {sorted(extra_keys)!r}")
    missing_keys = expected_keys - set(payload)
    if missing_keys:
        raise InvalidRepositoryStateError(f"missing fork origin receipt fields: {sorted(missing_keys)!r}")
    if payload.get("schema") != WORLD_FORK_ORIGIN_RECEIPT_SCHEMA:
        raise InvalidRepositoryStateError(f"unsupported fork origin receipt schema: {payload.get('schema')!r}")
    receipt_digest = payload.get("receipt_digest")
    unsigned = {key: value for key, value in payload.items() if key != "receipt_digest"}
    if receipt_digest != canonical_digest(unsigned):
        raise InvalidRepositoryStateError("fork origin receipt digest disagrees with payload")
    return _ForkOriginReceipt(
        authority_ref=_required_payload_str(payload, "fork origin receipt", "authority_ref"),
        world_store_id=_required_payload_str(payload, "fork origin receipt", "world_store_id"),
        first_world_oid=_required_payload_str(payload, "fork origin receipt", "first_world_oid"),
        forked_from_authority_ref=_required_payload_str(payload, "fork origin receipt", "forked_from_authority_ref"),
        forked_from_world_oid=_required_payload_str(payload, "fork origin receipt", "forked_from_world_oid"),
    )


def _world_selected_pins_are_authoritative(
    closure: WorldClosure,
    *,
    world_store_id: str,
    world_oid: str,
    protected_world_oids: frozenset[str],
    pin_classification: Mapping[str, tuple[str, ...]],
) -> bool:
    if world_oid not in protected_world_oids:
        return False
    bad_refs = set(pin_classification.get("missing_for_published_world", ())) | set(
        pin_classification.get("corrupt", ()),
    )
    selected_pin_refs = {
        world_pin_ref(world_store_id, head.world_oid, head.binding)
        for head in closure.heads
        if head.world_oid == world_oid
    }
    return not selected_pin_refs.intersection(bad_refs)


def _validate_advance_basis(world: WorldCommit, *, input_world_oid: str) -> None:
    if not input_world_oid:
        raise InvalidRepositoryStateError("advance publication requires input_world_oid")
    transition_input_world = world.transition.get("input_world")
    if transition_input_world != input_world_oid:
        raise InvalidRepositoryStateError("advance publication input_world_oid disagrees with world transition")
    if input_world_oid not in world.parent_oids:
        raise InvalidRepositoryStateError("advance publication input_world_oid must be a Git parent of the world")


def _current_ref_target(repo: pygit2.Repository, ref: str) -> str | None:
    try:
        return str(repo.references[ref].target)
    except KeyError:
        return None


# --- world-fsck / authority-lineage records (leaf-hosted so the controller
# imports them here, not from WSM; keeps controller free of WSM imports, S3=17) ---
@dataclass(frozen=True)
class WorldFsckReport:
    """Validation and pin-health report for one world commit."""

    world_oid: str
    pin_classification: dict[str, tuple[str, ...]]
    issue_details: tuple[StructuredIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issue_details

    @property
    def issues(self) -> tuple[str, ...]:
        return tuple(issue.message for issue in self.issue_details)


@dataclass(frozen=True)
class _AuthorityLineageSegments:
    local_world_oids: tuple[str, ...]
    fork_origin: _ForkOriginReceipt | None = None
    corrupt_fork_origin: str | None = None
