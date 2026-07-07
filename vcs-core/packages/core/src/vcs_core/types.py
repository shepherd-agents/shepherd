"""Public data transfer objects for vcs-core.

All types are frozen dataclasses. No pygit2 types are exposed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from vcs_core._typed_json import encode_typed_json

if TYPE_CHECKING:
    from pathlib import Path

    from vcs_core._authority import AuthorityMergeResult
    from vcs_core._substrate_driver import (
        Diagnostic,
        DriverIngressResult,
        DriverSelectionRequirementDraft,
        ObservationDraft,
        RetentionHint,
        TransitionDraft,
    )
    from vcs_core._transition_kernel_records import PayloadDescriptorClaim


@dataclass(frozen=True)
class ScopeInfo:
    """Immutable record of a scope, returned by Store.fork().

    instance_id uniquely identifies this scope instance for stale-handle
    detection. world_id is the durable identity for the logical world.
    creation_oid records the parent commit SHA at fork time.
    """

    name: str
    ref: str
    instance_id: str
    creation_oid: str
    world_id: str | None = None


@dataclass(frozen=True)
class Status:
    """Materialization status returned by status()."""

    local_changes: int
    commits_ahead: int


@dataclass(frozen=True)
class CommitInfo:
    """Commit metadata returned by log()."""

    oid: str
    message: str
    timestamp: float
    metadata: dict[str, object]
    parent_oids: list[str]


OperationVisibility = Literal["visible", "staged", "archived"]
ArchivedVia = Literal["operation_ref", "discarded_world_ref"]


@dataclass(frozen=True)
class OperationSummary:
    """Summary of one execution operation across one visibility surface."""

    operation_id: str
    label: str | None
    kind: str
    status: str
    visibility: OperationVisibility
    world_id: str
    world_name: str
    world_ref: str
    carrier_ref: str
    archived_via: ArchivedVia | None = None
    parent_operation_id: str | None = None
    effect_count: int = 0
    started_at: float | None = None
    closed_at: float | None = None
    anchor_oid: str | None = None
    final_phase: str | None = None


@dataclass(frozen=True)
class OperationHistory:
    """Committed history carried by one staged or archived execution carrier."""

    summary: OperationSummary
    commits: tuple[CommitInfo, ...]


@dataclass(frozen=True)
class RecoverySnapshot:
    """Current non-canonical recovery/debug state for one repository."""

    orphaned_scope_refs: tuple[str, ...] = ()
    open_operations: tuple[OperationSummary, ...] = ()
    archived_recovery_operations: tuple[OperationSummary, ...] = ()
    orphaned_operations: tuple[OperationSummary, ...] = ()
    workspace_authority_pending: tuple[str, ...] = ()


@dataclass(frozen=True)
class SelectedBindingRevision:
    """Payload plus identity for one currently selected substrate binding head."""

    binding: str
    store_id: str
    resource_id: str
    head: str
    payload: dict[str, object]


@dataclass(frozen=True)
class FileChange:
    """A single file change in a diff."""

    path: str
    status: str  # "added", "modified", "deleted"


@dataclass(frozen=True)
class DiffSummary:
    """File-level diff returned by diff()."""

    files: list[FileChange]


@dataclass(frozen=True)
class MaterializationPhase:
    """A group of operations sharing a reversibility level."""

    reversibility: str  # "auto", "compensable", "none"
    file_changes: list[FileChange]
    intents: list[dict[str, object]]

    @property
    def operation_count(self) -> int:
        return len(self.file_changes) + len(self.intents)


@dataclass(frozen=True)
class MaterializationPlan:
    """Returned by push(). Groups pending work by reversibility phase."""

    phases: list[MaterializationPhase]
    commits_ahead: int

    @property
    def total_operations(self) -> int:
        return sum(p.operation_count for p in self.phases)

    @property
    def has_irreversible(self) -> bool:
        return any(p.reversibility == "none" for p in self.phases)


@dataclass(frozen=True)
class RebaseResult:
    """Returned by Store.rebase()."""

    commits_replayed: int
    oid_mapping: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RecordedCommandOutcome:
    """Result of recording a substrate command through VcsCore."""

    oids: tuple[str, ...] = ()
    value: object | None = None


@dataclass(frozen=True)
class SealCandidateHandoff:
    """Durable custody record for one sealed child scope's substrate output.

    ``changed_paths`` is advisory read/display metadata. Settlement authority
    is re-derived from worlds, candidate refs, and handoff identity.
    """

    seal_operation_id: str
    producer_operation_id: str
    scope_name: str
    scope_ref: str
    scope_instance_id: str
    scope_world_id: str | None
    parent_ref: str
    parent_basis_world_oid: str
    output_world_oid: str
    binding: str
    store_id: str
    resource_id: str
    candidate_id: str
    candidate_ref: str
    candidate_head: str
    candidate_tuple_digest: str
    handoff_ref: str
    changed_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetainedWorkspaceHandle:
    """Copyable read handle recovered from retained seal custody."""

    scope_name: str
    scope_ref: str
    scope_instance_id: str
    output_world_oid: str
    binding: str
    store_id: str
    resource_id: str
    head: str
    basis_ref: str
    changed_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class SealResult:
    """Result returned by ``VcsCore.seal``."""

    scope: ScopeInfo
    parent: ScopeInfo
    handoff: SealCandidateHandoff


@dataclass(frozen=True)
class SealedExecutionOutcome:
    """Outcome for an execution-bound run retained instead of merged."""

    driver_result: DriverIngressResult
    seal_result: SealResult

    @property
    def handoff(self) -> SealCandidateHandoff:
        return self.seal_result.handoff


@dataclass(frozen=True)
class AuthorityExecutionOutcome:
    """Outcome for an execution-bound run settled by authority."""

    driver_result: DriverIngressResult
    authority_result: AuthorityMergeResult


@dataclass(frozen=True)
class RetainedOutputSettlement:
    """Consume-once settlement receipt for one retained output."""

    scope_name: str
    scope_ref: str
    scope_instance_id: str
    parent_ref: str
    handoff_ref: str
    output_world_oid: str
    binding: str
    store_id: str
    resource_id: str
    candidate_id: str
    candidate_head: str
    action: Literal["selected", "applied", "released", "discarded"]
    operation_id: str
    parent_world_before: str
    parent_world_after: str
    settlement_ref: str
    authority_operation_id: str | None = None
    authority_settlement_operation_id: str | None = None
    authority_outcome: str | None = None
    # The published binding head after an ``apply`` three-way settlement. For a
    # fast-forward-degenerate apply (parent unmoved) this equals candidate_head;
    # for a genuine three-way apply it is the merged revision head. ``None`` for
    # every non-apply action.
    applied_head: str | None = None


@dataclass(frozen=True)
class RetainedOutputSelectionResult:
    """Result returned by ``VcsCore.select_retained_output``."""

    scope: ScopeInfo
    parent: ScopeInfo
    output_world_oid: str
    parent_world_before: str
    parent_world_after: str
    settlement: RetainedOutputSettlement
    authority_operation_id: str | None = None
    authority_settlement_operation_id: str | None = None
    authority_outcome: str | None = None


@dataclass(frozen=True)
class RetainedOutputSettlementResult:
    """Result returned by non-select retained-output settlement verbs."""

    scope: ScopeInfo
    parent: ScopeInfo
    output_world_oid: str
    parent_world_before: str
    parent_world_after: str
    settlement: RetainedOutputSettlement
    # Authority evidence for verbs that run the decide lane (apply, T1 D7) — parity with
    # RetainedOutputSelectionResult; None for authority-less settlements and recoveries.
    authority_operation_id: str | None = None
    authority_settlement_operation_id: str | None = None
    authority_outcome: str | None = None


RetainedOutputState = Literal["unconsumed", "selected", "applied", "released", "discarded", "invalid"]


@dataclass(frozen=True)
class RetainedOutputIdentity:
    """Exact identity for addressable retained-output custody lookup."""

    scope_name: str
    scope_ref: str
    scope_instance_id: str
    parent_ref: str
    parent_scope_name: str | None
    parent_scope_instance_id: str | None
    binding: str
    output_world_oid: str
    handoff_ref: str
    parent_basis_world_oid: str
    store_id: str
    resource_id: str
    candidate_id: str
    candidate_ref: str
    candidate_head: str


@dataclass(frozen=True)
class RetainedOutputQueryResult:
    """Read-only classification of one retained output from custody facts."""

    scope_name: str
    scope_ref: str
    scope_instance_id: str
    parent_ref: str
    parent_scope_name: str | None
    parent_scope_instance_id: str | None
    state: RetainedOutputState
    binding: str | None = None
    output_world_oid: str | None = None
    handoff_ref: str | None = None
    parent_basis_world_oid: str | None = None
    store_id: str | None = None
    resource_id: str | None = None
    candidate_id: str | None = None
    candidate_ref: str | None = None
    candidate_head: str | None = None
    changed_paths: tuple[str, ...] = ()
    settlement_ref: str | None = None
    settlement: RetainedOutputSettlement | None = None
    invalid_reason: str | None = None


DRIVER_INGRESS_RESULT_VALUE_SCHEMA = "vcscore/driver-ingress-result/v1"


# A workspace file change: (path, content) or (path, content, git_filemode).
# The third element is optional; when absent, mode defaults to 100644.
WorkspaceChange = tuple[str, bytes | None] | tuple[str, bytes | None, int]


@dataclass(frozen=True)
class FileState:
    """Normalized content plus Git filemode for one regular workspace file."""

    content: bytes
    mode: int = 0o100644

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", normalize_git_filemode(self.mode))

    def to_workspace_change(self, path: str) -> WorkspaceChange:
        if self.mode == 0o100644:
            return (path, self.content)
        return (path, self.content, self.mode)


def normalize_git_filemode(mode: object) -> int:
    """Normalize supported regular-file Git modes to pygit2 filemode integers."""
    if isinstance(mode, bool):
        raise TypeError("Git filemode must be 100644 or 100755.")
    if isinstance(mode, str):
        text = mode.strip().lower()
        try:
            numeric_mode = int(text, 0)
        except ValueError:
            try:
                numeric_mode = int(text)
            except ValueError as exc:
                raise ValueError("Git filemode must be 100644 or 100755.") from exc
    elif isinstance(mode, int):
        numeric_mode = mode
    else:
        raise TypeError("Git filemode must be 100644 or 100755.")

    if numeric_mode in {0o100644, 100644}:
        return 0o100644
    if numeric_mode in {0o100755, 100755}:
        return 0o100755
    raise ValueError("Git filemode must be 100644 or 100755.")


def posix_to_git_mode(st_mode: int) -> int:
    """Map POSIX st_mode to Git filemode (100644 or 100755)."""
    import stat

    if stat.S_IMODE(st_mode) & 0o111:
        return 0o100755
    return 0o100644


@dataclass(frozen=True)
class EffectRecord:
    """An effect descriptor produced by substrate execution.

    Substrates return EffectRecords; the RecordingPipeline records them
    as C1 commits via Store._emit_effect(). Substrates never call
    Store._emit_effect() directly.
    """

    effect_type: str
    metadata: dict[str, Any]
    workspace_changes: tuple[WorkspaceChange, ...] = ()
    supersedes: tuple[str, ...] = ()  # reserved for R2 translation


def normalize_command_value(value: object) -> object:
    """Convert a command result into a deterministic JSON-safe shape."""
    try:
        from vcs_core._substrate_driver import DriverIngressResult

        if isinstance(value, DriverIngressResult):
            return _normalize_driver_ingress_result(value)
        return encode_typed_json(value)
    except TypeError as exc:
        message = str(exc)
        if "NaN or infinity" in message:
            raise TypeError("Command values must not contain NaN or infinity.") from exc
        if "object keys must be strings" in message:
            raise TypeError("Command result dict keys must be strings for JSON transport.") from exc
        raise TypeError(f"Unsupported command result type for transport: {type(value).__name__}") from exc


def normalize_recorded_command_outcome(outcome: RecordedCommandOutcome) -> dict[str, object]:
    """Convert a recorded command outcome into the session/raw-exec transport shape."""
    payload: dict[str, object] = {"oids": list(outcome.oids)}
    if outcome.value is not None:
        payload["value"] = normalize_command_value(outcome.value)
    return payload


def _normalize_driver_ingress_result(result: DriverIngressResult) -> dict[str, object]:
    from vcs_core._substrate_driver import DriverIngressResult

    if not isinstance(result, DriverIngressResult):  # pragma: no cover - guarded by caller
        raise TypeError(f"Expected DriverIngressResult, got {type(result).__name__}.")
    return {
        "schema": DRIVER_INGRESS_RESULT_VALUE_SCHEMA,
        "summary": {
            "observation_count": len(result.observations),
            "transition_count": len(result.transitions),
            "effect_count": len(result.effects),
            "retention_hint_count": len(result.retention_hints),
            "selection_requirement_count": len(result.selection_requirements),
            "diagnostic_count": len(result.diagnostics),
        },
        "observations": [_normalize_observation_draft(draft) for draft in result.observations],
        "transitions": [_normalize_transition_draft(draft) for draft in result.transitions],
        "effects": [_normalize_effect_record(effect) for effect in result.effects],
        "value": normalize_command_value(result.value) if result.value is not None else None,
        "retention_hints": [_normalize_retention_hint(hint) for hint in result.retention_hints],
        "selection_requirements": [
            _normalize_selection_requirement(requirement) for requirement in result.selection_requirements
        ],
        "diagnostics": [_normalize_diagnostic(diagnostic) for diagnostic in result.diagnostics],
    }


def _normalize_effect_record(effect: EffectRecord) -> dict[str, object]:
    return {
        "effect_type": effect.effect_type,
        "metadata": encode_typed_json(effect.metadata),
        "workspace_changes": encode_typed_json(effect.workspace_changes),
        "supersedes": list(effect.supersedes),
    }


def _normalize_observation_draft(draft: ObservationDraft) -> dict[str, object]:
    payload: dict[str, object] = {
        "observation_id": draft.observation_id,
        "evidence_kind": draft.evidence_kind,
        "stable_observation": encode_typed_json(draft.stable_observation),
        "observed_head": draft.observed_head,
        "observed_at_unix_ns": draft.observed_at_unix_ns,
        "mechanism": draft.mechanism,
        "correlation_id": draft.correlation_id,
        "metadata": encode_typed_json(draft.metadata),
    }
    claim = draft.evidence_payload_descriptor_claim
    if claim is not None:
        payload["evidence_payload_descriptor_claim"] = _normalize_payload_descriptor_claim(claim)
    return payload


def _normalize_transition_draft(draft: TransitionDraft) -> dict[str, object]:
    claim = draft.payload_descriptor_claim
    payload: dict[str, object] = {
        "transition_id": draft.transition_id,
        "semantic_op": draft.semantic_op,
        "payload": encode_typed_json(draft.payload),
        "observation_ids": list(draft.observation_ids),
        "evidence_citation_ids": list(draft.evidence_citation_ids),
        "base_heads": list(draft.base_heads),
        "payload_descriptor_claim": _normalize_payload_descriptor_claim(claim) if claim is not None else None,
        "materialization_class": draft.materialization_class,
        "relationship_requirements": [
            encode_typed_json(requirement.to_json()) for requirement in draft.relationship_requirements
        ],
        "metadata": encode_typed_json(draft.metadata),
        "git_tree_oid": draft.git_tree_oid,
    }
    return payload


def _normalize_retention_hint(hint: RetentionHint) -> dict[str, object]:
    return {
        "kind": hint.kind,
        "target": hint.target,
        "digest": hint.digest,
        "mandatory": hint.mandatory,
        "metadata": encode_typed_json(hint.metadata),
    }


def _normalize_selection_requirement(requirement: DriverSelectionRequirementDraft) -> dict[str, object]:
    return {
        "binding": requirement.binding,
        "role": requirement.role,
        "selection_kind": requirement.selection_kind,
        "transition_id": requirement.transition_id,
        "retention_hints": [_normalize_retention_hint(hint) for hint in requirement.retention_hints],
        "metadata": encode_typed_json(requirement.metadata),
    }


def _normalize_diagnostic(diagnostic: Diagnostic) -> object:

    return {
        "code": diagnostic.code,
        "message": diagnostic.message,
        "subject": diagnostic.subject,
        "detail": encode_typed_json(dict(diagnostic.detail)),
    }


def _normalize_payload_descriptor_claim(claim: PayloadDescriptorClaim) -> dict[str, object]:
    return {
        "codec_id": claim.codec_id,
        "codec_version": claim.codec_version,
        "authority_mode": claim.authority_mode,
        "payload_digest": claim.payload_digest,
        "canonical_manifest": encode_typed_json(claim.canonical_manifest),
        "payload_ref": claim.payload_ref,
    }


@dataclass(frozen=True)
class SubstrateContext:
    """Everything a substrate receives at instantiation time.

    Public SPI v0 intentionally limits this context to workspace-local
    construction inputs. Coordinator-owned runtime services are not
    passed through the public substrate authoring path.
    """

    workspace: Path
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BoundSubstrate:
    """A configured binding name paired with its substrate instance."""

    binding_name: str
    substrate_type: str
    instance: Any
    config: dict[str, Any] = field(default_factory=dict)
