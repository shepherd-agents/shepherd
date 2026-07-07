"""Internal filesystem authority helpers.

This module is intentionally vcs-core local. It classifies filesystem candidate
records and carries data-only decisions, but it does not import Shepherd's
``Match`` evaluator. The dialect can evaluate the flat request views and pass
the resulting decisions back into the authority-enabled merge path.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, get_args

from vcs_core._errors import InvalidRepositoryStateError, VcsCoreError
from vcs_core._permission_plan_evidence import (
    PermissionPlanEvidenceError,
    normalize_permission_plan_descriptor,
)
from vcs_core._permission_plan_evidence import (
    permission_plan_digest as compute_permission_plan_digest,
)
from vcs_core._world_refs import encode_ref_component

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from vcs_core.types import EffectRecord, ScopeInfo


AuthorityOutcome = Literal["allowed", "denied", "refused"]
AuthoritySettlement = Literal["merged", "discarded", "selected", "not_selected", "applied", "not_applied"]
RetainedOutputAuthoritySettlement = Literal["selected", "not_selected"]
RetainedOutputApplicationAuthoritySettlement = Literal["applied", "not_applied"]
AuthorityTransactionKind = Literal["filesystem_merge", "retained_output_selection", "retained_output_application"]
AuthoritySettlementPhase = Literal["pending_action", "adopted", "discarded"]
RetainedOutputClassificationBasis = Literal["exact_tree_diff", "changed_paths_fallback", "unclassifiable"]
AUTHZ_MATCH_VIEW_CLASSIFICATION_BASES = frozenset(
    {"effect_record", "exact_tree_diff", "changed_paths_fallback", "unclassifiable"}
)
AuthorityCommitOutcome = Literal[
    "pending",
    "merged",
    "selected",
    "applied",
    "discarded_with_cohort",
    "not_committed_denied",
    "not_committed_refused",
    "not_selected_denied",
    "not_selected_refused",
    "not_applied_denied",
    "not_applied_refused",
    "commit_failed_non_authority",
]

# Single source of truth for the closed vocabularies above (the future settlement-action
# registry's vocabulary column, g10): validators and parse helpers consume these derived sets so
# a Literal member added for a new verb cannot drift out of a hand-maintained copy.
AUTHORITY_OUTCOMES: frozenset[str] = frozenset(get_args(AuthorityOutcome))
AUTHORITY_SETTLEMENTS: frozenset[str] = frozenset(get_args(AuthoritySettlement))
AUTHORITY_TRANSACTION_KINDS: frozenset[str] = frozenset(get_args(AuthorityTransactionKind))
AUTHORITY_COMMIT_OUTCOMES: frozenset[str] = frozenset(get_args(AuthorityCommitOutcome))
AUTHORITY_SETTLEMENT_PHASES: frozenset[str] = frozenset(get_args(AuthoritySettlementPhase))
# The kind→route derivation (T1 D7): the PermissionPlan route is a function of the transaction
# kind, never a free constant threaded separately.
AUTHORITY_ROUTE_BY_TRANSACTION_KIND: dict[str, str] = {
    "filesystem_merge": "carrier_diff",
    "retained_output_selection": "retained_output_selection",
    "retained_output_application": "retained_output_application",
}
# Kind-conditional settlement vocabulary (validated in PendingAuthoritySettlement.__post_init__).
AUTHORITY_SETTLEMENTS_BY_TRANSACTION_KIND: dict[str, frozenset[str]] = {
    "filesystem_merge": frozenset({"merged", "discarded"}),
    "retained_output_selection": frozenset(get_args(RetainedOutputAuthoritySettlement)),
    "retained_output_application": frozenset(get_args(RetainedOutputApplicationAuthoritySettlement)),
}


class AuthorityDecisionError(VcsCoreError, ValueError):
    """Raised when an authority decision provider returns an invalid decision."""


class AuthorityMergeDriftError(VcsCoreError, ValueError):
    """Raised internally when a prepared candidate cohort no longer matches."""


class AuthorityBindingRootsError(VcsCoreError, ValueError):
    """Raised when a supplied GitRepo binding map cannot be used safely."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class AuthzMatchView:
    """Flat classifier-owned view passed to a policy evaluator."""

    domain: str
    kind: str
    binding_ref: str
    action: str
    path: str
    mutates: bool
    reversibility: str
    control_plane: bool
    monitor_basis: str
    route: str
    classification_basis: str = "effect_record"

    def __post_init__(self) -> None:
        _require_view_str(self.domain, "domain")
        if self.domain != "gitrepo.v0":
            raise ValueError(f"GitRepo authority view domain is unsupported: {self.domain!r}")
        _require_view_str(self.kind, "kind")
        if self.kind == "gitrepo.refused":
            _require_view_str(self.binding_ref, "binding_ref", allow_empty=True)
        else:
            _require_view_str(self.binding_ref, "binding_ref")
        _require_view_str(self.action, "action")
        _require_view_str(self.path, "path", allow_empty=True)
        _require_view_bool(self.mutates, "mutates")
        if self.mutates and self.kind != "gitrepo.refused" and self.path == "":
            raise ValueError("GitRepo authority view mutating facts require a path")
        _require_view_str(self.reversibility, "reversibility")
        _require_view_bool(self.control_plane, "control_plane")
        _require_view_str(self.monitor_basis, "monitor_basis")
        _require_view_str(self.route, "route")
        _require_view_str(self.classification_basis, "classification_basis")
        if self.classification_basis not in AUTHZ_MATCH_VIEW_CLASSIFICATION_BASES:
            raise ValueError(f"GitRepo authority classification basis is unsupported: {self.classification_basis!r}")

    def as_mapping(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "kind": self.kind,
            "binding_ref": self.binding_ref,
            "action": self.action,
            "path": self.path,
            "mutates": self.mutates,
            "reversibility": self.reversibility,
            "control_plane": self.control_plane,
            "monitor_basis": self.monitor_basis,
            "route": self.route,
            "classification_basis": self.classification_basis,
        }


@dataclass(frozen=True)
class GitRepoAuthorityRequest:
    """One classified candidate filesystem effect."""

    request_id: str
    candidate_effect_ref: str
    candidate_index: int
    effect_type: str
    substrate: str
    scope_ref: str
    parent_scope_ref: str
    candidate_digest: str
    match_view: AuthzMatchView
    reason_code: str | None = None

    def as_decision_input(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "candidate_effect_ref": self.candidate_effect_ref,
            "candidate_index": self.candidate_index,
            "effect_type": self.effect_type,
            "substrate": self.substrate,
            "scope_ref": self.scope_ref,
            "parent_scope_ref": self.parent_scope_ref,
            "candidate_digest": self.candidate_digest,
            "match_view": self.match_view.as_mapping(),
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class RetainedOutputAuthorityRequest:
    """One classified retained-output selection authority request."""

    request_id: str
    candidate_effect_ref: str
    candidate_index: int
    scope_ref: str
    parent_scope_ref: str
    handoff_ref: str
    candidate_digest: str
    classification_basis: RetainedOutputClassificationBasis
    match_view: AuthzMatchView
    reason_code: str | None = None

    def as_decision_input(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "candidate_effect_ref": self.candidate_effect_ref,
            "candidate_index": self.candidate_index,
            "scope_ref": self.scope_ref,
            "parent_scope_ref": self.parent_scope_ref,
            "handoff_ref": self.handoff_ref,
            "candidate_digest": self.candidate_digest,
            "classification_basis": self.classification_basis,
            "match_view": self.match_view.as_mapping(),
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class AuthorityDecision:
    """Data-only decision returned by the privileged authority evaluator."""

    outcome: AuthorityOutcome
    reason_code: str
    request_id: str | None = None
    matched_grant_ref: str | None = None
    monitor_basis: str = "carrier_check_at_commit"
    completeness: str = "complete"


@dataclass(frozen=True)
class AuthorityDecisionRecord:
    """Decision plus the request it judged."""

    decision_id: str
    request: GitRepoAuthorityRequest
    outcome: AuthorityOutcome
    reason_code: str
    request_record_digest: str
    match_view_digest: str
    effective_match_digest: str | None
    authority_surface_plan_digest: str | None
    permission_plan_digest: str | None
    permission_plan_descriptor: dict[str, object] | None
    matched_grant_ref: str | None
    monitor_basis: str
    completeness: str
    commit_outcome: AuthorityCommitOutcome = "pending"

    def to_metadata(
        self,
        *,
        cohort_id: str,
        operation_id: str,
        authority_context: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        metadata: dict[str, object] = {
            "schema": "vcscore/authority-decision/v1",
            "authority_operation_id": operation_id,
            "cohort_id": cohort_id,
            "decision_id": self.decision_id,
            "request": self.request.as_decision_input(),
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "request_record_digest": self.request_record_digest,
            "match_view_digest": self.match_view_digest,
            "effective_match_digest": self.effective_match_digest,
            "authority_surface_plan_digest": self.authority_surface_plan_digest,
            "permission_plan_digest": self.permission_plan_digest,
            "permission_plan_descriptor": (
                None if self.permission_plan_descriptor is None else dict(self.permission_plan_descriptor)
            ),
            "matched_grant_ref": self.matched_grant_ref,
            "monitor_basis": self.monitor_basis,
            "completeness": self.completeness,
            "commit_outcome": self.commit_outcome,
        }
        _add_authority_context(metadata, authority_context)
        return metadata


@dataclass(frozen=True)
class RetainedOutputAuthorityDecisionRecord:
    """Decision plus the retained-output authority request it judged."""

    decision_id: str
    request: RetainedOutputAuthorityRequest
    outcome: AuthorityOutcome
    reason_code: str
    request_record_digest: str
    match_view_digest: str
    effective_match_digest: str | None
    authority_surface_plan_digest: str | None
    permission_plan_digest: str | None
    permission_plan_descriptor: dict[str, object] | None
    matched_grant_ref: str | None
    monitor_basis: str
    completeness: str
    commit_outcome: str = "pending"

    def to_metadata(
        self,
        *,
        cohort_id: str,
        operation_id: str,
        authority_context: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        metadata: dict[str, object] = {
            "schema": "vcscore/retained-output-authority-decision/v1",
            "authority_operation_id": operation_id,
            "cohort_id": cohort_id,
            "decision_id": self.decision_id,
            "request": self.request.as_decision_input(),
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "request_record_digest": self.request_record_digest,
            "match_view_digest": self.match_view_digest,
            "effective_match_digest": self.effective_match_digest,
            "authority_surface_plan_digest": self.authority_surface_plan_digest,
            "permission_plan_digest": self.permission_plan_digest,
            "permission_plan_descriptor": (
                None if self.permission_plan_descriptor is None else dict(self.permission_plan_descriptor)
            ),
            "matched_grant_ref": self.matched_grant_ref,
            "monitor_basis": self.monitor_basis,
            "completeness": self.completeness,
            "commit_outcome": self.commit_outcome,
        }
        _add_authority_context(metadata, authority_context)
        return metadata


@dataclass(frozen=True)
class PreparedAuthorityMerge:
    """Prepared candidate cohort that must be settled without widening."""

    preparation_id: str
    idempotency_key: str
    cohort_id: str
    scope_ref: str
    scope_name: str
    scope_instance_id: str
    parent_scope_ref: str
    candidate_batch_ref: str
    candidate_digest: str
    prepared_substrate_names: tuple[str, ...]
    prepared_substrate_digests: dict[str, str]

    def to_metadata(
        self,
        *,
        operation_id: str,
        authority_context: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        metadata: dict[str, object] = {
            "schema": "vcscore/prepared-authority-merge/v1",
            "authority_operation_id": operation_id,
            "preparation_id": self.preparation_id,
            "idempotency_key": self.idempotency_key,
            "cohort_id": self.cohort_id,
            "authority_scope": {
                "ref": self.scope_ref,
                "name": self.scope_name,
                "instance_id": self.scope_instance_id,
                "parent_ref": self.parent_scope_ref,
            },
            "candidate_batch_ref": self.candidate_batch_ref,
            "candidate_digest": self.candidate_digest,
            "prepared_substrate_names": list(self.prepared_substrate_names),
            "prepared_substrate_digests": dict(self.prepared_substrate_digests),
        }
        _add_authority_context(metadata, authority_context)
        return metadata


@dataclass(frozen=True)
class PreparedRetainedOutputSelection:
    """Prepared retained-output settlement authority transaction (selection or application).

    ``transaction_kind`` discriminates the settling verb (T1 D7): for
    ``retained_output_application`` the ``selection_operation_id`` field carries the settling
    *apply* operation id and the pending/final records spell it ``application_operation_id`` —
    the prepared record keeps one field so candidate/cohort identity stays verb-independent.
    """

    preparation_id: str
    cohort_id: str
    selection_operation_id: str
    scope_ref: str
    scope_name: str
    scope_instance_id: str
    parent_scope_ref: str
    handoff_ref: str
    candidate_digest: str
    binding: str
    candidate_head: str
    parent_basis_world_oid: str
    output_world_oid: str
    changed_paths: tuple[str, ...]
    classification_basis: RetainedOutputClassificationBasis
    transaction_kind: AuthorityTransactionKind = "retained_output_selection"

    def __post_init__(self) -> None:
        if self.transaction_kind not in {"retained_output_selection", "retained_output_application"}:
            raise ValueError(f"prepared retained-output authority kind is unsupported: {self.transaction_kind!r}")

    @property
    def route(self) -> str:
        """The PermissionPlan route derived from the transaction kind (never a free constant)."""
        return AUTHORITY_ROUTE_BY_TRANSACTION_KIND[self.transaction_kind]

    def to_metadata(
        self,
        *,
        operation_id: str,
        authority_context: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        metadata: dict[str, object] = {
            "schema": "vcscore/prepared-retained-output-selection/v1",
            "transaction_kind": self.transaction_kind,
            "authority_operation_id": operation_id,
            "preparation_id": self.preparation_id,
            "cohort_id": self.cohort_id,
            "selection_operation_id": self.selection_operation_id,
            "authority_scope": {
                "ref": self.scope_ref,
                "name": self.scope_name,
                "instance_id": self.scope_instance_id,
                "parent_ref": self.parent_scope_ref,
            },
            "handoff_ref": self.handoff_ref,
            "candidate_digest": self.candidate_digest,
            "binding": self.binding,
            "candidate_head": self.candidate_head,
            "parent_basis_world_oid": self.parent_basis_world_oid,
            "output_world_oid": self.output_world_oid,
            "changed_paths": list(self.changed_paths),
            "classification_basis": self.classification_basis,
        }
        _add_authority_context(metadata, authority_context)
        return metadata


@dataclass(frozen=True)
class AuthorityMergeResult:
    """Return value for the internal authority-enabled merge path."""

    scope_name: str
    authority_operation_id: str
    settlement_operation_id: str
    cohort_id: str
    candidate_digest: str
    outcome: AuthorityOutcome
    settlement: AuthoritySettlement
    parent_world_before: str | None
    parent_world_after: str | None
    decisions: tuple[AuthorityDecisionRecord, ...]
    permission_plan_digest: str | None = None
    permission_plan_descriptor: dict[str, object] | None = None


class DecisionProvider(Protocol):
    def __call__(self, request: GitRepoAuthorityRequest) -> AuthorityDecision | AuthorityOutcome: ...


class RetainedOutputDecisionProvider(Protocol):
    def __call__(self, request: RetainedOutputAuthorityRequest) -> AuthorityDecision | AuthorityOutcome: ...


AUTHORITY_SETTLEMENT_PENDING_SCHEMA = "vcscore/authority-settlement-pending/v1"

_ACTION_BY_EFFECT = {
    "FileRead": ("gitrepo.file_read", "git_repo.file_read", False),
    "FileCreate": ("gitrepo.file_create", "git_repo.file_create", True),
    "FilePatch": ("gitrepo.file_patch", "git_repo.file_patch", True),
    "FileDelete": ("gitrepo.file_delete", "git_repo.file_delete", True),
}

_ACTION_BY_CHANGE_STATUS = {
    "added": ("gitrepo.file_create", "git_repo.file_create", True),
    "modified": ("gitrepo.file_patch", "git_repo.file_patch", True),
    "deleted": ("gitrepo.file_delete", "git_repo.file_delete", True),
}


@dataclass(frozen=True)
class PendingAuthoritySettlement:
    """Durable recovery record for an authority transaction whose settlement is not closed."""

    settlement_operation_id: str
    authority_operation_id: str
    scope_name: str
    scope_ref: str
    scope_instance_id: str
    scope_world_id: str | None
    parent_scope_name: str
    parent_scope_ref: str
    parent_scope_instance_id: str
    parent_scope_world_id: str | None
    cohort_id: str
    candidate_digest: str
    outcome: AuthorityOutcome
    settlement: AuthoritySettlement
    commit_outcome: AuthorityCommitOutcome
    decision_ids: tuple[str, ...]
    reason_code: str
    transaction_kind: AuthorityTransactionKind = "filesystem_merge"
    selection_operation_id: str | None = None
    # The settling apply operation for kind "retained_output_application" (T1 D7) — the
    # application twin of selection_operation_id; exactly one of the two is set per kind.
    application_operation_id: str | None = None
    workspace_publication_operation_id: str | None = None
    parent_world_before: str | None = None
    parent_world_after: str | None = None
    authority_context: dict[str, object] | None = None
    permission_plan_digest: str | None = None
    permission_plan_descriptor: dict[str, object] | None = None
    phase: AuthoritySettlementPhase = "pending_action"
    created_at_unix_ns: int = 0
    updated_at_unix_ns: int = 0
    schema: str = AUTHORITY_SETTLEMENT_PENDING_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != AUTHORITY_SETTLEMENT_PENDING_SCHEMA:
            raise ValueError("authority settlement pending record has unsupported schema")
        for field_name in (
            "settlement_operation_id",
            "authority_operation_id",
            "scope_name",
            "scope_ref",
            "scope_instance_id",
            "parent_scope_name",
            "parent_scope_ref",
            "parent_scope_instance_id",
            "cohort_id",
            "candidate_digest",
            "reason_code",
        ):
            _require_non_empty_str(getattr(self, field_name), field_name)
        if self.scope_world_id is not None:
            _require_non_empty_str(self.scope_world_id, "scope_world_id")
        if self.parent_scope_world_id is not None:
            _require_non_empty_str(self.parent_scope_world_id, "parent_scope_world_id")
        if self.outcome not in AUTHORITY_OUTCOMES:
            raise ValueError(f"authority outcome is unsupported: {self.outcome!r}")
        if self.transaction_kind not in AUTHORITY_TRANSACTION_KINDS:
            raise ValueError(f"authority transaction kind is unsupported: {self.transaction_kind!r}")
        if self.settlement not in AUTHORITY_SETTLEMENTS:
            raise ValueError(f"authority settlement is unsupported: {self.settlement!r}")
        allowed_settlements = AUTHORITY_SETTLEMENTS_BY_TRANSACTION_KIND[self.transaction_kind]
        if self.settlement not in allowed_settlements:
            raise ValueError(
                f"{self.transaction_kind} authority settlement must be one of {sorted(allowed_settlements)}; "
                f"got {self.settlement!r}"
            )
        if self.transaction_kind == "retained_output_selection":
            _require_non_empty_str(self.selection_operation_id, "selection_operation_id")
            if self.workspace_publication_operation_id is not None:
                raise ValueError("retained-output authority settlement cannot carry workspace publication id")
            if self.application_operation_id is not None:
                raise ValueError("retained-output selection authority settlement cannot carry application id")
        elif self.transaction_kind == "retained_output_application":
            _require_non_empty_str(self.application_operation_id, "application_operation_id")
            if self.workspace_publication_operation_id is not None:
                raise ValueError("retained-output application authority settlement cannot carry publication id")
            if self.selection_operation_id is not None:
                raise ValueError("retained-output application authority settlement cannot carry selection id")
        else:
            if self.selection_operation_id is not None:
                raise ValueError("filesystem authority settlement cannot carry selection_operation_id")
            if self.application_operation_id is not None:
                raise ValueError("filesystem authority settlement cannot carry application_operation_id")
        if self.workspace_publication_operation_id is not None:
            _require_non_empty_str(self.workspace_publication_operation_id, "workspace_publication_operation_id")
        if self.parent_world_before is not None:
            _require_non_empty_str(self.parent_world_before, "parent_world_before")
        if self.parent_world_after is not None:
            _require_non_empty_str(self.parent_world_after, "parent_world_after")
        object.__setattr__(self, "authority_context", normalize_authority_context(self.authority_context))
        if self.permission_plan_digest is not None:
            _require_non_empty_str(self.permission_plan_digest, "permission_plan_digest")
        permission_plan_descriptor = _optional_json_mapping(
            self.permission_plan_descriptor,
            "permission_plan_descriptor",
        )
        if (self.permission_plan_digest is None) != (permission_plan_descriptor is None):
            raise ValueError("authority settlement pending PermissionPlan evidence must include digest and descriptor")
        if permission_plan_descriptor is not None:
            try:
                permission_plan_descriptor = normalize_permission_plan_descriptor(permission_plan_descriptor)
            except PermissionPlanEvidenceError as exc:
                raise ValueError(f"authority settlement pending PermissionPlan descriptor is invalid: {exc}") from exc
            if compute_permission_plan_digest(permission_plan_descriptor) != self.permission_plan_digest:
                raise ValueError("authority settlement pending PermissionPlan digest mismatch")
            expected_route = AUTHORITY_ROUTE_BY_TRANSACTION_KIND[self.transaction_kind]
            assignments = cast("list[dict[str, object]]", permission_plan_descriptor["assignments"])
            if len(assignments) != 1 or assignments[0].get("route") != expected_route:
                raise ValueError("authority settlement pending PermissionPlan route mismatch")
        object.__setattr__(self, "permission_plan_descriptor", permission_plan_descriptor)
        if self.commit_outcome not in AUTHORITY_COMMIT_OUTCOMES:
            raise ValueError(f"authority commit outcome is unsupported: {self.commit_outcome!r}")
        if self.phase not in AUTHORITY_SETTLEMENT_PHASES:
            raise ValueError(f"authority settlement phase is unsupported: {self.phase!r}")
        for index, decision_id in enumerate(self.decision_ids):
            _require_non_empty_str(decision_id, f"decision_ids[{index}]")
        _require_int(self.created_at_unix_ns, "created_at_unix_ns")
        _require_int(self.updated_at_unix_ns, "updated_at_unix_ns")

    def with_update(self, **changes: object) -> PendingAuthoritySettlement:
        now = time.time_ns()
        if self.created_at_unix_ns == 0 and "created_at_unix_ns" not in changes:
            changes["created_at_unix_ns"] = now
        changes["updated_at_unix_ns"] = now
        return replace(self, **cast("dict[str, Any]", changes))

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        return {key: value for key, value in payload.items() if value is not None}

    @classmethod
    def from_dict(cls, data: object) -> PendingAuthoritySettlement:
        if not isinstance(data, dict):
            raise TypeError("authority settlement pending record must be an object")
        # Strict serde (persisted-evidence-serde-policy §2.1): reject unknown fields rather than
        # silently dropping them, so a drifted writer fails closed instead of losing evidence. The
        # allowed set is derived from the dataclass fields, so it auto-tracks additive vocabulary
        # (e.g. the D7 application-authority fields) without a second list to maintain.
        allowed = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ValueError(f"authority settlement pending record has unsupported field(s): {', '.join(unknown)}")
        return cls(
            settlement_operation_id=_required_str(data, "settlement_operation_id"),
            authority_operation_id=_required_str(data, "authority_operation_id"),
            scope_name=_required_str(data, "scope_name"),
            scope_ref=_required_str(data, "scope_ref"),
            scope_instance_id=_required_str(data, "scope_instance_id"),
            scope_world_id=_optional_str(data, "scope_world_id"),
            parent_scope_name=_required_str(data, "parent_scope_name"),
            parent_scope_ref=_required_str(data, "parent_scope_ref"),
            parent_scope_instance_id=_required_str(data, "parent_scope_instance_id"),
            parent_scope_world_id=_optional_str(data, "parent_scope_world_id"),
            cohort_id=_required_str(data, "cohort_id"),
            candidate_digest=_required_str(data, "candidate_digest"),
            outcome=_authority_outcome(data.get("outcome")),
            settlement=_authority_settlement(data.get("settlement")),
            commit_outcome=_authority_commit_outcome(data.get("commit_outcome")),
            decision_ids=_str_tuple(data.get("decision_ids"), "decision_ids"),
            reason_code=_required_str(data, "reason_code"),
            transaction_kind=_authority_transaction_kind(data.get("transaction_kind", "filesystem_merge")),
            selection_operation_id=_optional_str(data, "selection_operation_id"),
            application_operation_id=_optional_str(data, "application_operation_id"),
            workspace_publication_operation_id=_optional_str(data, "workspace_publication_operation_id"),
            parent_world_before=_optional_str(data, "parent_world_before"),
            parent_world_after=_optional_str(data, "parent_world_after"),
            authority_context=normalize_authority_context(data.get("authority_context")),
            permission_plan_digest=_optional_str(data, "permission_plan_digest"),
            permission_plan_descriptor=_optional_json_mapping(
                data.get("permission_plan_descriptor"),
                "permission_plan_descriptor",
            ),
            phase=_authority_settlement_phase(data.get("phase", "pending_action")),
            created_at_unix_ns=_int(data.get("created_at_unix_ns", 0), "created_at_unix_ns"),
            updated_at_unix_ns=_int(data.get("updated_at_unix_ns", 0), "updated_at_unix_ns"),
            schema=_required_str(data, "schema"),
        )


def normalize_gitrepo_binding_roots(binding_roots: Mapping[str, str]) -> dict[str, str]:
    """Validate and normalize a GitRepo binding map.

    Invalid binding maps are authority-surface errors, not ordinary misses. A
    malformed map must fail the cohort closed instead of silently falling back
    to a broader binding.
    """
    normalized: dict[str, str] = {}
    for binding_ref, raw_root in binding_roots.items():
        if not isinstance(binding_ref, str) or not binding_ref:
            raise AuthorityBindingRootsError(
                "invalid_binding_roots",
                f"GitRepo authority binding ref must be a non-empty string: {binding_ref!r}",
            )
        root = _normalize_binding_root(raw_root)
        if root is None:
            raise AuthorityBindingRootsError(
                "invalid_binding_roots",
                f"GitRepo authority binding root for {binding_ref!r} is invalid: {raw_root!r}",
            )
        normalized[binding_ref] = root
    return normalized


def classify_gitrepo_authority_request(
    effect: EffectRecord,
    *,
    candidate_index: int,
    candidate_digest: str,
    substrate: str,
    scope: ScopeInfo,
    parent: ScopeInfo,
    binding_roots: Mapping[str, str],
    monitor_basis: str,
    candidate_effect_ref: str,
) -> GitRepoAuthorityRequest:
    """Classify one filesystem candidate into a flat GitRepo authority request."""
    effect_type = effect.effect_type
    request_id = _short_digest(
        {
            "candidate_digest": candidate_digest,
            "candidate_index": candidate_index,
            "effect_type": effect_type,
            "scope_ref": scope.ref,
        }
    )
    action_row = _ACTION_BY_EFFECT.get(effect_type)
    raw_path = effect.metadata.get("path")
    if not isinstance(raw_path, str) or raw_path == "":
        return _refused_request(
            request_id,
            effect=effect,
            candidate_index=candidate_index,
            candidate_digest=candidate_digest,
            candidate_effect_ref=candidate_effect_ref,
            substrate=substrate,
            scope=scope,
            parent=parent,
            reason_code="missing_path",
            monitor_basis=monitor_basis,
        )
    path = _normalize_candidate_path(raw_path)
    if path is None:
        return _refused_request(
            request_id,
            effect=effect,
            candidate_index=candidate_index,
            candidate_digest=candidate_digest,
            candidate_effect_ref=candidate_effect_ref,
            substrate=substrate,
            scope=scope,
            parent=parent,
            reason_code="invalid_path",
            monitor_basis=monitor_basis,
            path=raw_path,
        )
    if path == ".git" or path.startswith(".git/"):
        return _refused_request(
            request_id,
            effect=effect,
            candidate_index=candidate_index,
            candidate_digest=candidate_digest,
            candidate_effect_ref=candidate_effect_ref,
            substrate=substrate,
            scope=scope,
            parent=parent,
            reason_code="raw_git_control_plane",
            monitor_basis=monitor_basis,
            path=path,
            control_plane=True,
        )
    if action_row is None:
        return _refused_request(
            request_id,
            effect=effect,
            candidate_index=candidate_index,
            candidate_digest=candidate_digest,
            candidate_effect_ref=candidate_effect_ref,
            substrate=substrate,
            scope=scope,
            parent=parent,
            reason_code="unknown_effect_type",
            monitor_basis=monitor_basis,
            path=path,
        )
    matches = _matching_bindings(path, binding_roots)
    if not matches:
        return _refused_request(
            request_id,
            effect=effect,
            candidate_index=candidate_index,
            candidate_digest=candidate_digest,
            candidate_effect_ref=candidate_effect_ref,
            substrate=substrate,
            scope=scope,
            parent=parent,
            reason_code="outside_declared_bindings",
            monitor_basis=monitor_basis,
            path=path,
        )
    if len(matches) > 1:
        return _refused_request(
            request_id,
            effect=effect,
            candidate_index=candidate_index,
            candidate_digest=candidate_digest,
            candidate_effect_ref=candidate_effect_ref,
            substrate=substrate,
            scope=scope,
            parent=parent,
            reason_code="ambiguous_binding",
            monitor_basis=monitor_basis,
            path=path,
        )
    binding_ref, root = matches[0]
    if root != "" and path == root:
        return _refused_request(
            request_id,
            effect=effect,
            candidate_index=candidate_index,
            candidate_digest=candidate_digest,
            candidate_effect_ref=candidate_effect_ref,
            substrate=substrate,
            scope=scope,
            parent=parent,
            reason_code="binding_root_path",
            monitor_basis=monitor_basis,
            path=path,
        )
    kind, action, mutates = action_row
    relative_path = path if root == "" else path.removeprefix(f"{root}/")
    return GitRepoAuthorityRequest(
        request_id=request_id,
        candidate_effect_ref=candidate_effect_ref,
        candidate_index=candidate_index,
        effect_type=effect_type,
        substrate=substrate,
        scope_ref=scope.ref,
        parent_scope_ref=parent.ref,
        candidate_digest=candidate_digest,
        match_view=AuthzMatchView(
            domain="gitrepo.v0",
            kind=kind,
            binding_ref=binding_ref,
            action=action,
            path=relative_path,
            mutates=mutates,
            reversibility="reversible",
            control_plane=False,
            monitor_basis=monitor_basis,
            route="carrier_diff",
        ),
    )


def make_decision_record(
    request: GitRepoAuthorityRequest,
    decision: AuthorityDecision | AuthorityOutcome,
    *,
    decision_index: int,
    effective_match_digest: str | None = None,
    authority_surface_plan_digest: str | None = None,
    permission_plan_digest: str | None = None,
    permission_plan_descriptor: Mapping[str, object] | None = None,
) -> AuthorityDecisionRecord:
    if isinstance(decision, str):
        decision = AuthorityDecision(outcome=decision, reason_code=f"{decision}_by_provider")
    if decision.outcome not in {"allowed", "denied", "refused"}:
        raise AuthorityDecisionError(f"unsupported authority outcome: {decision.outcome!r}")
    if decision.request_id is not None and decision.request_id != request.request_id:
        raise AuthorityDecisionError("authority decision request_id does not match request")
    outcome: AuthorityOutcome = decision.outcome
    return AuthorityDecisionRecord(
        decision_id=_short_digest(
            {
                "candidate": request.candidate_effect_ref,
                "decision_index": decision_index,
                "outcome": outcome,
                "request": request.request_id,
            }
        ),
        request=request,
        outcome=outcome,
        reason_code=decision.reason_code,
        request_record_digest=_short_digest(request.as_decision_input()),
        match_view_digest=_short_digest(request.match_view.as_mapping()),
        effective_match_digest=effective_match_digest,
        authority_surface_plan_digest=authority_surface_plan_digest,
        permission_plan_digest=permission_plan_digest,
        permission_plan_descriptor=_optional_json_mapping(
            permission_plan_descriptor,
            "permission_plan_descriptor",
        ),
        matched_grant_ref=decision.matched_grant_ref,
        monitor_basis=decision.monitor_basis,
        completeness=decision.completeness,
    )


def make_retained_output_decision_record(
    request: RetainedOutputAuthorityRequest,
    decision: AuthorityDecision | AuthorityOutcome,
    *,
    decision_index: int,
    effective_match_digest: str | None = None,
    authority_surface_plan_digest: str | None = None,
    permission_plan_digest: str | None = None,
    permission_plan_descriptor: Mapping[str, object] | None = None,
) -> RetainedOutputAuthorityDecisionRecord:
    if isinstance(decision, str):
        decision = AuthorityDecision(outcome=decision, reason_code=f"{decision}_by_provider")
    if decision.outcome not in {"allowed", "denied", "refused"}:
        raise AuthorityDecisionError(f"unsupported authority outcome: {decision.outcome!r}")
    if decision.request_id is not None and decision.request_id != request.request_id:
        raise AuthorityDecisionError("authority decision request_id does not match request")
    outcome: AuthorityOutcome = decision.outcome
    return RetainedOutputAuthorityDecisionRecord(
        decision_id=_short_digest(
            {
                "candidate": request.candidate_effect_ref,
                "decision_index": decision_index,
                "outcome": outcome,
                "request": request.request_id,
            }
        ),
        request=request,
        outcome=outcome,
        reason_code=decision.reason_code,
        request_record_digest=_short_digest(request.as_decision_input()),
        match_view_digest=_short_digest(request.match_view.as_mapping()),
        effective_match_digest=effective_match_digest,
        authority_surface_plan_digest=authority_surface_plan_digest,
        permission_plan_digest=permission_plan_digest,
        permission_plan_descriptor=_optional_json_mapping(
            permission_plan_descriptor,
            "permission_plan_descriptor",
        ),
        matched_grant_ref=decision.matched_grant_ref,
        monitor_basis=decision.monitor_basis,
        completeness=decision.completeness,
    )


def prepare_retained_output_selection_authority(
    *,
    selection_operation_id: str,
    handoff: Any,
    parent: ScopeInfo,
    changed_paths: Sequence[str],
    classification_basis: RetainedOutputClassificationBasis,
    transaction_kind: AuthorityTransactionKind = "retained_output_selection",
) -> PreparedRetainedOutputSelection:
    candidate_digest = _short_digest(
        {
            "scope_ref": handoff.scope_ref,
            "scope_instance_id": handoff.scope_instance_id,
            "handoff_ref": handoff.handoff_ref,
            "candidate_id": handoff.candidate_id,
            "candidate_head": handoff.candidate_head,
            "parent_basis_world_oid": handoff.parent_basis_world_oid,
            "output_world_oid": handoff.output_world_oid,
            "changed_paths": list(changed_paths),
        }
    )
    preparation_id = _short_digest(
        {
            "selection_operation_id": selection_operation_id,
            "parent_scope_ref": parent.ref,
            "candidate_digest": candidate_digest,
        }
    )
    return PreparedRetainedOutputSelection(
        preparation_id=preparation_id,
        cohort_id=f"retained_output_cohort_{candidate_digest}",
        selection_operation_id=selection_operation_id,
        scope_ref=handoff.scope_ref,
        scope_name=handoff.scope_name,
        scope_instance_id=handoff.scope_instance_id,
        parent_scope_ref=parent.ref,
        handoff_ref=handoff.handoff_ref,
        candidate_digest=candidate_digest,
        binding=handoff.binding,
        candidate_head=handoff.candidate_head,
        parent_basis_world_oid=handoff.parent_basis_world_oid,
        output_world_oid=handoff.output_world_oid,
        changed_paths=tuple(changed_paths),
        classification_basis=classification_basis,
        transaction_kind=transaction_kind,
    )


def classify_retained_output_authority_request(
    *,
    prepared: PreparedRetainedOutputSelection,
    candidate_index: int,
    path: str,
    status: str,
    mutates: bool,
    classification_basis: RetainedOutputClassificationBasis | None = None,
    reason_code: str | None = None,
    monitor_basis: str = "carrier_check_at_commit",
) -> RetainedOutputAuthorityRequest:
    classification_basis = classification_basis or prepared.classification_basis
    request_id = _short_digest(
        {
            "candidate_digest": prepared.candidate_digest,
            "candidate_index": candidate_index,
            "path": path,
            "status": status,
            "scope_ref": prepared.scope_ref,
        }
    )
    candidate_effect_ref = f"retained-output:{candidate_index}"
    normalized_path = ""
    control_plane = False
    effective_reason = reason_code
    if path:
        normalized = _normalize_candidate_path(path)
        if normalized is None:
            effective_reason = effective_reason or "invalid_path"
            normalized_path = path
        else:
            normalized_path = normalized
            control_plane = normalized == ".git" or normalized.startswith(".git/")
            if control_plane:
                effective_reason = effective_reason or "raw_git_control_plane"
    action_row = _ACTION_BY_CHANGE_STATUS.get(status)
    if effective_reason is not None or (mutates and action_row is None):
        return RetainedOutputAuthorityRequest(
            request_id=request_id,
            candidate_effect_ref=candidate_effect_ref,
            candidate_index=candidate_index,
            scope_ref=prepared.scope_ref,
            parent_scope_ref=prepared.parent_scope_ref,
            handoff_ref=prepared.handoff_ref,
            candidate_digest=prepared.candidate_digest,
            classification_basis=classification_basis,
            reason_code=effective_reason or "unknown_change_status",
            match_view=AuthzMatchView(
                domain="gitrepo.v0",
                kind="gitrepo.refused",
                binding_ref=prepared.binding,
                action="git_repo.refused",
                path=normalized_path,
                mutates=True,
                reversibility="reversible",
                control_plane=control_plane,
                monitor_basis=monitor_basis,
                route=prepared.route,
                classification_basis=classification_basis,
            ),
        )
    if action_row is None:
        kind, action, mutates = ("gitrepo.retained_output_select", "git_repo.retained_output_select", False)
    else:
        kind, action, mutates = action_row
    return RetainedOutputAuthorityRequest(
        request_id=request_id,
        candidate_effect_ref=candidate_effect_ref,
        candidate_index=candidate_index,
        scope_ref=prepared.scope_ref,
        parent_scope_ref=prepared.parent_scope_ref,
        handoff_ref=prepared.handoff_ref,
        candidate_digest=prepared.candidate_digest,
        classification_basis=classification_basis,
        match_view=AuthzMatchView(
            domain="gitrepo.v0",
            kind=kind,
            binding_ref=prepared.binding,
            action=action,
            path=normalized_path,
            mutates=mutates,
            reversibility="reversible",
            control_plane=False,
            monitor_basis=monitor_basis,
            route=prepared.route,
            classification_basis=classification_basis,
        ),
    )


def prepare_authority_merge(
    *,
    scope: ScopeInfo,
    parent: ScopeInfo,
    effects_by_substrate: Mapping[str, Sequence[EffectRecord]],
) -> PreparedAuthorityMerge:
    prepared_substrate_digests = {
        substrate: digest_effects(effects) for substrate, effects in sorted(effects_by_substrate.items())
    }
    candidate_digest = _short_digest(prepared_substrate_digests)
    preparation_id = _short_digest(
        {
            "scope_ref": scope.ref,
            "scope_instance_id": scope.instance_id,
            "parent_scope_ref": parent.ref,
            "candidate_digest": candidate_digest,
        }
    )
    return PreparedAuthorityMerge(
        preparation_id=preparation_id,
        idempotency_key=preparation_id,
        cohort_id=f"cohort_{candidate_digest}",
        scope_ref=scope.ref,
        scope_name=scope.name,
        scope_instance_id=scope.instance_id,
        parent_scope_ref=parent.ref,
        candidate_batch_ref=f"candidate_batch_{candidate_digest}",
        candidate_digest=candidate_digest,
        prepared_substrate_names=tuple(sorted(effects_by_substrate)),
        prepared_substrate_digests=prepared_substrate_digests,
    )


def digest_effects(effects: Sequence[EffectRecord]) -> str:
    return _short_digest([_effect_digest_payload(effect, index=index) for index, effect in enumerate(effects)])


def settlement_metadata(
    *,
    operation_id: str,
    cohort_id: str,
    candidate_digest: str,
    outcome: AuthorityOutcome,
    settlement: str,
    commit_outcome: AuthorityCommitOutcome,
    decision_ids: Sequence[str],
    reason_code: str,
    workspace_publication_operation_id: str | None = None,
    parent_world_before: str | None = None,
    parent_world_after: str | None = None,
    permission_plan_digest: str | None = None,
    permission_plan_descriptor: Mapping[str, object] | None = None,
    authority_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "schema": "vcscore/authority-settlement/v1",
        "authority_operation_id": operation_id,
        "cohort_id": cohort_id,
        "candidate_digest": candidate_digest,
        "outcome": outcome,
        "settlement": settlement,
        "commit_outcome": commit_outcome,
        "decision_ids": list(decision_ids),
        "reason_code": reason_code,
    }
    if workspace_publication_operation_id is not None:
        metadata["workspace_publication_operation_id"] = workspace_publication_operation_id
    if parent_world_before is not None:
        metadata["parent_world_before"] = parent_world_before
    if parent_world_after is not None:
        metadata["parent_world_after"] = parent_world_after
    if permission_plan_digest is not None:
        metadata["permission_plan_digest"] = permission_plan_digest
    if permission_plan_descriptor is not None:
        metadata["permission_plan_descriptor"] = _optional_json_mapping(
            permission_plan_descriptor,
            "permission_plan_descriptor",
        )
    _add_authority_context(metadata, authority_context)
    return metadata


def normalize_authority_context(value: object) -> dict[str, object] | None:
    """Return a deterministic JSON authority context payload.

    Authority context is durable provenance evidence. Unsupported values fail
    closed instead of falling back to repr-based serialization.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("authority context must be an object")
    normalized = _authority_context_json(value, path="authority_context")
    if not isinstance(normalized, dict):
        raise TypeError("authority context must normalize to an object")
    return cast("dict[str, object]", normalized)


def _add_authority_context(metadata: dict[str, object], authority_context: Mapping[str, object] | None) -> None:
    normalized = normalize_authority_context(dict(authority_context) if authority_context is not None else None)
    if normalized is not None:
        metadata["authority_context"] = normalized


def _authority_context_json(value: object, *, path: str) -> object:
    if isinstance(value, dict):
        normalized: dict[str, object] = {}
        for key, item in sorted(value.items(), key=lambda item: str(item[0])):
            if not isinstance(key, str) or not key:
                raise TypeError(f"authority context field {path} has a non-string or empty key")
            if "\0" in key:
                raise ValueError(f"authority context field {path}.{key!r} contains NUL")
            normalized[key] = _authority_context_json(item, path=f"{path}.{key}")
        return normalized
    if isinstance(value, list):
        return [_authority_context_json(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, tuple):
        return [_authority_context_json(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, bytes):
        raise TypeError(f"authority context field {path} must not be bytes")
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"authority context field {path} must be a finite float")
        return value
    if isinstance(value, str):
        if "\0" in value:
            raise ValueError(f"authority context field {path} must not contain NUL")
        return value
    raise TypeError(f"authority context field {path} must be JSON-compatible")


def _optional_json_mapping(value: object, field_name: str) -> dict[str, object] | None:
    if value is None:
        return None
    normalized = _authority_context_json(value, path=field_name)
    if not isinstance(normalized, dict):
        raise TypeError(f"authority settlement field {field_name!r} must be an object or null")
    return cast("dict[str, object]", normalized)


def retained_output_authority_settlement_metadata(
    *,
    operation_id: str,
    cohort_id: str,
    candidate_digest: str,
    outcome: AuthorityOutcome,
    settlement: RetainedOutputAuthoritySettlement | RetainedOutputApplicationAuthoritySettlement,
    commit_outcome: str,
    decision_ids: Sequence[str],
    reason_code: str,
    selection_operation_id: str | None = None,
    application_operation_id: str | None = None,
    permission_plan_digest: str | None = None,
    permission_plan_descriptor: Mapping[str, object] | None = None,
    authority_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if (selection_operation_id is None) == (application_operation_id is None):
        raise ValueError(
            "retained-output authority settlement metadata requires exactly one of "
            "selection_operation_id / application_operation_id"
        )
    settling_key = "selection_operation_id" if selection_operation_id is not None else "application_operation_id"
    metadata: dict[str, object] = {
        "schema": "vcscore/retained-output-authority-settlement/v1",
        "authority_operation_id": operation_id,
        settling_key: selection_operation_id if selection_operation_id is not None else application_operation_id,
        "cohort_id": cohort_id,
        "candidate_digest": candidate_digest,
        "outcome": outcome,
        "settlement": settlement,
        "commit_outcome": commit_outcome,
        "decision_ids": list(decision_ids),
        "reason_code": reason_code,
    }
    if permission_plan_digest is not None:
        metadata["permission_plan_digest"] = permission_plan_digest
    if permission_plan_descriptor is not None:
        metadata["permission_plan_descriptor"] = _optional_json_mapping(
            permission_plan_descriptor,
            "permission_plan_descriptor",
        )
    _add_authority_context(metadata, authority_context)
    return metadata


def _require_view_str(value: object, field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str):
        raise TypeError(f"GitRepo authority view field {field_name!r} must be a string")
    if not allow_empty and not value:
        raise ValueError(f"GitRepo authority view field {field_name!r} must not be empty")
    if "\0" in value:
        raise ValueError(f"GitRepo authority view field {field_name!r} must not contain NUL")


def _require_view_bool(value: object, field_name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"GitRepo authority view field {field_name!r} must be a boolean")


def _refused_request(
    request_id: str,
    *,
    effect: EffectRecord,
    candidate_index: int,
    candidate_digest: str,
    candidate_effect_ref: str,
    substrate: str,
    scope: ScopeInfo,
    parent: ScopeInfo,
    reason_code: str,
    monitor_basis: str,
    path: str = "",
    control_plane: bool = False,
) -> GitRepoAuthorityRequest:
    return GitRepoAuthorityRequest(
        request_id=request_id,
        candidate_effect_ref=candidate_effect_ref,
        candidate_index=candidate_index,
        effect_type=effect.effect_type,
        substrate=substrate,
        scope_ref=scope.ref,
        parent_scope_ref=parent.ref,
        candidate_digest=candidate_digest,
        reason_code=reason_code,
        match_view=AuthzMatchView(
            domain="gitrepo.v0",
            kind="gitrepo.refused",
            binding_ref="",
            action="git_repo.refused",
            path=path,
            mutates=True,
            reversibility="reversible",
            control_plane=control_plane,
            monitor_basis=monitor_basis,
            route="carrier_diff",
        ),
    )


def _matching_bindings(path: str, binding_roots: Mapping[str, str]) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    for binding_ref, root in normalize_gitrepo_binding_roots(binding_roots).items():
        if root == "":
            matches.append((binding_ref, root))
            continue
        if path == root or path.startswith(f"{root}/"):
            matches.append((binding_ref, root))
    if not matches:
        return []
    longest_root_len = max(len(root) for _binding_ref, root in matches)
    return [(binding_ref, root) for binding_ref, root in matches if len(root) == longest_root_len]


def _normalize_candidate_path(raw_path: str) -> str | None:
    pure = PurePosixPath(raw_path)
    if pure.is_absolute():
        return None
    parts = pure.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    return "/".join(parts)


def _normalize_binding_root(raw_root: object) -> str | None:
    if not isinstance(raw_root, str):
        return None
    if raw_root in {"", ".", "/"}:
        return ""
    pure = PurePosixPath(raw_root)
    if pure.is_absolute():
        return None
    parts = pure.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    return "/".join(parts)


def _effect_digest_payload(effect: EffectRecord, *, index: int) -> dict[str, object]:
    return {
        "index": index,
        "effect_type": effect.effect_type,
        "metadata": _jsonish(effect.metadata),
        "workspace_changes": [_workspace_change_payload(change) for change in effect.workspace_changes],
    }


def _workspace_change_payload(change: tuple[str, bytes | None] | tuple[str, bytes | None, int]) -> dict[str, object]:
    content = change[1]
    payload: dict[str, object] = {"path": change[0], "mode": change[2] if len(change) > 2 else None}
    if content is None:
        payload["content"] = None
    else:
        payload["content_sha256"] = hashlib.sha256(content).hexdigest()
        payload["content_len"] = len(content)
    return payload


def _jsonish(value: Any) -> object:
    if isinstance(value, bytes):
        return {"__bytes_b64__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {str(key): _jsonish(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonish(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _short_digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def pending_authority_settlement_records(repo_path: str | Path) -> tuple[PendingAuthoritySettlement, ...]:
    root = _authority_settlement_pending_root(repo_path)
    if not root.exists():
        return ()
    records: list[PendingAuthoritySettlement] = []
    for path in sorted(root.glob("*.json")):
        try:
            records.append(PendingAuthoritySettlement.from_dict(json.loads(path.read_text())))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise InvalidRepositoryStateError(f"Cannot read authority settlement pending record {path}: {exc}") from exc
    return tuple(records)


def read_pending_authority_settlement(
    repo_path: str | Path, settlement_operation_id: str
) -> PendingAuthoritySettlement:
    path = _authority_settlement_pending_path(repo_path, settlement_operation_id)
    try:
        return PendingAuthoritySettlement.from_dict(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise InvalidRepositoryStateError(f"Cannot read authority settlement pending record {path}: {exc}") from exc


def write_pending_authority_settlement(repo_path: str | Path, pending: PendingAuthoritySettlement) -> None:
    _reject_authority_settlement_locator_collision(repo_path, pending)
    path = _authority_settlement_pending_path(repo_path, pending.settlement_operation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(pending.to_dict(), sort_keys=True, separators=(",", ":"))
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(payload)
    tmp.replace(path)


def clear_pending_authority_settlement(repo_path: str | Path, settlement_operation_id: str) -> None:
    _authority_settlement_pending_path(repo_path, settlement_operation_id).unlink(missing_ok=True)


def authority_settlement_pending_labels(repo_path: str | Path) -> tuple[str, ...]:
    labels: list[str] = []
    root = _authority_settlement_pending_root(repo_path)
    if not root.exists():
        return ()
    for path in sorted(root.glob("*.json")):
        try:
            pending = PendingAuthoritySettlement.from_dict(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            labels.append(f"{path.name} (invalid)")
        else:
            labels.append(pending.settlement_operation_id)
    return tuple(labels)


def _authority_settlement_pending_root(repo_path: str | Path) -> Path:
    return Path(repo_path) / "authority" / "pending-settlements"


def _authority_settlement_pending_path(repo_path: str | Path, settlement_operation_id: str) -> Path:
    return _authority_settlement_pending_root(repo_path) / f"{encode_ref_component(settlement_operation_id)}.json"


def _reject_authority_settlement_locator_collision(
    repo_path: str | Path,
    pending: PendingAuthoritySettlement,
) -> None:
    path = _authority_settlement_pending_path(repo_path, pending.settlement_operation_id)
    if not path.exists():
        return
    try:
        existing = PendingAuthoritySettlement.from_dict(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise InvalidRepositoryStateError(
            "Cannot write authority settlement "
            f"{pending.settlement_operation_id!r}: pending locator {path} is unreadable"
        ) from exc
    if existing.settlement_operation_id != pending.settlement_operation_id:
        raise InvalidRepositoryStateError(
            "Cannot write authority settlement "
            f"{pending.settlement_operation_id!r}: pending locator {path} already claims "
            f"{existing.settlement_operation_id!r}"
        )


def _require_non_empty_str(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"authority settlement field {field_name!r} must be a non-empty string")


def _required_str(data: dict[str, object], field_name: str) -> str:
    value = data.get(field_name)
    _require_non_empty_str(value, field_name)
    return str(value)


def _optional_str(data: dict[str, object], field_name: str) -> str | None:
    value = data.get(field_name)
    if value is None:
        return None
    _require_non_empty_str(value, field_name)
    return str(value)


def _str_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError(f"authority settlement field {field_name!r} must be a list")
    result: list[str] = []
    for index, item in enumerate(value):
        _require_non_empty_str(item, f"{field_name}[{index}]")
        result.append(str(item))
    return tuple(result)


def _int(value: object, field_name: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise TypeError(f"authority settlement field {field_name!r} must be an integer")


def _require_int(value: object, field_name: str) -> None:
    _int(value, field_name)


def _authority_outcome(value: object) -> AuthorityOutcome:
    if value in {"allowed", "denied", "refused"}:
        return cast("AuthorityOutcome", value)
    raise ValueError(f"authority outcome is unsupported: {value!r}")


def _authority_settlement(value: object) -> AuthoritySettlement:
    if value in AUTHORITY_SETTLEMENTS:
        return cast("AuthoritySettlement", value)
    raise ValueError(f"authority settlement is unsupported: {value!r}")


def _authority_transaction_kind(value: object) -> AuthorityTransactionKind:
    if value in AUTHORITY_TRANSACTION_KINDS:
        return cast("AuthorityTransactionKind", value)
    raise ValueError(f"authority transaction kind is unsupported: {value!r}")


def _authority_commit_outcome(value: object) -> AuthorityCommitOutcome:
    if value in AUTHORITY_COMMIT_OUTCOMES:
        return cast("AuthorityCommitOutcome", value)
    raise ValueError(f"authority commit outcome is unsupported: {value!r}")


def _authority_settlement_phase(value: object) -> AuthoritySettlementPhase:
    if value in AUTHORITY_SETTLEMENT_PHASES:
        return cast("AuthoritySettlementPhase", value)
    raise ValueError(f"authority settlement phase is unsupported: {value!r}")
