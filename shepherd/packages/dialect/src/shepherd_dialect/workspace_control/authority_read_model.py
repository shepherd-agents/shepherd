"""Read-only public views over persisted workspace-control authority evidence."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING

from shepherd_dialect.workspace_control.errors import WorkspaceControlError
from shepherd_dialect.workspace_control.workspace_authority import validate_run_authority_context

if TYPE_CHECKING:
    from shepherd2.schemas.run_outputs import RunOutputRef

    from shepherd_dialect.workspace_control.schemas import RunRecord

JsonObject = dict[str, object]


@dataclass(frozen=True)
class RunAuthority:
    """Validated read model for one run's persisted authority context.

    This is an inspection surface, not a capability token. Settlement and
    custody remain owned by vcs-core retained-output state.
    """

    run_ref: str
    schema: str
    task_default_may: str
    requested_may: str | None
    effective_may: str
    repo_authority: str
    workspace_selection_can_mutate: bool
    effective_grant_ref: str
    effective_grant_digest: str
    grant_clamp_digest: str
    effective_match_digest: str
    authority_surface_plan_digest: str
    uses_signature_gitrepo_grant: bool
    effective_grant: JsonObject
    grant_clamp: JsonObject
    classifier_policy: JsonObject

    def to_json(self) -> JsonObject:
        """Return a stable JSON-shaped authority inspection payload."""
        return {
            "run_ref": self.run_ref,
            "schema": self.schema,
            "task_default_may": self.task_default_may,
            "requested_may": self.requested_may,
            "effective_may": self.effective_may,
            "repo_authority": self.repo_authority,
            "workspace_selection_can_mutate": self.workspace_selection_can_mutate,
            "effective_grant_ref": self.effective_grant_ref,
            "effective_grant_digest": self.effective_grant_digest,
            "grant_clamp_digest": self.grant_clamp_digest,
            "effective_match_digest": self.effective_match_digest,
            "authority_surface_plan_digest": self.authority_surface_plan_digest,
            "uses_signature_gitrepo_grant": self.uses_signature_gitrepo_grant,
            "effective_grant": _copy_json_object(self.effective_grant),
            "grant_clamp": _copy_json_object(self.grant_clamp),
            "classifier_policy": _copy_json_object(self.classifier_policy),
        }


@dataclass(frozen=True)
class RunOutputSettlementPolicy:
    """Read-only settlement policy view for one retained run output."""

    output_id: str
    output_name: str
    binding: str
    run_ref: str
    state: str
    settlement_ref: str | None
    authority: RunAuthority
    settlement_policy: JsonObject | None
    custody_owner: str = "vcs-core.retained-output"
    consume_once: bool = True
    settlement_verbs: tuple[str, ...] = ("select", "apply", "release", "discard")

    def to_json(self) -> JsonObject:
        """Return a stable JSON-shaped settlement-policy inspection payload."""
        return {
            "output_id": self.output_id,
            "output_name": self.output_name,
            "binding": self.binding,
            "run_ref": self.run_ref,
            "state": self.state,
            "settlement_ref": self.settlement_ref,
            "custody_owner": self.custody_owner,
            "consume_once": self.consume_once,
            "settlement_verbs": list(self.settlement_verbs),
            "settlement_policy": None if self.settlement_policy is None else _copy_json_object(self.settlement_policy),
            "authority": self.authority.to_json(),
        }


@dataclass(frozen=True)
class RunOutputSettlementEvidence:
    """Joined read-only view of run authority, custody receipt, and settlement monitor evidence."""

    output_id: str
    output_name: str
    binding: str
    run_ref: str
    state: str
    settlement_ref: str | None
    settlement_action: str | None
    settlement_operation_id: str | None
    authority: RunAuthority
    authority_operation_id: str | None = None
    authority_settlement_operation_id: str | None = None
    authority_outcome: str | None = None
    permission_plan_digest: str | None = None
    permission_plan_descriptor: JsonObject | None = None
    authority_settlement: JsonObject | None = None
    custody_owner: str = "vcs-core.retained-output"

    def to_json(self) -> JsonObject:
        """Return a stable JSON-shaped settlement evidence payload."""
        return {
            "output_id": self.output_id,
            "output_name": self.output_name,
            "binding": self.binding,
            "run_ref": self.run_ref,
            "state": self.state,
            "settlement_ref": self.settlement_ref,
            "settlement_action": self.settlement_action,
            "settlement_operation_id": self.settlement_operation_id,
            "custody_owner": self.custody_owner,
            "authority_operation_id": self.authority_operation_id,
            "authority_settlement_operation_id": self.authority_settlement_operation_id,
            "authority_outcome": self.authority_outcome,
            "permission_plan_digest": self.permission_plan_digest,
            "permission_plan_descriptor": (
                None if self.permission_plan_descriptor is None else _copy_json_object(self.permission_plan_descriptor)
            ),
            "authority_settlement": (
                None if self.authority_settlement is None else _copy_json_object(self.authority_settlement)
            ),
            "authority": self.authority.to_json(),
        }


def run_authority_from_record(record: RunRecord) -> RunAuthority:
    """Hydrate and validate a public authority view from a run record."""
    context = getattr(record, "authority_context", None)
    if context is None:
        run_ref = getattr(record, "run_ref", "<unknown>")
        raise WorkspaceControlError(f"run {run_ref!r} has no recorded authority context")
    try:
        validated = validate_run_authority_context(context)
    except (TypeError, ValueError) as exc:
        run_ref = getattr(record, "run_ref", "<unknown>")
        raise WorkspaceControlError(f"run {run_ref!r} has invalid authority context: {exc}") from exc

    return RunAuthority(
        run_ref=record.run_ref,
        schema=context.schema,
        task_default_may=context.task_default_may,
        requested_may=context.requested_may,
        effective_may=context.effective_may,
        repo_authority=context.repo_authority,
        workspace_selection_can_mutate=context.workspace_selection_can_mutate,
        effective_grant_ref=validated.effective_grant.grant_ref,
        effective_grant_digest=context.effective_grant_digest,
        grant_clamp_digest=validated.grant_clamp.digest,
        effective_match_digest=context.effective_match_digest,
        authority_surface_plan_digest=context.authority_surface_plan_digest,
        uses_signature_gitrepo_grant=validated.uses_signature_gitrepo_grant,
        effective_grant=_copy_json_object(validated.effective_grant.to_descriptor()),
        grant_clamp=_copy_json_object(validated.grant_clamp.to_descriptor()),
        classifier_policy=_copy_json_object(context.classifier_policy),
    )


def run_output_settlement_policy_from_record(
    output: RunOutputRef,
    record: RunRecord,
) -> RunOutputSettlementPolicy:
    """Hydrate a public settlement-policy view from current output state and run authority."""
    owner = getattr(output, "owner", None)
    run_ref = getattr(owner, "run_id", None)
    if not isinstance(run_ref, str) or not run_ref:
        raise WorkspaceControlError("run-output settlement policy requires a run-owned output")
    if run_ref != record.run_ref:
        raise WorkspaceControlError("run-output settlement policy run identity disagrees with run record")

    authority = run_authority_from_record(record)
    raw_policy = getattr(record.launch_context, "settlement_policy", None)
    settlement_policy = _copy_json_object(raw_policy) if raw_policy is not None else None
    return RunOutputSettlementPolicy(
        output_id=output.identity.output_id,
        output_name=output.identity.output_name,
        binding=output.identity.binding,
        run_ref=run_ref,
        state=output.state,
        settlement_ref=output.settlement_ref,
        authority=authority,
        settlement_policy=settlement_policy,
    )


def run_output_settlement_evidence_from_record(
    output: RunOutputRef,
    record: RunRecord,
    *,
    retained_row: object,
    authority_settlement: Mapping[str, object] | None = None,
) -> RunOutputSettlementEvidence:
    """Hydrate a joined settlement evidence view from custody state and optional authority history."""
    owner = getattr(output, "owner", None)
    run_ref = getattr(owner, "run_id", None)
    if not isinstance(run_ref, str) or not run_ref:
        raise WorkspaceControlError("run-output settlement evidence requires a run-owned output")
    if run_ref != record.run_ref:
        raise WorkspaceControlError("run-output settlement evidence run identity disagrees with run record")

    authority = run_authority_from_record(record)
    settlement = getattr(retained_row, "settlement", None)
    settlement_action = getattr(settlement, "action", None) if settlement is not None else None
    settlement_operation_id = getattr(settlement, "operation_id", None) if settlement is not None else None
    authority_operation_id = getattr(settlement, "authority_operation_id", None) if settlement is not None else None
    authority_settlement_operation_id = (
        getattr(settlement, "authority_settlement_operation_id", None) if settlement is not None else None
    )
    authority_outcome = getattr(settlement, "authority_outcome", None) if settlement is not None else None
    normalized_authority_settlement = None if authority_settlement is None else _copy_json_object(authority_settlement)
    permission_plan_descriptor = None
    permission_plan_digest = None
    if normalized_authority_settlement is not None:
        raw_digest = normalized_authority_settlement.get("permission_plan_digest")
        raw_descriptor = normalized_authority_settlement.get("permission_plan_descriptor")
        permission_plan_digest = raw_digest if isinstance(raw_digest, str) else None
        permission_plan_descriptor = _copy_json_object(raw_descriptor) if isinstance(raw_descriptor, Mapping) else None

    return RunOutputSettlementEvidence(
        output_id=output.identity.output_id,
        output_name=output.identity.output_name,
        binding=output.identity.binding,
        run_ref=run_ref,
        state=output.state,
        settlement_ref=output.settlement_ref,
        settlement_action=settlement_action if isinstance(settlement_action, str) else None,
        settlement_operation_id=settlement_operation_id if isinstance(settlement_operation_id, str) else None,
        authority=authority,
        authority_operation_id=(authority_operation_id if isinstance(authority_operation_id, str) else None),
        authority_settlement_operation_id=(
            authority_settlement_operation_id if isinstance(authority_settlement_operation_id, str) else None
        ),
        authority_outcome=authority_outcome if isinstance(authority_outcome, str) else None,
        permission_plan_digest=permission_plan_digest,
        permission_plan_descriptor=permission_plan_descriptor,
        authority_settlement=normalized_authority_settlement,
    )


def _copy_json_object(value: Mapping[str, object]) -> JsonObject:
    return deepcopy(dict(value))
