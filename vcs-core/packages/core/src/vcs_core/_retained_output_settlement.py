"""Durable consume-once settlement records for retained outputs."""

from __future__ import annotations

import json
from typing import Any

import pygit2

from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._pygit2_helpers import lookup_path, require_blob
from vcs_core._world_refs import encode_ref_component
from vcs_core._world_types import canonical_digest
from vcs_core.git_store import build_tree, create_commit_with_recovery, create_or_update_reference, create_signature
from vcs_core.types import RetainedOutputSettlement

SETTLEMENT_SCHEMA = "vcscore/retained-output-settlement/v1"
SETTLEMENT_PATH = "meta/retained-output-settlement.json"


def retained_output_settlement_ref(
    *,
    scope_name: str,
    scope_instance_id: str,
    binding: str,
    candidate_id: str,
) -> str:
    """Return the deterministic settlement receipt ref for one retained output."""
    return (
        "refs/vcscore/settlements/"
        f"{encode_ref_component(scope_name)}/"
        f"{encode_ref_component(scope_instance_id)}/"
        f"{encode_ref_component(binding)}/"
        f"{encode_ref_component(candidate_id)}"
    )


def read_retained_output_settlement(
    store: Any,
    ref: str,
    *,
    missing_ok: bool = False,
) -> RetainedOutputSettlement | None:
    """Load one retained-output settlement receipt."""
    repo = store._repo
    if ref not in repo.references:
        if missing_ok:
            return None
        raise InvalidRepositoryStateError(f"retained output settlement ref is missing: {ref}")
    commit = repo.references[ref].peel(pygit2.Commit)
    payload = _read_json_blob(repo, commit.tree, SETTLEMENT_PATH)
    settlement = _settlement_from_json(payload)
    if settlement.settlement_ref != ref:
        raise InvalidRepositoryStateError("retained output settlement identity disagrees with ref")
    expected_ref = retained_output_settlement_ref(
        scope_name=settlement.scope_name,
        scope_instance_id=settlement.scope_instance_id,
        binding=settlement.binding,
        candidate_id=settlement.candidate_id,
    )
    if expected_ref != ref:
        raise InvalidRepositoryStateError("retained output settlement ref disagrees with output identity")
    return settlement


def write_retained_output_settlement(
    store: Any,
    settlement: RetainedOutputSettlement,
) -> RetainedOutputSettlement:
    """Persist a settlement receipt, failing if the retained output was already consumed."""
    expected_ref = retained_output_settlement_ref(
        scope_name=settlement.scope_name,
        scope_instance_id=settlement.scope_instance_id,
        binding=settlement.binding,
        candidate_id=settlement.candidate_id,
    )
    if settlement.settlement_ref != expected_ref:
        raise InvalidRepositoryStateError("retained output settlement ref disagrees with output identity")
    existing = read_retained_output_settlement(store, settlement.settlement_ref, missing_ok=True)
    if existing is not None:
        raise InvalidRepositoryStateError(f"retained output is already settled: {settlement.settlement_ref}")

    repo = store._repo
    payload = _settlement_to_json(settlement)
    tree_oid = build_tree(
        repo,
        None,
        [(SETTLEMENT_PATH, json.dumps(payload, sort_keys=True).encode("utf-8"))],
    )
    sig = create_signature("settlement")
    oid = create_commit_with_recovery(
        repo,
        None,
        sig,
        sig,
        f"settlement:{settlement.scope_name}:{settlement.binding}:{settlement.candidate_id}",
        tree_oid,
        [],
    )
    create_or_update_reference(repo, settlement.settlement_ref, oid)
    return settlement


def _settlement_to_json(settlement: RetainedOutputSettlement) -> dict[str, object]:
    payload = {
        "schema": SETTLEMENT_SCHEMA,
        "scope_name": settlement.scope_name,
        "scope_ref": settlement.scope_ref,
        "scope_instance_id": settlement.scope_instance_id,
        "parent_ref": settlement.parent_ref,
        "handoff_ref": settlement.handoff_ref,
        "output_world_oid": settlement.output_world_oid,
        "binding": settlement.binding,
        "store_id": settlement.store_id,
        "resource_id": settlement.resource_id,
        "candidate_id": settlement.candidate_id,
        "candidate_head": settlement.candidate_head,
        "action": settlement.action,
        "operation_id": settlement.operation_id,
        "parent_world_before": settlement.parent_world_before,
        "parent_world_after": settlement.parent_world_after,
        "settlement_ref": settlement.settlement_ref,
    }
    if settlement.authority_operation_id is not None:
        payload["authority_operation_id"] = settlement.authority_operation_id
    if settlement.authority_settlement_operation_id is not None:
        payload["authority_settlement_operation_id"] = settlement.authority_settlement_operation_id
    if settlement.authority_outcome is not None:
        payload["authority_outcome"] = settlement.authority_outcome
    if settlement.applied_head is not None:
        payload["applied_head"] = settlement.applied_head
    return {**payload, "settlement_digest": canonical_digest(payload)}


def _settlement_from_json(value: dict[str, object]) -> RetainedOutputSettlement:
    expected = {
        "schema",
        "scope_name",
        "scope_ref",
        "scope_instance_id",
        "parent_ref",
        "handoff_ref",
        "output_world_oid",
        "binding",
        "store_id",
        "resource_id",
        "candidate_id",
        "candidate_head",
        "action",
        "operation_id",
        "parent_world_before",
        "parent_world_after",
        "settlement_ref",
        "authority_operation_id",
        "authority_settlement_operation_id",
        "authority_outcome",
        "applied_head",
        "settlement_digest",
    }
    extra = set(value) - expected
    if extra:
        raise InvalidRepositoryStateError(f"unexpected retained output settlement fields: {sorted(extra)!r}")
    if value.get("schema") != SETTLEMENT_SCHEMA:
        raise InvalidRepositoryStateError(f"unsupported retained output settlement schema: {value.get('schema')!r}")
    digest_payload = {key: item for key, item in value.items() if key != "settlement_digest"}
    if value.get("settlement_digest") != canonical_digest(digest_payload):
        raise InvalidRepositoryStateError("retained output settlement digest mismatch")
    action = _required_str(value, "action")
    if action not in {"selected", "applied", "released", "discarded"}:
        raise InvalidRepositoryStateError(f"unsupported retained output settlement action: {action!r}")
    return RetainedOutputSettlement(
        scope_name=_required_str(value, "scope_name"),
        scope_ref=_required_str(value, "scope_ref"),
        scope_instance_id=_required_str(value, "scope_instance_id"),
        parent_ref=_required_str(value, "parent_ref"),
        handoff_ref=_required_str(value, "handoff_ref"),
        output_world_oid=_required_str(value, "output_world_oid"),
        binding=_required_str(value, "binding"),
        store_id=_required_str(value, "store_id"),
        resource_id=_required_str(value, "resource_id"),
        candidate_id=_required_str(value, "candidate_id"),
        candidate_head=_required_str(value, "candidate_head"),
        action=action,  # type: ignore[arg-type]
        operation_id=_required_str(value, "operation_id"),
        parent_world_before=_required_str(value, "parent_world_before"),
        parent_world_after=_required_str(value, "parent_world_after"),
        settlement_ref=_required_str(value, "settlement_ref"),
        authority_operation_id=_optional_str(value, "authority_operation_id"),
        authority_settlement_operation_id=_optional_str(value, "authority_settlement_operation_id"),
        authority_outcome=_optional_str(value, "authority_outcome"),
        applied_head=_optional_str(value, "applied_head"),
    )


def _read_json_blob(repo: pygit2.Repository, tree: pygit2.Tree, path: str) -> dict[str, Any]:
    obj = lookup_path(repo, tree, path)
    if obj is None:
        raise InvalidRepositoryStateError(f"retained output settlement is missing {path}")
    blob = require_blob(repo, obj.id, context=path)
    try:
        value = json.loads(bytes(blob.data).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidRepositoryStateError(f"retained output settlement {path} is malformed JSON") from exc
    if not isinstance(value, dict):
        raise InvalidRepositoryStateError(f"retained output settlement {path} must be a JSON object")
    return value


def _required_str(value: dict[str, object], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item:
        raise InvalidRepositoryStateError(f"retained output settlement field {field!r} is required")
    return item


def _optional_str(value: dict[str, object], field: str) -> str | None:
    item = value.get(field)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise InvalidRepositoryStateError(f"retained output settlement field {field!r} must be a non-empty string")
    return item
