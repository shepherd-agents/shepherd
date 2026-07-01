"""Inventory probes for authority settlement pending files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from vcs_core._authority import (
    AUTHORITY_SETTLEMENT_PENDING_SCHEMA,
    PendingAuthoritySettlement,
    _authority_settlement_pending_path,
    _authority_settlement_pending_root,
)
from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._query_inventory import (
    Health,
    HealthIssue,
    InventoryIssue,
    InventoryItem,
    issue_id,
    present_invalid,
    present_valid,
)
from vcs_core._query_locators import classify_locator_component
from vcs_core._world_refs import encode_ref_component

AUTHORITY_SETTLEMENT_FILE_UNREADABLE = "authority_settlement_file_unreadable"
AUTHORITY_SETTLEMENT_PAYLOAD_CORRUPT = "authority_settlement_payload_corrupt"
AUTHORITY_SETTLEMENT_SCHEMA_MISMATCH = "authority_settlement_schema_mismatch"
AUTHORITY_SETTLEMENT_IDENTITY_MISMATCH = "authority_settlement_identity_mismatch"


def probe_authority_settlement_pending(repo_path: str | Path) -> tuple[InventoryItem,...]:
    root = _authority_settlement_pending_root(repo_path)
    if not root.exists():
        return ()
    return tuple(probe_authority_settlement_pending_file(path) for path in sorted(root.glob("*.json")))


def authority_settlement_pending_label(item: InventoryItem) -> str:
    operation_id = item.fields.get("settlement_operation_id")
    if isinstance(operation_id, str) and operation_id:
        return operation_id
    payload_operation_id = item.fields.get("payload_settlement_operation_id")
    if isinstance(payload_operation_id, str) and payload_operation_id:
        return payload_operation_id
    locator_operation_id = item.fields.get("locator_settlement_operation_id")
    if isinstance(locator_operation_id, str) and locator_operation_id:
        return f"{locator_operation_id} ({item.health.status})"
    return f"{Path(str(item.locator)).name} ({item.health.status})"


def authority_settlement_pending_labels(repo_path: str | Path) -> tuple[str,...]:
    return tuple(
        authority_settlement_pending_label(item)
        for item in probe_authority_settlement_pending(repo_path)
    )


def read_valid_authority_settlement_pending_records(
    repo_path: str | Path,
) -> tuple[PendingAuthoritySettlement,...]:
    items = probe_authority_settlement_pending(repo_path)
    invalid_items = tuple(item for item in items if item.health.validity != "valid")
    if invalid_items:
        raise InvalidRepositoryStateError(_invalid_pending_records_message(invalid_items))

    records: list[PendingAuthoritySettlement] = []
    for item in items:
        if item.locator is None:
            raise InvalidRepositoryStateError(
                f"authority settlement inventory item {item.id!r} has no locator."
            )
        try:
            payload = json.loads(Path(item.locator).read_text())
            records.append(PendingAuthoritySettlement.from_dict(payload))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise InvalidRepositoryStateError(
                "authority settlement pending inventory changed while recovery was reading "
                f"{item.locator}: {exc}"
            ) from exc
    return tuple(records)


def probe_authority_settlement_pending_record(
    repo_path: str | Path,
    settlement_operation_id: str,
) -> InventoryItem:
    path = _authority_settlement_pending_path(repo_path, settlement_operation_id)
    if path.exists():
        return probe_authority_settlement_pending_file(
            path,
            expected_settlement_operation_id=settlement_operation_id,
        )
    item_id = _item_id(path)
    issue = _issue(
        item_id,
        AUTHORITY_SETTLEMENT_FILE_UNREADABLE,
        f"authority settlement file is missing: {path}",
        path,
    )
    return _item(
        item_id=item_id,
        path=path,
        health=present_invalid(
            primary_issue="missing",
            issue_codes=(AUTHORITY_SETTLEMENT_FILE_UNREADABLE,),
            lifecycle="recoverable",
            authority_role="authoritative",
        ),
        fields=_locator_fields(path),
        issues=(issue,),
    )


def probe_authority_settlement_pending_file(
    path: str | Path,
    *,
    expected_settlement_operation_id: str | None = None,
) -> InventoryItem:
    file_path = Path(path)
    item_id = _item_id(file_path)
    fields = _locator_fields(file_path)
    source_identity: dict[str, object] = {"path": str(file_path)}

    try:
        stat = file_path.stat()
        raw = file_path.read_bytes()
    except OSError as exc:
        return _invalid_item(
            item_id=item_id,
            path=file_path,
            code=AUTHORITY_SETTLEMENT_FILE_UNREADABLE,
            primary_issue="unreadable",
            message=str(exc),
            fields=fields,
            source_identity=source_identity,
        )
    source_identity.update(
        {
            "file_size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "content_digest": f"sha256:{hashlib.sha256(raw).hexdigest()}",
        }
    )

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _invalid_item(
            item_id=item_id,
            path=file_path,
            code=AUTHORITY_SETTLEMENT_PAYLOAD_CORRUPT,
            primary_issue="corrupt",
            message=str(exc),
            fields=fields,
            source_identity=source_identity,
        )
    if not isinstance(payload, dict):
        return _invalid_item(
            item_id=item_id,
            path=file_path,
            code=AUTHORITY_SETTLEMENT_PAYLOAD_CORRUPT,
            primary_issue="corrupt",
            message="authority settlement pending record must be an object",
            fields=fields,
            source_identity=source_identity,
        )
    fields.update(_payload_fields(payload))
    if payload.get("schema") != AUTHORITY_SETTLEMENT_PENDING_SCHEMA:
        return _invalid_item(
            item_id=item_id,
            path=file_path,
            code=AUTHORITY_SETTLEMENT_SCHEMA_MISMATCH,
            primary_issue="schema_mismatch",
            message=f"unsupported authority settlement schema: {payload.get('schema')!r}",
            fields=fields,
            source_identity=source_identity,
        )
    try:
        pending = PendingAuthoritySettlement.from_dict(payload)
    except (TypeError, ValueError) as exc:
        return _invalid_item(
            item_id=item_id,
            path=file_path,
            code=AUTHORITY_SETTLEMENT_SCHEMA_MISMATCH,
            primary_issue="schema_mismatch",
            message=str(exc),
            fields=fields,
            source_identity=source_identity,
        )

    fields.update(_record_fields(pending))
    identity_issue = _identity_issue(
        file_path,
        pending.settlement_operation_id,
        expected_settlement_operation_id=expected_settlement_operation_id,
    )
    if identity_issue is not None:
        fields["identity_match"] = False
        return _invalid_item(
            item_id=item_id,
            path=file_path,
            code=AUTHORITY_SETTLEMENT_IDENTITY_MISMATCH,
            primary_issue="identity_mismatch",
            message=identity_issue,
            fields=fields,
            source_identity=source_identity,
        )
    fields["identity_match"] = True
    return _item(
        item_id=item_id,
        path=file_path,
        health=present_valid(authority_role="authoritative"),
        fields=fields,
        source_identity=source_identity,
    )


def _locator_fields(path: Path) -> dict[str, object]:
    stem = path.name.removesuffix(".json")
    component = classify_locator_component(stem)
    fields = {
        "filename": path.name,
        "locator_component": component.raw_component,
        "locator_encoding": component.encoding,
        "locator_reversible": component.reversible,
    }
    if component.decoded_value is not None:
        fields["locator_settlement_operation_id"] = component.decoded_value
    if component.issue is not None:
        fields["locator_issue"] = component.issue
    return fields


def _payload_fields(payload: dict[str, Any]) -> dict[str, object]:
    fields: dict[str, object] = {}
    for key, value in payload.items():
        if isinstance(value, (str, int, bool)) or value is None:
            fields[f"payload_{key}"] = value
    return fields


def _record_fields(pending: PendingAuthoritySettlement) -> dict[str, object]:
    return {
        "settlement_operation_id": pending.settlement_operation_id,
        "authority_operation_id": pending.authority_operation_id,
        "scope_name": pending.scope_name,
        "scope_ref": pending.scope_ref,
        "scope_instance_id": pending.scope_instance_id,
        "scope_world_id": pending.scope_world_id,
        "parent_scope_name": pending.parent_scope_name,
        "parent_scope_ref": pending.parent_scope_ref,
        "parent_scope_instance_id": pending.parent_scope_instance_id,
        "parent_scope_world_id": pending.parent_scope_world_id,
        "cohort_id": pending.cohort_id,
        "candidate_digest": pending.candidate_digest,
        "outcome": pending.outcome,
        "settlement": pending.settlement,
        "commit_outcome": pending.commit_outcome,
        "reason_code": pending.reason_code,
        "transaction_kind": pending.transaction_kind,
        "selection_operation_id": pending.selection_operation_id,
        "workspace_publication_operation_id": pending.workspace_publication_operation_id,
        "parent_world_before": pending.parent_world_before,
        "parent_world_after": pending.parent_world_after,
        "phase": pending.phase,
    }


def _identity_issue(
    path: Path,
    settlement_operation_id: str,
    *,
    expected_settlement_operation_id: str | None,
) -> str | None:
    stem = path.name.removesuffix(".json")
    component = classify_locator_component(stem)
    if expected_settlement_operation_id is not None and settlement_operation_id != expected_settlement_operation_id:
        return (
            f"authority settlement payload operation_id {settlement_operation_id!r} "
            f"disagrees with expected operation_id {expected_settlement_operation_id!r}"
        )
    if stem != encode_ref_component(settlement_operation_id):
        if component.encoding == "malformed":
            return "authority settlement pending filename is malformed"
        return "authority settlement payload operation_id disagrees with canonical locator"
    return None


def _item_id(path: Path) -> str:
    return f"authority_settlement_pending:file:{path.name}"


def _invalid_pending_records_message(items: tuple[InventoryItem,...]) -> str:
    details = []
    for item in items:
        issue_codes = ",".join(item.health.issue_codes) or item.health.status
        details.append(f"{authority_settlement_pending_label(item)} [{issue_codes}]")
    return "Cannot recover authority settlements while pending-settlement inventory is invalid: " + "; ".join(
        details
    )


def _item(
    *,
    item_id: str,
    path: Path,
    health: Health,
    fields: dict[str, object],
    source_identity: dict[str, object] | None = None,
    issues: tuple[InventoryIssue,...] = (),
) -> InventoryItem:
    return InventoryItem(
        id=item_id,
        domain="authority_settlement",
        kind="authority_settlement_pending",
        locator=str(path),
        source_kind="filesystem_file",
        source_store="coordinator",
        health=health,
        role=("authority", "recovery"),
        fields=fields,
        source_identity=dict(source_identity or {"path": str(path)}),
        issues=issues,
    )


def _invalid_item(
    *,
    item_id: str,
    path: Path,
    code: str,
    primary_issue: HealthIssue,
    message: str,
    fields: dict[str, object],
    source_identity: dict[str, object],
) -> InventoryItem:
    issue = _issue(item_id, code, message, path)
    return _item(
        item_id=item_id,
        path=path,
        health=present_invalid(
            primary_issue=primary_issue,
            issue_codes=(code,),
            authority_role="authoritative",
        ),
        fields=fields,
        source_identity=source_identity,
        issues=(issue,),
    )


def _issue(subject_id: str, code: str, message: str, path: Path) -> InventoryIssue:
    return InventoryIssue(
        id=issue_id(subject_id, code),
        code=code,
        message=message,
        subject_id=subject_id,
        locator=str(path),
        recovery_hint="Run recover_authority_settlements() before starting mutating work.",
    )
