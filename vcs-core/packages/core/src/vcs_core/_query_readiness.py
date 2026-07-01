"""First-cut Shepherd readiness service over private query inventory."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args

from vcs_core._authority_inventory import (
    AUTHORITY_SETTLEMENT_FILE_UNREADABLE,
    AUTHORITY_SETTLEMENT_IDENTITY_MISMATCH,
    AUTHORITY_SETTLEMENT_PAYLOAD_CORRUPT,
    AUTHORITY_SETTLEMENT_SCHEMA_MISMATCH,
    probe_authority_settlement_pending,
)
from vcs_core._operation_journal_inventory import probe_operation_journals
from vcs_core._query_inventory import (
    AUTHORITY_REF_TARGET_MISSING_WORLD,
    AUTHORITY_REF_UNREADABLE,
    OPEN_OPERATION_JOURNAL_INDEX_CORRUPT,
    OPERATION_JOURNAL_CHAIN_INVALID,
    OPERATION_JOURNAL_IDENTITY_MISMATCH,
    OPERATION_JOURNAL_PAYLOAD_CORRUPT,
    OPERATION_JOURNAL_REF_UNREADABLE,
    OPERATION_JOURNAL_SCHEMA_MISMATCH,
    OPERATION_JOURNAL_UNSUPPORTED_FAMILY,
    QUERY_DOMAIN_UNREADABLE,
    RECOVERY_DIRTY_PUSH,
    RECOVERY_DIRTY_PUSH_CORRUPT,
    RECOVERY_MATERIALIZATION_RUN,
    RECOVERY_MATERIALIZATION_RUN_CORRUPT,
    RECOVERY_ORPHANED_OPERATION_REF,
    RECOVERY_ORPHANED_SCOPE_REF,
    RECOVERY_SCOPE_REGISTRY_MISMATCH,
    RECOVERY_SIBLING_GROUP_BLOCKER,
    WORKSPACE_AUTHORITY_FILE_UNREADABLE,
    WORKSPACE_AUTHORITY_IDENTITY_MISMATCH,
    WORKSPACE_AUTHORITY_LEGACY_LOCATOR,
    WORKSPACE_AUTHORITY_LOCATOR_COLLISION,
    WORKSPACE_AUTHORITY_PAYLOAD_CORRUPT,
    WORKSPACE_AUTHORITY_SCHEMA_MISMATCH,
    WORLD_BINDING_INVALID,
    WORLD_SELECTED_HEAD_DANGLING,
    WORLD_UNREADABLE,
    InventoryIssue,
    InventoryItem,
    InventorySnapshot,
    RecoveryKind,
    issue_id,
    missing,
    present_invalid,
    present_valid,
)
from vcs_core._recovery_inventory import recovery_inventory_snapshot, recovery_inventory_snapshot_for_store
from vcs_core._scope_world_inventory import (
    RequiredBinding,
    probe_authority_ref,
    probe_scope,
    probe_selected_world,
    scope_ref_for_selector,
)
from vcs_core._workspace_authority_inventory import probe_workspace_authority_pending

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vcs_core.store import Store
    from vcs_core.vcscore import VcsCore

READINESS_RESULT_SCHEMA = "vcscore/shepherd-query-readiness/v1"
READINESS_REQUEST_SCHEMA = "vcscore/shepherd-query-readiness-request/v1"

ReadinessFreshness = Literal["best_effort", "locked", "revalidated"]
ReadinessState = Literal["safe_to_run", "observed_clear", "blocked", "needs_recovery"]
SystemHealthState = Literal["healthy", "needs_recovery"]
ReadinessCommand = Literal[
    "shepherd.status",
    "shepherd.run",
    "shepherd.recover",
    "vcscore.materialize",
    "vcscore.recover",
    "vcscore.lifecycle",
    "vcscore.runtime",
    "vcscore.push-status",
    "vcscore.reset-materialized",
    "vcscore.retained-output-selection",
]
MutationClass = ReadinessCommand

_COMMAND_ALIASES: dict[str, ReadinessCommand] = {
    "status": "shepherd.status",
    "shepherd.status": "shepherd.status",
    "shepherd.run": "shepherd.run",
    "run": "shepherd.run",
    "shepherd.recover": "shepherd.recover",
    "recover": "shepherd.recover",
    "vcscore.recover": "vcscore.recover",
    "vcs-core.recover": "vcscore.recover",
    "vcscore.lifecycle": "vcscore.lifecycle",
    "vcs-core.lifecycle": "vcscore.lifecycle",
    "vcscore.runtime": "vcscore.runtime",
    "vcs-core.runtime": "vcscore.runtime",
    "vcscore.push-status": "vcscore.push-status",
    "vcs-core.push-status": "vcscore.push-status",
    "vcscore.reset-materialized": "vcscore.reset-materialized",
    "vcs-core.reset-materialized": "vcscore.reset-materialized",
    "vcscore.retained-output-selection": "vcscore.retained-output-selection",
    "vcs-core.retained-output-selection": "vcscore.retained-output-selection",
    "retained-output-selection": "vcscore.retained-output-selection",
    "vcscore.materialize": "vcscore.materialize",
    "vcs-core.push": "vcscore.push-status",
    "push": "vcscore.push-status",
    "materialize": "vcscore.materialize",
}

_RUN_BASELINE_BINDINGS = (RequiredBinding(binding="workspace", head_kind="filesystem", role="shepherd.WorkspaceRef"),)
_ALL_DOMAINS = frozenset(
    {
        "scope",
        "authority_ref",
        "world",
        "workspace_authority",
        "authority_settlement",
        "recovery",
        "operation_journal",
    }
)
_CONTROL_PLANE_DOMAINS = frozenset(
    {"scope", "workspace_authority", "authority_settlement", "recovery", "operation_journal"}
)
_RUNTIME_DOMAINS = _CONTROL_PLANE_DOMAINS | frozenset({"operation"})
# The ground-world read triad (consumed/precondition for run + materialize). Named
# so the policy table references one source instead of re-inlining the literal.
_GROUND_WORLD_DOMAINS = frozenset({"scope", "authority_ref", "world"})
_ALL_RECOVERY_KINDS = frozenset(get_args(RecoveryKind))
_LIFECYCLE_RECOVERY_KINDS = frozenset(
    {
        "orphaned_operation_ref",
        "scope_registry_mismatch",
        "sibling_group_blocker",
    }
)
_PUSH_STATUS_RECOVERY_KINDS = _ALL_RECOVERY_KINDS
_RESET_RECOVERY_KINDS = frozenset({"sibling_group_blocker"})


@dataclass(frozen=True)
class ReadinessPolicy:
    command: ReadinessCommand
    mutates: bool = False
    baseline_required_bindings: tuple[RequiredBinding,...] = ()
    default_freshness: ReadinessFreshness | None = None
    default_allow_best_effort: bool | None = None
    observed_domains: frozenset[str] = frozenset()
    blocking_domains: frozenset[str] = frozenset()
    health_domains: frozenset[str] = frozenset()
    consumed_domains: frozenset[str] = frozenset()
    precondition_domains: frozenset[str] = frozenset()
    blocking_recovery_kinds: frozenset[str] = frozenset()
    shepherd_public: bool = False

    @property
    def request_freshness(self) -> ReadinessFreshness:
        if self.default_freshness is not None:
            return self.default_freshness
        return "locked" if self.mutates else "best_effort"

    @property
    def request_allow_best_effort(self) -> bool:
        if self.default_allow_best_effort is not None:
            return self.default_allow_best_effort
        return not self.mutates


_READINESS_POLICIES: dict[ReadinessCommand, ReadinessPolicy] = {
    "shepherd.status": ReadinessPolicy(
        command="shepherd.status",
        observed_domains=_ALL_DOMAINS,
        health_domains=_ALL_DOMAINS,
        blocking_recovery_kinds=_ALL_RECOVERY_KINDS,
        shepherd_public=True,
    ),
    "shepherd.run": ReadinessPolicy(
        command="shepherd.run",
        mutates=True,
        baseline_required_bindings=_RUN_BASELINE_BINDINGS,
        observed_domains=_ALL_DOMAINS,
        blocking_domains=_ALL_DOMAINS,
        health_domains=_ALL_DOMAINS,
        consumed_domains=_GROUND_WORLD_DOMAINS,
        precondition_domains=_GROUND_WORLD_DOMAINS,
        blocking_recovery_kinds=_ALL_RECOVERY_KINDS,
        shepherd_public=True,
    ),
    "vcscore.materialize": ReadinessPolicy(
        command="vcscore.materialize",
        mutates=True,
        baseline_required_bindings=_RUN_BASELINE_BINDINGS,
        observed_domains=_ALL_DOMAINS,
        blocking_domains=_ALL_DOMAINS,
        health_domains=_ALL_DOMAINS,
        consumed_domains=_GROUND_WORLD_DOMAINS,
        precondition_domains=_GROUND_WORLD_DOMAINS,
        blocking_recovery_kinds=_ALL_RECOVERY_KINDS,
        shepherd_public=True,
    ),
    "shepherd.recover": ReadinessPolicy(
        command="shepherd.recover",
        mutates=True,
        observed_domains=_CONTROL_PLANE_DOMAINS,
        blocking_domains=_CONTROL_PLANE_DOMAINS,
        health_domains=_CONTROL_PLANE_DOMAINS,
        consumed_domains=_CONTROL_PLANE_DOMAINS,
        precondition_domains=_CONTROL_PLANE_DOMAINS,
        blocking_recovery_kinds=_ALL_RECOVERY_KINDS,
        shepherd_public=True,
    ),
    "vcscore.recover": ReadinessPolicy(
        command="vcscore.recover",
        mutates=True,
        observed_domains=_CONTROL_PLANE_DOMAINS,
        blocking_domains=_CONTROL_PLANE_DOMAINS,
        health_domains=_CONTROL_PLANE_DOMAINS,
        consumed_domains=_CONTROL_PLANE_DOMAINS,
        precondition_domains=_CONTROL_PLANE_DOMAINS,
        blocking_recovery_kinds=_ALL_RECOVERY_KINDS,
    ),
    "vcscore.lifecycle": ReadinessPolicy(
        command="vcscore.lifecycle",
        mutates=True,
        observed_domains=_CONTROL_PLANE_DOMAINS,
        blocking_domains=_CONTROL_PLANE_DOMAINS,
        health_domains=_CONTROL_PLANE_DOMAINS,
        consumed_domains=frozenset({"scope"}),
        precondition_domains=frozenset({"scope"}),
        blocking_recovery_kinds=_LIFECYCLE_RECOVERY_KINDS,
    ),
    "vcscore.runtime": ReadinessPolicy(
        command="vcscore.runtime",
        mutates=True,
        observed_domains=_RUNTIME_DOMAINS,
        blocking_domains=_RUNTIME_DOMAINS,
        health_domains=_RUNTIME_DOMAINS,
        consumed_domains=frozenset({"scope", "operation"}),
        precondition_domains=frozenset({"scope", "operation"}),
        blocking_recovery_kinds=_LIFECYCLE_RECOVERY_KINDS,
    ),
    "vcscore.push-status": ReadinessPolicy(
        command="vcscore.push-status",
        mutates=True,
        observed_domains=_CONTROL_PLANE_DOMAINS,
        blocking_domains=_CONTROL_PLANE_DOMAINS,
        health_domains=_CONTROL_PLANE_DOMAINS,
        consumed_domains=frozenset({"scope"}),
        precondition_domains=frozenset({"scope"}),
        blocking_recovery_kinds=_PUSH_STATUS_RECOVERY_KINDS,
    ),
    "vcscore.reset-materialized": ReadinessPolicy(
        command="vcscore.reset-materialized",
        mutates=True,
        observed_domains=_CONTROL_PLANE_DOMAINS,
        blocking_domains=_CONTROL_PLANE_DOMAINS,
        health_domains=_CONTROL_PLANE_DOMAINS,
        consumed_domains=frozenset({"scope"}),
        precondition_domains=frozenset({"scope"}),
        blocking_recovery_kinds=_RESET_RECOVERY_KINDS,
    ),
    "vcscore.retained-output-selection": ReadinessPolicy(
        command="vcscore.retained-output-selection",
        mutates=True,
        observed_domains=_ALL_DOMAINS,
        blocking_domains=_ALL_DOMAINS,
        health_domains=_ALL_DOMAINS,
        consumed_domains=_ALL_DOMAINS,
        precondition_domains=_ALL_DOMAINS,
        blocking_recovery_kinds=_LIFECYCLE_RECOVERY_KINDS,
    ),
}

MUTATION_PRECONDITION_SCHEMA = "vcscore/mutation-precondition/v1"

_RECOVERY_COMMANDS: frozenset[ReadinessCommand] = frozenset({"shepherd.recover", "vcscore.recover"})

_RECOVERABLE_ISSUES = frozenset(
    {
        AUTHORITY_REF_TARGET_MISSING_WORLD,
        AUTHORITY_REF_UNREADABLE,
        OPEN_OPERATION_JOURNAL_INDEX_CORRUPT,
        OPERATION_JOURNAL_CHAIN_INVALID,
        OPERATION_JOURNAL_IDENTITY_MISMATCH,
        OPERATION_JOURNAL_PAYLOAD_CORRUPT,
        OPERATION_JOURNAL_REF_UNREADABLE,
        OPERATION_JOURNAL_SCHEMA_MISMATCH,
        OPERATION_JOURNAL_UNSUPPORTED_FAMILY,
        QUERY_DOMAIN_UNREADABLE,
        RECOVERY_DIRTY_PUSH,
        RECOVERY_DIRTY_PUSH_CORRUPT,
        RECOVERY_MATERIALIZATION_RUN,
        RECOVERY_MATERIALIZATION_RUN_CORRUPT,
        RECOVERY_ORPHANED_OPERATION_REF,
        RECOVERY_ORPHANED_SCOPE_REF,
        RECOVERY_SCOPE_REGISTRY_MISMATCH,
        RECOVERY_SIBLING_GROUP_BLOCKER,
        AUTHORITY_SETTLEMENT_FILE_UNREADABLE,
        AUTHORITY_SETTLEMENT_IDENTITY_MISMATCH,
        AUTHORITY_SETTLEMENT_PAYLOAD_CORRUPT,
        AUTHORITY_SETTLEMENT_SCHEMA_MISMATCH,
        WORKSPACE_AUTHORITY_FILE_UNREADABLE,
        WORKSPACE_AUTHORITY_IDENTITY_MISMATCH,
        WORKSPACE_AUTHORITY_LEGACY_LOCATOR,
        WORKSPACE_AUTHORITY_LOCATOR_COLLISION,
        WORKSPACE_AUTHORITY_PAYLOAD_CORRUPT,
        WORKSPACE_AUTHORITY_SCHEMA_MISMATCH,
        WORLD_BINDING_INVALID,
        WORLD_SELECTED_HEAD_DANGLING,
        WORLD_UNREADABLE,
        "readiness_operation_journal_open",
        "readiness_authority_settlement_pending",
        "readiness_workspace_authority_pending",
    }
)


@dataclass(frozen=True)
class ReadinessTarget:
    """Private target selector for recovery-oriented readiness."""

    domain: str
    kind: str | None = None
    item_id: str | None = None
    locator: str | None = None
    operation_id: str | None = None
    family: str | None = None

    @classmethod
    def from_json(cls, payload: object) -> ReadinessTarget:
        if not isinstance(payload, dict):
            raise TypeError("readiness target entries must be objects")
        return cls(
            domain=_required_str(payload, "domain"),
            kind=_optional_str(payload, "kind"),
            item_id=_optional_str(payload, "item_id"),
            locator=_optional_str(payload, "locator"),
            operation_id=_optional_str(payload, "operation_id"),
            family=_optional_str(payload, "family"),
        )

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {"domain": self.domain}
        if self.kind is not None:
            payload["kind"] = self.kind
        if self.item_id is not None:
            payload["item_id"] = self.item_id
        if self.locator is not None:
            payload["locator"] = self.locator
        if self.operation_id is not None:
            payload["operation_id"] = self.operation_id
        if self.family is not None:
            payload["family"] = self.family
        return payload


@dataclass(frozen=True)
class ReadinessOperationAuthority:
    """Private live-operation authority admitted by a runtime readiness request."""

    operation_id: str
    operation_ref: str | None = None
    kind: str | None = None
    scope_ref: str | None = None
    scope_instance_id: str | None = None
    session_id: str | None = None
    role: str = "runtime"

    @classmethod
    def from_json(cls, payload: object) -> ReadinessOperationAuthority:
        if not isinstance(payload, dict):
            raise TypeError("authorized operation entries must be objects")
        return cls(
            operation_id=_required_str(payload, "operation_id"),
            operation_ref=_optional_str(payload, "operation_ref"),
            kind=_optional_str(payload, "kind"),
            scope_ref=_optional_str(payload, "scope_ref"),
            scope_instance_id=_optional_str(payload, "scope_instance_id"),
            session_id=_optional_str(payload, "session_id"),
            role=_required_str(payload, "role", default="runtime"),
        )

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "operation_id": self.operation_id,
            "role": self.role,
        }
        if self.operation_ref is not None:
            payload["operation_ref"] = self.operation_ref
        if self.kind is not None:
            payload["kind"] = self.kind
        if self.scope_ref is not None:
            payload["scope_ref"] = self.scope_ref
        if self.scope_instance_id is not None:
            payload["scope_instance_id"] = self.scope_instance_id
        if self.session_id is not None:
            payload["session_id"] = self.session_id
        return payload


ReadinessAuthoritySource = Literal["request_field", "implicit_runtime_stack", "runtime_admission_sidecar"]
_OWNER_DERIVED_AUTHORITY_SOURCES: frozenset[ReadinessAuthoritySource] = frozenset(
    ("implicit_runtime_stack", "runtime_admission_sidecar")
)


@dataclass(frozen=True)
class RuntimeAdmissionContext:
    """Owner-computed runtime-only context for readiness admission."""

    record_class: str | None = None
    nested_authorizations: tuple[Any,...] = ()
    authorized_operations: tuple[ReadinessOperationAuthority,...] = ()
    allowed_blocker_item_ids: tuple[str,...] = ()
    authority_source: ReadinessAuthoritySource = "runtime_admission_sidecar"


@dataclass(frozen=True)
class _SourcedOperationAuthority:
    authority: ReadinessOperationAuthority
    sources: frozenset[ReadinessAuthoritySource]

    @property
    def owner_derived(self) -> bool:
        return bool(self.sources & _OWNER_DERIVED_AUTHORITY_SOURCES)


@dataclass(frozen=True)
class ReadinessRequest:
    """Private request DTO for first-cut Shepherd readiness."""

    command: ReadinessCommand = "shepherd.status"
    scope_selector: str = "ground"
    required_bindings: tuple[RequiredBinding,...] = ()
    requested_freshness: ReadinessFreshness | None = None
    allow_best_effort: bool | None = None
    targets: tuple[ReadinessTarget,...] = ()
    authorized_operations: tuple[ReadinessOperationAuthority,...] = ()

    def __post_init__(self) -> None:
        normalized_command = normalize_mutation_class(self.command)
        policy = _policy_for(normalized_command)
        _validate_targets_for_command(normalized_command, self.targets)
        _validate_authorized_operations_for_command(normalized_command, self.authorized_operations)
        object.__setattr__(self, "command", normalized_command)
        object.__setattr__(
            self,
            "required_bindings",
            _merge_required_bindings(policy.baseline_required_bindings, self.required_bindings),
        )
        object.__setattr__(
            self,
            "requested_freshness",
            policy.request_freshness
            if self.requested_freshness is None
            else _normalize_freshness(self.requested_freshness),
        )
        object.__setattr__(
            self,
            "allow_best_effort",
            policy.request_allow_best_effort if self.allow_best_effort is None else self.allow_best_effort,
        )

    @classmethod
    def create(
        cls,
        *,
        command: str = "shepherd.status",
        scope: str | None = None,
        required_bindings: tuple[RequiredBinding,...] | None = None,
        requested_freshness: str | None = None,
        allow_best_effort: bool | None = None,
        targets: tuple[ReadinessTarget,...] = (),
        authorized_operations: tuple[ReadinessOperationAuthority,...] = (),
    ) -> ReadinessRequest:
        normalized_command = normalize_mutation_class(command)
        normalized_freshness = None if requested_freshness is None else _normalize_freshness(requested_freshness)
        return cls(
            command=normalized_command,
            scope_selector=scope or "ground",
            required_bindings=required_bindings or (),
            requested_freshness=normalized_freshness,
            allow_best_effort=allow_best_effort,
            targets=targets,
            authorized_operations=authorized_operations,
        )

    @classmethod
    def from_json(cls, payload: dict[str, object]) -> ReadinessRequest:
        raw_scope = payload.get("scope", "ground")
        if isinstance(raw_scope, dict):
            raw_scope = raw_scope.get("ref") or raw_scope.get("selector") or "ground"
        if not isinstance(raw_scope, str):
            raise TypeError("readiness scope must be a string or object with selector/ref")
        raw_bindings = payload.get("required_bindings")
        bindings = _bindings_from_json(raw_bindings) if raw_bindings is not None else None
        raw_targets = payload.get("targets")
        targets = _targets_from_json(raw_targets) if raw_targets is not None else ()
        raw_authorized_operations = payload.get("authorized_operations")
        authorized_operations = (
            _authorized_operations_from_json(raw_authorized_operations) if raw_authorized_operations is not None else ()
        )
        return cls.create(
            command=_required_str(payload, "command", default="shepherd.status"),
            scope=raw_scope,
            required_bindings=bindings,
            requested_freshness=_optional_str(payload, "requested_freshness"),
            allow_best_effort=_optional_bool(payload, "allow_best_effort", default=None),
            targets=targets,
            authorized_operations=authorized_operations,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "schema": READINESS_REQUEST_SCHEMA,
            "command": self.command,
            "scope": {"selector": self.scope_selector},
            "required_bindings": [
                {
                    "binding": binding.binding,
                    "head_kind": binding.head_kind,
                    "role": binding.role,
                    "check": binding.check,
                }
                for binding in self.required_bindings
            ],
            "requested_freshness": self.requested_freshness,
            "allow_best_effort": self.allow_best_effort,
            "targets": [target.to_json() for target in self.targets],
            "authorized_operations": [authority.to_json() for authority in self.authorized_operations],
        }

    @property
    def mutates(self) -> bool:
        return _policy_for(self.command).mutates


@dataclass(frozen=True)
class ReadinessBlocker:
    id: str
    kind: str
    command: ReadinessCommand
    item_id: str
    issue_id: str | None
    severity: str = "blocker"
    recovery_hint: str | None = None
    blocks: bool = True

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "kind": self.kind,
            "command": self.command,
            "blocks": self.blocks,
            "item_id": self.item_id,
            "severity": self.severity,
        }
        if self.issue_id is not None:
            payload["issue_id"] = self.issue_id
        if self.recovery_hint is not None:
            payload["recovery_hint"] = self.recovery_hint
        return payload


@dataclass(frozen=True)
class MutationPrecondition:
    """Opaque vcs-core precondition for a later locked revalidation step."""

    schema: str
    command: ReadinessCommand
    scope_ref: str
    snapshot_id: str
    mode: ReadinessFreshness
    item_ids: tuple[str,...]
    source_identities: dict[str, dict[str, object]]
    checked_at_unix_ns: int

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> MutationPrecondition:
        if payload.get("schema") != MUTATION_PRECONDITION_SCHEMA:
            raise ValueError("mutation precondition has an unsupported schema")
        command = normalize_mutation_class(_required_str(dict(payload), "command"))
        scope_ref = _required_str(dict(payload), "scope_ref")
        snapshot_id = _required_str(dict(payload), "snapshot_id")
        mode = _normalize_freshness(_required_str(dict(payload), "mode"))
        raw_item_ids = payload.get("item_ids")
        if not isinstance(raw_item_ids, list) or not all(isinstance(item_id, str) for item_id in raw_item_ids):
            raise TypeError("mutation precondition item_ids must be an array of strings")
        raw_identities = payload.get("source_identities")
        if not isinstance(raw_identities, dict):
            raise TypeError("mutation precondition source_identities must be an object")
        identities: dict[str, dict[str, object]] = {}
        for key, value in raw_identities.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise TypeError("mutation precondition source_identities must map strings to objects")
            identities[key] = dict(value)
        checked_at = payload.get("checked_at_unix_ns")
        if not isinstance(checked_at, int):
            raise TypeError("mutation precondition checked_at_unix_ns must be an integer")
        return cls(
            schema=MUTATION_PRECONDITION_SCHEMA,
            command=command,
            scope_ref=scope_ref,
            snapshot_id=snapshot_id,
            mode=mode,
            item_ids=tuple(raw_item_ids),
            source_identities=identities,
            checked_at_unix_ns=checked_at,
        )

    @classmethod
    def from_snapshot(
        cls,
        *,
        mode: ReadinessFreshness,
        request: ReadinessRequest,
        scope_ref: str,
        snapshot: InventorySnapshot,
        item_ids: tuple[str,...],
    ) -> MutationPrecondition:
        items_by_id = {item.id: item for item in snapshot.items}
        identities = {item_id: dict(items_by_id[item_id].source_identity) for item_id in item_ids}
        return cls(
            schema=MUTATION_PRECONDITION_SCHEMA,
            command=request.command,
            scope_ref=scope_ref,
            snapshot_id=snapshot.id,
            mode=mode,
            item_ids=item_ids,
            source_identities=identities,
            checked_at_unix_ns=time.time_ns(),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "command": self.command,
            "scope_ref": self.scope_ref,
            "snapshot_id": self.snapshot_id,
            "mode": self.mode,
            "item_ids": list(self.item_ids),
            "source_identities": {key: dict(value) for key, value in self.source_identities.items()},
            "checked_at_unix_ns": self.checked_at_unix_ns,
        }


@dataclass(frozen=True)
class ReadinessResult:
    repository_path: str
    request: ReadinessRequest
    scope_name: str
    scope_ref: str
    snapshot: InventorySnapshot
    blockers: tuple[ReadinessBlocker,...]
    state: ReadinessState
    allowed: bool
    admission_authoritative: bool
    freshness: ReadinessFreshness
    mutation_precondition: MutationPrecondition | None = None
    recovery_available: bool = False
    recovery_required: bool = False
    system_health_state: SystemHealthState = "healthy"
    # Open shell-capture lease operation ids excluded as the live daemon's own
    # active context (M3). Surfaced for auditability; empty outside a daemon.
    excluded_daemon_lease_ids: tuple[str,...] = ()

    def to_json(self) -> dict[str, object]:
        return {
            "schema": READINESS_RESULT_SCHEMA,
            "repository": {"path": self.repository_path},
            "scope": {"name": self.scope_name, "ref": self.scope_ref},
            "readiness": {
                "command": self.request.command,
                # `allowed` is only meaningful when this query path is the admission
                # authority. For non-authoritative classes (mutations admitted at the
                # chokepoint) emit null, not false, so a consumer cannot misread a
                # non-answer as "blocked". The internal `allowed` bool is unchanged.
                "allowed": self.allowed if self.admission_authoritative else None,
                "state": self.state,
                "admission_authoritative": self.admission_authoritative,
                "freshness": self.freshness,
                "recovery": {
                    "available": self.recovery_available,
                    "required": self.recovery_required,
                },
            },
            "system_health": {
                "state": self.system_health_state,
                "recovery": {
                    "available": self.recovery_available,
                    "required": self.recovery_required,
                },
            },
            "snapshot": {
                "id": self.snapshot.id,
                "created_at_unix_ns": self.snapshot.created_at_unix_ns,
                "consistency": self.snapshot.consistency,
                "source_identity": dict(self.snapshot.source_identity),
            },
            "items": [item.to_json() for item in self.snapshot.items],
            "edges": [edge.to_json() for edge in self.snapshot.edges],
            "issues": [issue.to_json() for issue in self.snapshot.issues],
            "blockers": [blocker.to_json() for blocker in self.blockers],
            "mutation_precondition": (
                None if self.mutation_precondition is None else self.mutation_precondition.to_json()
            ),
        }


def normalize_mutation_class(command: str) -> ReadinessCommand:
    try:
        return _COMMAND_ALIASES[command]
    except KeyError as exc:
        raise ValueError(f"unknown readiness command: {command!r}") from exc


def known_readiness_commands() -> tuple[str,...]:
    """Accepted ``--command`` values (aliases + canonical), sorted.

    Exposed for the CLI to enumerate in help and in the structured error it
    renders for an unknown command (issue 04).
    """
    return tuple(sorted(_COMMAND_ALIASES))


def readiness_command_metadata(command: str) -> dict[str, object]:
    """Return private command-policy metadata for tests and internal adapters."""
    normalized = normalize_mutation_class(command)
    policy = _policy_for(normalized)
    return {
        "command": normalized,
        "mutates": policy.mutates,
        "shepherd_public": policy.shepherd_public,
        "default_freshness": policy.request_freshness,
        "default_allow_best_effort": policy.request_allow_best_effort,
        "observed_domains": sorted(policy.observed_domains),
        "blocking_domains": sorted(policy.blocking_domains),
        "health_domains": sorted(policy.health_domains),
        "consumed_domains": sorted(policy.consumed_domains),
        "precondition_domains": sorted(policy.precondition_domains),
        "blocking_recovery_kinds": sorted(policy.blocking_recovery_kinds),
    }


def _baseline_required_bindings(command: ReadinessCommand) -> tuple[RequiredBinding,...]:
    return _policy_for(command).baseline_required_bindings


def _merge_required_bindings(
    baseline: tuple[RequiredBinding,...],
    extras: tuple[RequiredBinding,...],
) -> tuple[RequiredBinding,...]:
    merged: list[RequiredBinding] = []
    seen: set[RequiredBinding] = set()
    for binding in (*baseline, *extras):
        if binding in seen:
            continue
        merged.append(binding)
        seen.add(binding)
    return tuple(merged)


def evaluate_readiness(
    repo_path: str | Path,
    request: ReadinessRequest | None = None,
    *,
    owner: VcsCore | None = None,
    force_freshness: ReadinessFreshness | None = None,
    runtime_admission_context: RuntimeAdmissionContext | None = None,
) -> ReadinessResult:
    """Evaluate first-cut readiness through one private policy path."""
    current_request = request or ReadinessRequest.create()
    policy = _policy_for(current_request.command)
    freshness = force_freshness or current_request.requested_freshness or policy.request_freshness
    scope_ref = scope_ref_for_selector(current_request.scope_selector)
    scope_name = "ground" if scope_ref == "refs/vcscore/ground" else scope_ref.rsplit("/", 1)[-1]
    operation_authorities = _operation_authorities_for_request(
        current_request,
        owner,
        runtime_admission_context=runtime_admission_context,
    )
    nested_authorizations = _nested_authorizations_for_request(owner, scope_ref, runtime_admission_context)
    items = list(
        _inventory_items_for_policy(
            repo_path,
            current_request,
            policy,
            scope_ref,
            owner=owner,
            operation_authorities=operation_authorities,
            nested_authorizations=nested_authorizations,
            runtime_admission_context=runtime_admission_context,
        )
    )
    blockers, policy_issues = _derive_blockers(current_request, policy, tuple(items))
    consumed_item_ids = _consumed_item_ids(policy, tuple(items))
    unverifiable_issues = _unverifiable_consumed_fact_issues(tuple(items), consumed_item_ids)
    if unverifiable_issues:
        policy_issues = (*policy_issues, *unverifiable_issues)
        blockers = (
            *blockers,
            *(
                _blocker(current_request, _item_by_id(tuple(items), issue.subject_id), issue)
                for issue in unverifiable_issues
            ),
        )
    issues = (*policy_issues, *(issue for item in items for issue in item.issues))
    snapshot = InventorySnapshot.create(
        items=tuple(items),
        issues=issues,
        consistency="best_effort" if freshness == "best_effort" else "locked",
        source_identity={"repo_path": str(Path(repo_path)), "scope_ref": scope_ref},
    )
    if blockers:
        state: ReadinessState = "blocked"
        allowed = False
        authoritative = True
        precondition = None
    elif current_request.mutates and freshness == "best_effort":
        state = "observed_clear"
        allowed = False
        authoritative = False
        precondition = None
    else:
        state = "safe_to_run"
        allowed = True
        authoritative = True
        precondition = (
            MutationPrecondition.from_snapshot(
                mode=freshness,
                request=current_request,
                scope_ref=scope_ref,
                snapshot=snapshot,
                item_ids=consumed_item_ids,
            )
            if current_request.mutates
            else None
        )
    recovery_required = _system_recovery_required(tuple(items), policy_issues, policy)
    return ReadinessResult(
        repository_path=str(Path(repo_path)),
        request=current_request,
        scope_name=scope_name,
        scope_ref=scope_ref,
        snapshot=snapshot,
        blockers=blockers,
        state=state,
        allowed=allowed,
        admission_authoritative=authoritative,
        freshness=freshness,
        mutation_precondition=precondition,
        recovery_available=recovery_required,
        recovery_required=recovery_required,
        system_health_state="needs_recovery" if recovery_required else "healthy",
    )


def revalidate_mutation_precondition(
    repo_path: str | Path,
    request: ReadinessRequest,
    precondition: MutationPrecondition | Mapping[str, object],
    *,
    owner: VcsCore,
    runtime_admission_context: RuntimeAdmissionContext | None = None,
) -> ReadinessResult:
    """Re-run readiness under the coordinator lock and compare opaque durable identities."""
    current_precondition = (
        precondition if isinstance(precondition, MutationPrecondition) else MutationPrecondition.from_json(precondition)
    )
    if not request.mutates:
        raise ValueError("mutation precondition revalidation requires a mutating readiness request")
    if current_precondition.mode == "best_effort":
        raise ValueError("best-effort mutation preconditions cannot authorize mutation")
    scope_ref = scope_ref_for_selector(request.scope_selector)
    if current_precondition.command != request.command:
        raise ValueError("mutation precondition command does not match the readiness request")
    if current_precondition.scope_ref != scope_ref:
        raise ValueError("mutation precondition scope does not match the readiness request")

    fresh = evaluate_readiness(
        repo_path,
        request,
        owner=owner,
        force_freshness="revalidated",
        runtime_admission_context=runtime_admission_context,
    )
    if not fresh.allowed or fresh.mutation_precondition is None:
        return _readiness_with_stale_precondition(
            fresh,
            current_precondition,
            reason="mutation precondition no longer matches an allowed readiness state",
        )
    fresh_precondition = fresh.mutation_precondition
    if fresh_precondition.item_ids != current_precondition.item_ids:
        return _readiness_with_stale_precondition(
            fresh,
            current_precondition,
            reason="mutation precondition item identities changed",
        )
    for item_id in current_precondition.item_ids:
        if fresh_precondition.source_identities.get(item_id) != current_precondition.source_identities.get(item_id):
            return _readiness_with_stale_precondition(
                fresh,
                current_precondition,
                reason=f"mutation precondition source identity changed for {item_id}",
                changed_item_id=item_id,
            )
    return fresh


def _readiness_with_stale_precondition(
    fresh: ReadinessResult,
    precondition: MutationPrecondition,
    *,
    reason: str,
    changed_item_id: str | None = None,
) -> ReadinessResult:
    subject = _precondition_subject_item(fresh.snapshot.items, precondition, changed_item_id)
    issue = InventoryIssue(
        id=issue_id(subject.id, "readiness_mutation_precondition_stale"),
        code="readiness_mutation_precondition_stale",
        message=reason,
        subject_id=subject.id,
        locator=subject.locator,
        recovery_hint="Re-run readiness and retry with the new mutation precondition.",
        evidence={
            "precondition_snapshot_id": precondition.snapshot_id,
            "precondition_item_ids": list(precondition.item_ids),
            "changed_item_id": changed_item_id,
        },
    )
    snapshot = replace(fresh.snapshot, issues=(*fresh.snapshot.issues, issue))
    return replace(
        fresh,
        snapshot=snapshot,
        blockers=(*fresh.blockers, _blocker(fresh.request, subject, issue)),
        state="blocked",
        allowed=False,
        admission_authoritative=True,
        freshness="revalidated",
        mutation_precondition=None,
    )


def _precondition_subject_item(
    items: tuple[InventoryItem,...],
    precondition: MutationPrecondition,
    changed_item_id: str | None,
) -> InventoryItem:
    candidate_ids = (changed_item_id, *precondition.item_ids) if changed_item_id is not None else precondition.item_ids
    for item_id in candidate_ids:
        for item in items:
            if item.id == item_id:
                return item
    return _recovery_target_subject_item(items)


def _open_store(repo_path: str | Path) -> Store:
    from vcs_core.store import Store

    return Store.open_existing(str(repo_path))


def _operation_authorities_for_request(
    request: ReadinessRequest,
    owner: VcsCore | None,
    *,
    runtime_admission_context: RuntimeAdmissionContext | None = None,
) -> tuple[_SourcedOperationAuthority,...]:
    if request.command != "vcscore.runtime":
        return ()
    merged: dict[str, _SourcedOperationAuthority] = {}

    def add(authority: ReadinessOperationAuthority, source: ReadinessAuthoritySource) -> None:
        key = authority.operation_id
        existing = merged.get(key)
        if existing is None:
            merged[key] = _SourcedOperationAuthority(authority=authority, sources=frozenset((source,)))
            return
        selected_authority = _merge_operation_authority(existing, authority, source)
        merged[key] = _SourcedOperationAuthority(
            authority=selected_authority,
            sources=frozenset((*existing.sources, source)),
        )

    for authority in request.authorized_operations:
        add(authority, "request_field")
    for authority in _implicit_runtime_operation_authorities(owner):
        add(authority, "implicit_runtime_stack")
    if runtime_admission_context is not None:
        for authority in runtime_admission_context.authorized_operations:
            add(authority, runtime_admission_context.authority_source)
    return tuple(merged.values())


def _merge_operation_authority(
    existing: _SourcedOperationAuthority,
    authority: ReadinessOperationAuthority,
    source: ReadinessAuthoritySource,
) -> ReadinessOperationAuthority:
    existing_authority = existing.authority
    existing_has_request = "request_field" in existing.sources
    incoming_is_request = source == "request_field"

    def merge_optional(existing_value: str | None, incoming_value: str | None) -> str | None:
        if existing_has_request:
            return existing_value if existing_value is not None else incoming_value
        if incoming_is_request:
            return incoming_value if incoming_value is not None else existing_value
        return incoming_value if incoming_value is not None else existing_value

    role = existing_authority.role if existing_has_request else authority.role
    return replace(
        existing_authority,
        operation_ref=merge_optional(existing_authority.operation_ref, authority.operation_ref),
        kind=merge_optional(existing_authority.kind, authority.kind),
        scope_ref=merge_optional(existing_authority.scope_ref, authority.scope_ref),
        scope_instance_id=merge_optional(existing_authority.scope_instance_id, authority.scope_instance_id),
        session_id=merge_optional(existing_authority.session_id, authority.session_id),
        role=role,
    )


def _nested_authorizations_for_request(
    owner: VcsCore | None,
    scope_ref: str,
    runtime_admission_context: RuntimeAdmissionContext | None,
) -> tuple[Any,...]:
    authorizations: list[Any] = []
    if runtime_admission_context is not None:
        authorizations.extend(runtime_admission_context.nested_authorizations)
    if owner is not None:
        from vcs_core._vcscore_runtime import _nested_admission_authorizations

        authorizations.extend(_nested_admission_authorizations(owner, scope_ref))
    return tuple(authorizations)


def _implicit_runtime_operation_authorities(owner: VcsCore | None) -> tuple[ReadinessOperationAuthority,...]:
    if owner is None:
        return ()
    operation_stack = getattr(owner._pipeline.context, "operation_stack", ())
    operations = tuple(operation_stack)
    if not operations:
        current_operation = owner._pipeline.current_operation()
        operations = () if current_operation is None else (current_operation,)
    return tuple(
        ReadinessOperationAuthority(
            operation_id=operation.durable_id,
            operation_ref=operation.ref,
            kind=operation.kind,
            scope_ref=operation.scope_ref,
            scope_instance_id=operation.scope_instance_id,
            session_id=operation.session_id,
            role="runtime",
        )
        for operation in operations
    )


def _operation_authority_items(
    store: Store,
    authorities: tuple[_SourcedOperationAuthority,...],
    *,
    scope_ref: str,
    nested_authorizations: tuple[Any,...] = (),
    runtime_admission_context: RuntimeAdmissionContext | None = None,
) -> tuple[tuple[InventoryItem,...], set[str]]:
    if not authorities:
        return (), set()
    open_operations = tuple(store.list_open_operations())
    items: list[InventoryItem] = []
    authorized_refs: set[str] = set()
    for sourced_authority in authorities:
        authority = sourced_authority.authority
        match = _matching_open_operation(open_operations, authority)
        if match is not None:
            if getattr(match, "scope_ref", None) != scope_ref:
                if sourced_authority.owner_derived and _nested_authority_admits(
                    nested_authorizations,
                    parent_scope_ref=str(getattr(match, "scope_ref", "")),
                    child_scope_ref=scope_ref,
                ):
                    items.append(_authorized_open_operation_item(store, authority, match))
                    authorized_refs.add(match.ref)
                    continue
                parent_item = _nested_child_quiescence_item(
                    store,
                    authority,
                    match,
                    scope_ref=scope_ref,
                    sourced_authority=sourced_authority,
                    runtime_admission_context=runtime_admission_context,
                )
                if parent_item is not None:
                    items.append(parent_item)
                    continue
                items.append(_scope_mismatched_operation_authority_item(store, authority, match, scope_ref=scope_ref))
                continue
            items.append(_authorized_open_operation_item(store, authority, match))
            authorized_refs.add(match.ref)
            continue
        candidate = _candidate_open_operation(open_operations, authority)
        items.append(_missing_or_mismatched_operation_authority_item(store, authority, candidate))
    return tuple(items), authorized_refs


def _nested_authority_admits(
    nested_authorizations: tuple[Any,...],
    *,
    parent_scope_ref: str,
    child_scope_ref: str,
) -> bool:
    for authorization in nested_authorizations:
        admits = getattr(authorization, "admits", None)
        if callable(admits) and admits(parent_scope_ref=parent_scope_ref, child_scope_ref=child_scope_ref):
            return True
    return False


def _matching_open_operation(
    open_operations: tuple[Any,...],
    authority: ReadinessOperationAuthority,
) -> Any | None:
    for operation in open_operations:
        if getattr(operation, "durable_id", None) != authority.operation_id:
            continue
        if authority.operation_ref is not None and getattr(operation, "ref", None) != authority.operation_ref:
            continue
        if authority.kind is not None and getattr(operation, "kind", None) != authority.kind:
            continue
        if authority.scope_ref is not None and getattr(operation, "scope_ref", None) != authority.scope_ref:
            continue
        if (
            authority.scope_instance_id is not None
            and getattr(operation, "scope_instance_id", None) != authority.scope_instance_id
        ):
            continue
        if authority.session_id is not None and getattr(operation, "session_id", None) != authority.session_id:
            continue
        return operation
    return None


def _candidate_open_operation(
    open_operations: tuple[Any,...],
    authority: ReadinessOperationAuthority,
) -> Any | None:
    for operation in open_operations:
        if getattr(operation, "durable_id", None) == authority.operation_id:
            return operation
        if authority.operation_ref is not None and getattr(operation, "ref", None) == authority.operation_ref:
            return operation
    return None


def _authorized_open_operation_item(
    store: Store,
    authority: ReadinessOperationAuthority,
    operation: Any,
) -> InventoryItem:
    operation_ref = str(operation.ref)
    operation_id = str(operation.durable_id)
    return InventoryItem(
        id=_operation_authority_item_id(authority),
        domain="operation",
        kind="authorized_open_operation",
        locator=operation_ref,
        source_kind="git_ref",
        source_store="coordinator",
        health=present_valid(lifecycle="active", authority_role="authoritative"),
        role=("operation", "live_authority"),
        fields={
            "operation_id": operation_id,
            "operation_ref": operation_ref,
            "operation_kind": str(operation.kind),
            "scope_ref": str(operation.scope_ref),
            "scope_instance_id": str(operation.scope_instance_id),
            "session_id": getattr(operation, "session_id", None),
            "role": authority.role,
        },
        source_identity=_operation_source_identity(store, operation),
    )


def _missing_or_mismatched_operation_authority_item(
    store: Store,
    authority: ReadinessOperationAuthority,
    candidate: Any | None,
) -> InventoryItem:
    item_id = _operation_authority_item_id(authority)
    if candidate is None:
        issue = InventoryIssue(
            id=issue_id(item_id, "readiness_operation_authority_missing"),
            code="readiness_operation_authority_missing",
            message=f"authorized runtime operation is not open: {authority.operation_id}",
            subject_id=item_id,
            locator=authority.operation_ref,
            recovery_hint="Re-run readiness or restart the runtime operation before mutating.",
            evidence={"authority": authority.to_json()},
        )
        return InventoryItem(
            id=item_id,
            domain="operation",
            kind="authorized_open_operation",
            locator=authority.operation_ref,
            source_kind="readiness_request",
            source_store="coordinator",
            health=missing(issue_codes=(issue.code,), authority_role="authoritative"),
            role=("operation", "live_authority"),
            fields={"operation_id": authority.operation_id, "role": authority.role},
            source_identity={"operation_id": authority.operation_id, "operation_ref": authority.operation_ref},
            issues=(issue,),
        )
    issue = InventoryIssue(
        id=issue_id(item_id, "readiness_operation_authority_mismatch"),
        code="readiness_operation_authority_mismatch",
        message=f"authorized runtime operation does not match the open operation: {authority.operation_id}",
        subject_id=item_id,
        locator=getattr(candidate, "ref", authority.operation_ref),
        recovery_hint="Re-run readiness with the current operation authority before mutating.",
        evidence={"authority": authority.to_json(), "open_operation": _operation_authority_evidence(candidate)},
    )
    return InventoryItem(
        id=item_id,
        domain="operation",
        kind="authorized_open_operation",
        locator=getattr(candidate, "ref", authority.operation_ref),
        source_kind="git_ref",
        source_store="coordinator",
        health=present_invalid(
            primary_issue="identity_mismatch",
            issue_codes=(issue.code,),
            lifecycle="active",
            authority_role="authoritative",
        ),
        role=("operation", "live_authority"),
        fields={"operation_id": authority.operation_id, "role": authority.role},
        source_identity=_operation_source_identity(store, candidate),
        issues=(issue,),
    )


def _scope_mismatched_operation_authority_item(
    store: Store,
    authority: ReadinessOperationAuthority,
    operation: Any,
    *,
    scope_ref: str,
) -> InventoryItem:
    item_id = _operation_authority_item_id(authority)
    issue = InventoryIssue(
        id=issue_id(item_id, "readiness_operation_scope_mismatch"),
        code="readiness_operation_scope_mismatch",
        message=f"authorized runtime operation is scoped to {getattr(operation, 'scope_ref', None)}, not {scope_ref}",
        subject_id=item_id,
        locator=getattr(operation, "ref", authority.operation_ref),
        recovery_hint="Re-run readiness for the operation's scope or close the operation before mutating another scope.",
        evidence={
            "request_scope_ref": scope_ref,
            "authority": authority.to_json(),
            "open_operation": _operation_authority_evidence(operation),
        },
    )
    return InventoryItem(
        id=item_id,
        domain="operation",
        kind="authorized_open_operation",
        locator=getattr(operation, "ref", authority.operation_ref),
        source_kind="git_ref",
        source_store="coordinator",
        health=present_invalid(
            primary_issue="identity_mismatch",
            issue_codes=(issue.code,),
            lifecycle="active",
            authority_role="authoritative",
        ),
        role=("operation", "live_authority"),
        fields={
            "operation_id": authority.operation_id,
            "operation_ref": getattr(operation, "ref", authority.operation_ref),
            "scope_ref": getattr(operation, "scope_ref", None),
            "requested_scope_ref": scope_ref,
            "role": authority.role,
        },
        source_identity=_operation_source_identity(store, operation),
        issues=(issue,),
    )


def _nested_child_quiescence_item(
    store: Store,
    authority: ReadinessOperationAuthority,
    operation: Any,
    *,
    scope_ref: str,
    sourced_authority: _SourcedOperationAuthority,
    runtime_admission_context: RuntimeAdmissionContext | None,
) -> InventoryItem | None:
    child_scope_ref = getattr(operation, "scope_ref", None)
    parent_scope_ref = getattr(operation, "nested_parent_scope_ref", None)
    persisted_child_ref = getattr(operation, "nested_child_scope_ref", None)
    if not sourced_authority.owner_derived or parent_scope_ref != scope_ref or persisted_child_ref != child_scope_ref:
        return None
    disposition = getattr(operation, "world_disposition", None) or "adopt"
    if disposition not in {"adopt", "release"}:
        return None
    if disposition == "release" or (
        runtime_admission_context is not None and runtime_admission_context.record_class == "trace_evidence"
    ):
        return _nested_child_quiescence_exemption_item(
            store,
            authority,
            operation,
            scope_ref=scope_ref,
            disposition=disposition,
            sourced_authority=sourced_authority,
            runtime_admission_context=runtime_admission_context,
        )
    return _nested_child_quiescence_blocker_item(
        store,
        authority,
        operation,
        scope_ref=scope_ref,
        disposition=disposition,
        sourced_authority=sourced_authority,
    )


def _nested_child_quiescence_exemption_item(
    store: Store,
    authority: ReadinessOperationAuthority,
    operation: Any,
    *,
    scope_ref: str,
    disposition: str,
    sourced_authority: _SourcedOperationAuthority,
    runtime_admission_context: RuntimeAdmissionContext | None,
) -> InventoryItem:
    operation_ref = str(operation.ref)
    operation_id = str(operation.durable_id)
    item_id = _operation_authority_item_id(authority)
    return InventoryItem(
        id=item_id,
        domain="operation",
        kind="nested_child_quiescence_exempt",
        locator=operation_ref,
        source_kind="git_ref",
        source_store="coordinator",
        health=present_valid(lifecycle="active", authority_role="authoritative"),
        role=("operation", "live_authority"),
        fields={
            "operation_id": operation_id,
            "operation_ref": operation_ref,
            "operation_kind": str(operation.kind),
            "scope_ref": str(operation.scope_ref),
            "requested_scope_ref": scope_ref,
            "nested_parent_scope_ref": getattr(operation, "nested_parent_scope_ref", None),
            "nested_child_scope_ref": getattr(operation, "nested_child_scope_ref", None),
            "world_disposition": disposition,
            "authority_sources": sorted(sourced_authority.sources),
            "record_class": None if runtime_admission_context is None else runtime_admission_context.record_class,
            "role": authority.role,
        },
        source_identity=_operation_source_identity(store, operation),
    )


def _nested_child_quiescence_blocker_item(
    store: Store,
    authority: ReadinessOperationAuthority,
    operation: Any,
    *,
    scope_ref: str,
    disposition: str,
    sourced_authority: _SourcedOperationAuthority,
) -> InventoryItem:
    operation_ref = str(operation.ref)
    item_id = _operation_authority_item_id(authority)
    issue = InventoryIssue(
        id=issue_id(item_id, "readiness_nested_child_quiescence"),
        code="readiness_nested_child_quiescence",
        message=(
            f"live child operation {getattr(operation, 'durable_id', None)} on {getattr(operation, 'scope_ref', None)} "
            f"blocks parent mutation on {scope_ref}"
        ),
        subject_id=item_id,
        locator=operation_ref,
        recovery_hint=(
            "Finish or archive the child operation before mutating its parent, or discard the child scope and fork fresh."
        ),
        evidence={
            "request_scope_ref": scope_ref,
            "authority": authority.to_json(),
            "open_operation": _operation_authority_evidence(operation),
            "world_disposition": disposition,
            "authority_sources": sorted(sourced_authority.sources),
        },
    )
    return InventoryItem(
        id=item_id,
        domain="operation",
        kind="nested_child_quiescence",
        locator=operation_ref,
        source_kind="git_ref",
        source_store="coordinator",
        health=present_invalid(
            primary_issue="identity_mismatch",
            issue_codes=(issue.code,),
            lifecycle="active",
            authority_role="authoritative",
        ),
        role=("operation", "live_authority"),
        fields={
            "operation_id": getattr(operation, "durable_id", None),
            "operation_ref": operation_ref,
            "scope_ref": getattr(operation, "scope_ref", None),
            "requested_scope_ref": scope_ref,
            "nested_parent_scope_ref": getattr(operation, "nested_parent_scope_ref", None),
            "nested_child_scope_ref": getattr(operation, "nested_child_scope_ref", None),
            "world_disposition": disposition,
            "authority_sources": sorted(sourced_authority.sources),
            "role": authority.role,
        },
        source_identity=_operation_source_identity(store, operation),
        issues=(issue,),
    )


def _filter_authorized_operation_recovery_items(
    items: tuple[InventoryItem,...],
    authorized_operation_refs: set[str],
    *,
    request: ReadinessRequest,
    scope_ref: str,
    current_session_id: str | None,
) -> tuple[InventoryItem,...]:
    if not authorized_operation_refs and request.command != "vcscore.runtime":
        return items
    return tuple(
        item
        for item in items
        if not _runtime_operation_recovery_item_is_live_authority_context(
            item,
            authorized_operation_refs=authorized_operation_refs,
            request=request,
            scope_ref=scope_ref,
            current_session_id=current_session_id,
        )
    )


def _runtime_operation_recovery_item_is_live_authority_context(
    item: InventoryItem,
    *,
    authorized_operation_refs: set[str],
    request: ReadinessRequest,
    scope_ref: str,
    current_session_id: str | None,
) -> bool:
    if item.domain != "recovery" or item.kind != "orphaned_operation_ref":
        return False
    if item.locator in authorized_operation_refs:
        return True
    if request.command != "vcscore.runtime" or current_session_id is None:
        return False
    return item.fields.get("session_id") == current_session_id and item.fields.get("scope_ref") != scope_ref


def _operation_authority_item_id(authority: ReadinessOperationAuthority) -> str:
    return f"operation:authorized:{authority.operation_id}"


def _operation_authority_evidence(operation: Any) -> dict[str, object]:
    return {
        "operation_id": getattr(operation, "durable_id", None),
        "operation_ref": getattr(operation, "ref", None),
        "kind": getattr(operation, "kind", None),
        "scope_ref": getattr(operation, "scope_ref", None),
        "scope_instance_id": getattr(operation, "scope_instance_id", None),
        "session_id": getattr(operation, "session_id", None),
        "world_disposition": getattr(operation, "world_disposition", None),
        "nested_parent_scope_ref": getattr(operation, "nested_parent_scope_ref", None),
        "nested_child_scope_ref": getattr(operation, "nested_child_scope_ref", None),
    }


def _operation_source_identity(store: Store, operation: Any) -> dict[str, object]:
    ref = str(operation.ref)
    identity: dict[str, object] = {
        **_operation_authority_evidence(operation),
        "exists": store.ref_exists(ref),
    }
    try:
        commit = store.resolve_to_commit(ref)
    except Exception: # noqa: BLE001
        return identity
    if commit is not None:
        identity["ref_target_oid"] = str(commit.id)
    return identity


def _inventory_items_for_policy(
    repo_path: str | Path,
    request: ReadinessRequest,
    policy: ReadinessPolicy,
    scope_ref: str,
    *,
    owner: VcsCore | None,
    operation_authorities: tuple[_SourcedOperationAuthority,...],
    nested_authorizations: tuple[Any,...] = (),
    runtime_admission_context: RuntimeAdmissionContext | None = None,
) -> tuple[InventoryItem,...]:
    items: list[InventoryItem] = []
    domains = policy.observed_domains
    store = owner.store if owner is not None else None
    authorized_operation_refs: set[str] = set()
    if "scope" in domains:
        items.append(probe_scope(repo_path, request.scope_selector))
    if "authority_ref" in domains:
        items.append(probe_authority_ref(repo_path, scope_ref))
    if "world" in domains:
        items.extend(probe_selected_world(repo_path, scope_ref, required_bindings=request.required_bindings))
    if "workspace_authority" in domains:
        items.extend(probe_workspace_authority_pending(repo_path))
    if "authority_settlement" in domains:
        items.extend(probe_authority_settlement_pending(repo_path))
    if "operation_journal" in domains:
        # Admission (mutating policies) reads the bounded index-backed source; status/read-only
        # policies keep the scanning source. This is THE structural boundary: a mutating policy
        # never observes an operation-journal-namespace enumeration (the count-contract).
        if policy.mutates:
            items.extend(_admission_operation_journal_items(repo_path))
        else:
            items.extend(_status_operation_journal_items(repo_path))
    if "operation" in domains:
        store = store or _open_store(repo_path)
        operation_items, authorized_operation_refs = _operation_authority_items(
            store,
            operation_authorities,
            scope_ref=scope_ref,
            nested_authorizations=nested_authorizations,
            runtime_admission_context=runtime_admission_context,
        )
        items.extend(operation_items)
    if "recovery" in domains:
        store = store or _open_store(repo_path)
        recovery_items = _recovery_inventory_items(repo_path, store, owner=owner)
        items.extend(
            _filter_authorized_operation_recovery_items(
                recovery_items,
                authorized_operation_refs,
                request=request,
                scope_ref=scope_ref,
                current_session_id=None if owner is None else owner._session_id,
            )
        )
    return tuple(items)


# Readiness/admission observes only the `open` operation-journal family. A healthy non-`open`
# terminal (`closed`/`archived`) journal can never block readiness: `_derive_blockers` fires the
# operation-journal blocker only on `lifecycle=="active"` or tip `status in {failed,recovery_required}`,
# and a terminal-family ref structurally cannot carry those statuses (`_world_operation_journal.py`
#:395-399 rejects a closed/archived ref whose tip is not closed/archived). The only way a terminal
# blocks today is corruption (the generic `validity=="invalid"` fallback) — over-blocking whose
# integrity belongs to the explicit, off-hot-path `WorldStorageManager.fsck_operation_journals()`,
# not to admission. Scoping to `open` sheds the dominant per-journal blob-read term of the scan
# (Cost-B leg 3; `260622-step2-open-only-admission.md`). A residual O(total refs) enumeration leg
# remains (the scoped probe still walks the namespace) — that is later pruning/accelerator work.
_ADMISSION_JOURNAL_FAMILY = "open"


def _admission_operation_journal_items(repo_path: str | Path) -> tuple[InventoryItem,...]:
    """Bounded admission source: read the open-journal index, probe ONLY those refs (no namespace scan).

    The only ``operation_journal`` source a *mutating* policy observes. Index present → bounded;
    missing → one fallback scan + opportunistic self-heal; corrupt → a single fail-closed blocking
    fact. See:func:`admission_operation_journal_items`.
    """
    from vcs_core._operation_journal_inventory import admission_operation_journal_items
    from vcs_core._world_storage_installation import default_world_storage_exists, open_existing_default_world_storage

    if not default_world_storage_exists(repo_path):
        return ()
    try:
        manager = open_existing_default_world_storage(repo_path)
    except Exception: # noqa: BLE001
        return ()
    return admission_operation_journal_items(manager)


def _status_operation_journal_items(repo_path: str | Path) -> tuple[InventoryItem,...]:
    """Status source (may scan): enumerate the open family off the mutation gate (step-2 behavior).

    Read-only/status policies keep the O(total-refs) open scan; it never gates mutation. Splitting
    admission (bounded) from status (scanning) is what lets the two diverge without a side effect.
    """
    from vcs_core._world_storage_installation import default_world_storage_exists, open_existing_default_world_storage

    if not default_world_storage_exists(repo_path):
        return ()
    try:
        manager = open_existing_default_world_storage(repo_path)
    except Exception: # noqa: BLE001
        return ()
    return probe_operation_journals(manager.world_store.repo, family=_ADMISSION_JOURNAL_FAMILY)


def _recovery_inventory_items(
    repo_path: str | Path,
    store: Store,
    *,
    owner: VcsCore | None,
) -> tuple[InventoryItem,...]:
    if owner is not None:
        return recovery_inventory_snapshot(owner).items
    return recovery_inventory_snapshot_for_store(repo_path, store).items


def _derive_blockers(
    request: ReadinessRequest,
    policy: ReadinessPolicy,
    items: tuple[InventoryItem,...],
) -> tuple[tuple[ReadinessBlocker,...], tuple[InventoryIssue,...]]:
    if not policy.mutates:
        return (), ()
    blockers: list[ReadinessBlocker] = []
    policy_issues: list[InventoryIssue] = []
    matched_target_item_ids: set[str] = set()
    for item in items:
        if item.domain not in policy.blocking_domains:
            continue
        target_match = _item_matches_recovery_target(request, item)
        if item.domain == "workspace_authority" and item.health.presence == "present":
            issue = _policy_issue(
                item,
                code="readiness_workspace_authority_pending",
                message="workspace authority recovery is pending",
                recovery_hint="Run `vcs-core recover-workspace-authority` before starting mutating work.",
            )
            policy_issues.append(issue)
            if _target_recovery_allows(request, item, issue, target_match):
                matched_target_item_ids.add(item.id)
                continue
            blockers.append(_blocker(request, item, issue))
            continue
        if item.domain == "authority_settlement" and item.health.presence == "present":
            issue = _policy_issue(
                item,
                code="readiness_authority_settlement_pending",
                message="authority settlement recovery is pending",
                recovery_hint=(
                    "Run recover_authority_settlements() before starting mutating work."
                ),
            )
            policy_issues.append(issue)
            if _target_recovery_allows(request, item, issue, target_match):
                matched_target_item_ids.add(item.id)
                continue
            blockers.append(_blocker(request, item, issue))
            continue
        if item.domain == "operation_journal":
            journal_status = item.fields.get("status")
            if item.health.lifecycle == "active" or journal_status in {"failed", "recovery_required"}:
                issue = _policy_issue(
                    item,
                    code="readiness_operation_journal_open",
                    message=f"operation journal blocks mutating readiness: {item.locator}",
                    recovery_hint="Run vcs-core recovery for the pending world-vector operation before mutating.",
                )
                policy_issues.append(issue)
                if _target_recovery_allows(request, item, issue, target_match):
                    matched_target_item_ids.add(item.id)
                    continue
                blockers.append(_blocker(request, item, issue))
                continue
        if (
            item.domain == "recovery"
            and item.health.validity == "invalid"
            and _policy_blocks_recovery_item(policy, item)
        ):
            blocking_issue = item.issues[0] if item.issues else None
            if _target_recovery_allows(request, item, blocking_issue, target_match):
                matched_target_item_ids.add(item.id)
                continue
            blockers.append(_blocker(request, item, blocking_issue))
            continue
        if item.domain == "recovery":
            continue
        if item.disposition is not None:
            # Part C: a fact that DECLARES its Tier-2 disposition is classified here, before the
            # generic validity fallback. Un-migrated facts (disposition is None) fall through to the
            # legacy rules — no wholesale rewrite. Today only the open-journal-index facts declare one
            # (the corrupt-index admission fact reaches here; the open journal / recovery items are
            # already handled by their specific branches above, so this only sees the new facts).
            if item.disposition == "blocking":
                blocking_issue = item.issues[0] if item.issues else None
                if _target_recovery_allows(request, item, blocking_issue, target_match):
                    matched_target_item_ids.add(item.id)
                else:
                    blockers.append(_blocker(request, item, blocking_issue))
            # "diagnostic" never blocks; "recoverable" facts are targeted via the recovery branches
            # above and never reach here. A declared disposition is fully classified.
            continue
        if item.health.presence == "absent" or item.health.validity == "invalid":
            blocking_issue = item.issues[0] if item.issues else None
            if _target_recovery_allows(request, item, blocking_issue, target_match):
                matched_target_item_ids.add(item.id)
                continue
            blockers.append(_blocker(request, item, blocking_issue))
    if request.command in _RECOVERY_COMMANDS:
        missing_target_issues = _missing_recovery_target_issues(request, items, matched_target_item_ids)
        policy_issues.extend(missing_target_issues)
        blockers.extend(
            _blocker(request, _recovery_target_subject_item(items), issue) for issue in missing_target_issues
        )
    return tuple(blockers), tuple(policy_issues)


def _system_recovery_required(
    items: tuple[InventoryItem,...],
    policy_issues: tuple[InventoryIssue,...],
    policy: ReadinessPolicy,
) -> bool:
    if any(_issue_is_recoverable(issue) for issue in policy_issues):
        return True
    for item in items:
        if item.domain not in policy.health_domains:
            continue
        if item.domain == "workspace_authority" and item.health.presence == "present":
            return True
        if item.domain == "authority_settlement" and item.health.presence == "present":
            return True
        if (
            item.domain == "recovery"
            and item.health.validity == "invalid"
            and _policy_blocks_recovery_item(policy, item)
        ):
            return True
        if item.domain == "recovery":
            continue
        if item.domain == "operation_journal":
            journal_status = item.fields.get("status")
            if item.health.lifecycle == "active" or journal_status in {"failed", "recovery_required"}:
                return True
        if item.health.validity == "invalid" and any(_issue_is_recoverable(issue) for issue in item.issues):
            return True
    return False


def _consumed_item_ids(policy: ReadinessPolicy, items: tuple[InventoryItem,...]) -> tuple[str,...]:
    if not policy.mutates:
        return ()
    return tuple(
        item.id
        for item in items
        if item.domain in policy.precondition_domains
        and item.health.presence == "present"
        and (item.health.validity == "valid" or policy.command in _RECOVERY_COMMANDS)
    )


def _policy_blocks_recovery_item(policy: ReadinessPolicy, item: InventoryItem) -> bool:
    if item.domain != "recovery":
        return False
    return item.kind in policy.blocking_recovery_kinds


def _targets_from_json(value: object) -> tuple[ReadinessTarget,...]:
    if not isinstance(value, list):
        raise TypeError("targets must be an array")
    return tuple(ReadinessTarget.from_json(item) for item in value)


def _authorized_operations_from_json(value: object) -> tuple[ReadinessOperationAuthority,...]:
    if not isinstance(value, list):
        raise TypeError("authorized_operations must be an array")
    return tuple(ReadinessOperationAuthority.from_json(item) for item in value)


def _validate_targets_for_command(command: ReadinessCommand, targets: tuple[ReadinessTarget,...]) -> None:
    if command not in _RECOVERY_COMMANDS:
        return
    for target in targets:
        if target.domain != "recovery":
            continue
        if target.kind is None:
            raise ValueError("explicit recovery-domain targets must include kind")
        if not any((target.item_id, target.locator, target.operation_id, target.family)):
            raise ValueError("explicit recovery-domain targets must include item_id, locator, operation_id, or family")


def _validate_authorized_operations_for_command(
    command: ReadinessCommand,
    authorized_operations: tuple[ReadinessOperationAuthority,...],
) -> None:
    if not authorized_operations:
        return
    if command != "vcscore.runtime":
        raise ValueError("authorized operation authorities are only supported for vcscore.runtime readiness")
    seen_operation_ids: set[str] = set()
    for authority in authorized_operations:
        if authority.role != "runtime":
            raise ValueError("authorized operation authorities must use role='runtime'")
        if authority.operation_id in seen_operation_ids:
            raise ValueError("authorized operation authorities must be unique by operation_id")
        seen_operation_ids.add(authority.operation_id)


def _item_matches_recovery_target(request: ReadinessRequest, item: InventoryItem) -> bool:
    if request.command not in _RECOVERY_COMMANDS:
        return False
    if not request.targets:
        return _item_has_recoverable_issue(item)
    return any(_target_matches_item(target, item) for target in request.targets)


def _target_matches_item(target: ReadinessTarget, item: InventoryItem) -> bool:
    if target.domain != item.domain:
        return False
    if target.kind is not None and target.kind != item.kind:
        return False
    matched_selector = False
    if target.item_id is not None:
        matched_selector = True
        if target.item_id != item.id:
            return False
    if target.locator is not None:
        matched_selector = True
        if target.locator != item.locator:
            return False
    if target.operation_id is not None:
        matched_selector = True
        operation_id = item.fields.get("operation_id") or item.fields.get("payload_operation_id")
        if operation_id != target.operation_id:
            return False
    if target.family is not None:
        matched_selector = True
        if item.fields.get("family") != target.family:
            return False
    return matched_selector


def _target_recovery_allows(
    request: ReadinessRequest,
    item: InventoryItem,
    issue: InventoryIssue | None,
    target_match: bool,
) -> bool:
    if request.command not in _RECOVERY_COMMANDS or not target_match:
        return False
    if issue is not None and _issue_is_recoverable(issue):
        return True
    return _item_has_recoverable_issue(item)


def _item_has_recoverable_issue(item: InventoryItem) -> bool:
    if item.domain == "workspace_authority" and item.health.presence == "present":
        return True
    if item.domain == "authority_settlement" and item.health.presence == "present":
        return True
    if item.domain == "operation_journal" and item.health.lifecycle == "active":
        return True
    return any(_issue_is_recoverable(issue) for issue in item.issues)


def _issue_is_recoverable(issue: InventoryIssue | None) -> bool:
    return issue is not None and issue.code in _RECOVERABLE_ISSUES


def _missing_recovery_target_issues(
    request: ReadinessRequest,
    items: tuple[InventoryItem,...],
    matched_target_item_ids: set[str],
) -> tuple[InventoryIssue,...]:
    issues: list[InventoryIssue] = []
    for target in request.targets:
        matched_items = tuple(item for item in items if _target_matches_item(target, item))
        if any(item.id in matched_target_item_ids for item in matched_items):
            continue
        target_family = target.kind or target.domain
        target_selector = target.item_id or target.locator or target.operation_id or target.family or "unknown"
        subject_id = f"readiness_target:{target.domain}:{target_family}:{target_selector}"
        if matched_items:
            issues.append(
                InventoryIssue(
                    id=issue_id(subject_id, "readiness_recovery_target_not_recoverable"),
                    code="readiness_recovery_target_not_recoverable",
                    message="requested recovery target is present but has no recoverable issue",
                    subject_id=subject_id,
                    locator=target.locator,
                    recovery_hint="Inspect readiness inventory and retry recovery with a recoverable target.",
                    evidence={
                        "target": target.to_json(),
                        "matched_item_ids": [item.id for item in matched_items],
                        "matched_target_item_ids": sorted(matched_target_item_ids),
                    },
                )
            )
            continue
        issues.append(
            InventoryIssue(
                id=issue_id(subject_id, "readiness_recovery_target_missing"),
                code="readiness_recovery_target_missing",
                message="requested recovery target was not found in readiness inventory",
                subject_id=subject_id,
                locator=target.locator,
                recovery_hint="Inspect readiness inventory and retry recovery with an existing target.",
                evidence={"target": target.to_json(), "matched_target_item_ids": sorted(matched_target_item_ids)},
            )
        )
    return tuple(issues)


def _recovery_target_subject_item(items: tuple[InventoryItem,...]) -> InventoryItem:
    for item in items:
        if item.domain == "scope":
            return item
    return items[0]


def _unverifiable_consumed_fact_issues(
    items: tuple[InventoryItem,...],
    item_ids: tuple[str,...],
) -> tuple[InventoryIssue,...]:
    issues: list[InventoryIssue] = []
    for item_id in item_ids:
        item = _item_by_id(items, item_id)
        if item.source_identity:
            continue
        issues.append(
            _policy_issue(
                item,
                code="readiness_consumed_fact_unverifiable",
                message=f"readiness consumed fact has no source identity: {item.id}",
                recovery_hint="Inspect readiness inventory before starting mutating work.",
            )
        )
    return tuple(issues)


def _item_by_id(items: tuple[InventoryItem,...], item_id: str) -> InventoryItem:
    for item in items:
        if item.id == item_id:
            return item
    raise KeyError(item_id)


def _blocker(request: ReadinessRequest, item: InventoryItem, issue: InventoryIssue | None) -> ReadinessBlocker:
    return ReadinessBlocker(
        id=f"blocker:{request.command}:{item.domain}:{item.id}",
        kind=item.domain,
        command=request.command,
        item_id=item.id,
        issue_id=None if issue is None else issue.id,
        recovery_hint=None if issue is None else issue.recovery_hint,
    )


def _policy_issue(item: InventoryItem, *, code: str, message: str, recovery_hint: str) -> InventoryIssue:
    return InventoryIssue(
        id=issue_id(item.id, code),
        code=code,
        message=message,
        subject_id=item.id,
        locator=item.locator,
        recovery_hint=recovery_hint,
    )


def _bindings_from_json(value: object) -> tuple[RequiredBinding,...]:
    if not isinstance(value, list):
        raise TypeError("required_bindings must be an array")
    bindings: list[RequiredBinding] = []
    for item in value:
        if not isinstance(item, dict):
            raise TypeError("required_bindings entries must be objects")
        binding = _required_str(item, "binding")
        bindings.append(
            RequiredBinding(
                binding=binding,
                head_kind=_optional_str(item, "head_kind"),
                role=_optional_str(item, "role"),
                check=_required_str(item, "check", default="selected_head"),
            )
        )
    return tuple(bindings)


def _required_str(payload: dict[str, object], key: str, *, default: str | None = None) -> str:
    value = payload.get(key, default)
    if not isinstance(value, str) or not value:
        raise TypeError(f"{key} must be a non-empty string")
    return value


def _optional_str(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise TypeError(f"{key} must be a non-empty string when provided")
    return value


def _optional_bool(payload: dict[str, object], key: str, *, default: bool | None) -> bool | None:
    value = payload.get(key, default)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise TypeError(f"{key} must be a boolean")
    return value


def _normalize_freshness(value: str) -> ReadinessFreshness:
    if value in {"best_effort", "locked", "revalidated"}:
        return value # type: ignore[return-value]
    raise ValueError(f"unknown readiness freshness: {value!r}")


def _policy_for(command: ReadinessCommand) -> ReadinessPolicy:
    return _READINESS_POLICIES[command]
