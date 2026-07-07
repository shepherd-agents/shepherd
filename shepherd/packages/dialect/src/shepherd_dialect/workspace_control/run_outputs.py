"""Public RunOutput wrapper for workspace-control retained outputs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from shepherd_dialect.workspace_control.errors import WorkspaceControlError

if TYPE_CHECKING:
    from shepherd2.schemas.run_outputs import RunOutputRef
    from shepherd_runtime.nucleus import GitRepo
    from vcs_core.types import RetainedOutputSelectionResult, RetainedOutputSettlementResult

    from shepherd_dialect.workspace_control.authority_read_model import (
        RunAuthority,
        RunOutputSettlementEvidence,
        RunOutputSettlementPolicy,
    )
    from shepherd_dialect.workspace_control.changesets import Changeset
    from shepherd_dialect.workspace_control.schemas import RunRecord
    from shepherd_dialect.workspace_control.workspace import ShepherdWorkspace

JsonObject = dict[str, object]


@dataclass(frozen=True, eq=False)
class RunOutput:
    """Thin public wrapper around one resolved run-output query value.

    The wrapper carries workspace context for refresh and settlement delegation.
    It does not own custody state; the wrapped ref is a snapshot from a resolved
    run-output query.
    """

    _workspace: ShepherdWorkspace = field(repr=False, compare=False)
    _ref: RunOutputRef = field(repr=False)

    @property
    def ref(self) -> RunOutputRef:
        """Return the immutable resolved query value for advanced callers."""
        return self._ref

    @property
    def identity(self) -> Any:
        return self._ref.identity

    @property
    def owner(self) -> Any:
        return self._ref.owner

    @property
    def descriptor(self) -> Any:
        return self._ref.descriptor

    @property
    def state(self) -> str:
        return self._ref.state

    @property
    def parent_basis_world_oid(self) -> str:
        return self._ref.parent_basis_world_oid

    @property
    def candidate_ref(self) -> str:
        return self._ref.candidate_ref

    @property
    def store_id(self) -> str:
        return self._ref.store_id

    @property
    def resource_id(self) -> str:
        return self._ref.resource_id

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return self._ref.changed_paths

    @property
    def settlement_ref(self) -> str | None:
        return self._ref.settlement_ref

    @property
    def invalid_reason(self) -> str | None:
        return self._ref.invalid_reason

    @property
    def descriptor_locator(self) -> Any:
        return self._ref.descriptor_locator

    @property
    def output_id(self) -> str:
        return self.identity.output_id

    @property
    def output_name(self) -> str:
        return self.identity.output_name

    @property
    def binding(self) -> str:
        return self.identity.binding

    @property
    def output_world_oid(self) -> str:
        return self.identity.output_world_oid

    def refresh(self) -> RunOutput:
        """Re-resolve this output through the owning workspace."""
        owner = self.owner
        if owner.kind != "run" or owner.run_id is None:
            raise WorkspaceControlError("only run-owned outputs can be refreshed through workspace runs")
        for output in self._workspace.runs.outputs(run_ref=owner.run_id, binding=self.binding):
            if output.output_id == self.output_id:
                return output
        raise WorkspaceControlError(f"run output {self.output_id!r} is no longer visible")

    def inspect(self) -> JsonObject:
        """Return a JSON-shaped, custody-refreshed output snapshot.

        This is a read/query operation. It re-resolves the output through the
        owning workspace so current custody state comes from vcs-core rather
        than this wrapper's immutable snapshot.
        """
        return self._validated_current_output("inspection").to_json()

    def read_file(self, path: str) -> tuple[bytes, int] | None:
        """Read a file from this retained workspace output without selecting it."""
        _validate_output_relative_path(path, field_name="run-output file path")
        if self.owner.kind != "run" or self.owner.run_id is None:
            raise WorkspaceControlError("run-output file reads require a run-owned output")
        if self.binding != "workspace":
            raise WorkspaceControlError("run-output file reads currently require workspace binding")
        if self.state == "invalid":
            raise WorkspaceControlError("run-output file reads require valid retained custody")
        from shepherd_dialect.workspace_control.retained_outputs import (
            _validate_retained_run_output_read_handle,
        )

        _validate_retained_run_output_read_handle(self._workspace, self._ref)
        reader = getattr(self._workspace.mg, "read_retained_workspace_file", None)
        if not callable(reader):
            raise WorkspaceControlError("VcsCore.read_retained_workspace_file is required for run-output file reads")
        return reader(self.identity.scope_name, path)

    def read_text(self, path: str, *, encoding: str = "utf-8") -> str:
        """Read a retained output file as text, failing closed if it is missing."""
        data = self.read_file(path)
        if data is None:
            raise WorkspaceControlError(f"run output file {path!r} is not present")
        return data[0].decode(encoding)

    def read_json(self, path: str, *, encoding: str = "utf-8") -> object:
        """Read a retained output file as JSON, failing closed if it is missing or malformed."""
        return json.loads(self.read_text(path, encoding=encoding))

    def artifact(self, path: str) -> Any:
        """Return a path-specific artifact view over this retained output."""
        from shepherd_dialect.workspace_control.input_refs import RunOutputArtifact

        return RunOutputArtifact(self, path)

    def changeset(self) -> Changeset:
        """Return a read-only view of this output's candidate workspace delta."""
        from shepherd_dialect.workspace_control.changesets import Changeset

        return Changeset(self)

    def run_authority(self) -> RunAuthority:
        """Return the validated producer-run authority view for this output."""
        from shepherd_dialect.workspace_control.authority_read_model import run_authority_from_record

        current = self._validated_current_output("authority inspection")
        return run_authority_from_record(current._owning_run_record())

    def settlement_policy(self) -> RunOutputSettlementPolicy:
        """Return a read-only view of settlement policy and current custody state."""
        from shepherd_dialect.workspace_control.authority_read_model import run_output_settlement_policy_from_record

        current = self._validated_current_output("settlement-policy inspection")
        return run_output_settlement_policy_from_record(current.ref, current._owning_run_record())

    def settlement_evidence(self) -> RunOutputSettlementEvidence:
        """Return joined settlement receipt and authority-monitor evidence for this output."""
        from shepherd_dialect.workspace_control.authority_read_model import run_output_settlement_evidence_from_record

        current = self._validated_current_output("settlement-evidence inspection")
        retained_row = current._retained_output_row()
        authority_settlement = _authority_settlement_metadata(
            current._workspace.mg,
            getattr(retained_row, "settlement", None),
        )
        return run_output_settlement_evidence_from_record(
            current.ref,
            current._owning_run_record(),
            retained_row=retained_row,
            authority_settlement=authority_settlement,
        )

    def as_readonly_git_repo(self) -> GitRepo:
        """Return a read-only GitRepo value view over this retained workspace output."""
        if self.owner.kind != "run" or self.owner.run_id is None:
            raise WorkspaceControlError("GitRepo hydration requires a run-owned output")
        if self.binding != "workspace":
            raise WorkspaceControlError("GitRepo hydration currently requires workspace binding")
        if self.descriptor.materialization_kind != "tree":
            raise WorkspaceControlError("GitRepo hydration currently requires a tree materialization")
        if self.state == "invalid":
            raise WorkspaceControlError("GitRepo hydration requires valid retained custody")
        from shepherd_dialect.workspace_control.gitrepo_handles import readonly_git_repo_for_retained_output
        from shepherd_dialect.workspace_control.retained_outputs import (
            _validate_retained_run_output_read_handle,
        )

        _validate_retained_run_output_read_handle(self._workspace, self._ref)
        return readonly_git_repo_for_retained_output(self)

    def select(self) -> RetainedOutputSelectionResult:
        """Select this output through the owning workspace."""
        return self._workspace.select(self)

    def apply(self) -> RetainedOutputSettlementResult:
        """Apply this output onto the (possibly advanced) parent through the owning workspace.

        Three-way whole-output settlement: succeeds only when this run's delta and the
        parent's changes since the fork basis are path-disjoint; fails closed on overlap.
        """
        return self._workspace.apply(self)

    def release(self) -> RetainedOutputSettlementResult:
        """Release this output through the owning workspace."""
        return self._workspace.release(self)

    def discard(self) -> RetainedOutputSettlementResult:
        """Discard this output through the owning workspace."""
        return self._workspace.discard(self)

    def to_json(self) -> JsonObject:
        """Return a JSON-shaped snapshot of the wrapped output."""
        locator = self.descriptor_locator
        return {
            "identity": asdict(self.identity),
            "owner": asdict(self.owner),
            "descriptor": asdict(self.descriptor),
            "state": self.state,
            "parent_basis_world_oid": self.parent_basis_world_oid,
            "candidate_ref": self.candidate_ref,
            "store_id": self.store_id,
            "resource_id": self.resource_id,
            "changed_paths": list(self.changed_paths),
            "settlement_ref": self.settlement_ref,
            "invalid_reason": self.invalid_reason,
            "descriptor_locator": None if locator is None else asdict(locator),
        }

    def _owning_run_record(self) -> RunRecord:
        if self.owner.kind != "run" or self.owner.run_id is None:
            raise WorkspaceControlError("run-output authority inspection requires a run-owned output")
        record = self._workspace.runs.show(self.owner.run_id)
        if record is None:
            raise WorkspaceControlError(f"run-output authority cannot resolve run {self.owner.run_id!r}")
        return record

    def _retained_output_row(self) -> Any:
        from vcs_core.types import RetainedOutputIdentity

        reader = getattr(self._workspace.mg, "get_retained_output", None)
        if not callable(reader):
            raise WorkspaceControlError("VcsCore.get_retained_output is required for run-output custody inspection")
        identity = RetainedOutputIdentity(
            scope_name=self.identity.scope_name,
            scope_ref=self.identity.scope_ref,
            scope_instance_id=self.identity.scope_instance_id,
            parent_ref=self.identity.parent_ref,
            parent_scope_name=self.identity.parent_scope_name,
            parent_scope_instance_id=self.identity.parent_scope_instance_id,
            binding=self.binding,
            output_world_oid=self.identity.output_world_oid,
            handoff_ref=self.identity.handoff_ref,
            parent_basis_world_oid=self.parent_basis_world_oid,
            store_id=self.store_id,
            resource_id=self.resource_id,
            candidate_id=self.identity.candidate_id,
            candidate_ref=self.candidate_ref,
            candidate_head=self.identity.candidate_head,
        )
        row = reader(identity)
        if row is None:
            raise WorkspaceControlError(f"run output {self.output_id!r} no longer has retained custody")
        return row

    def _validated_current_output(self, operation: str) -> RunOutput:
        if self.owner.kind != "run" or self.owner.run_id is None:
            raise WorkspaceControlError(f"run-output {operation} requires a run-owned output")
        from shepherd_dialect.workspace_control.retained_outputs import (
            _validate_retained_run_output_read_handle,
        )

        _validate_retained_run_output_read_handle(self._workspace, self._ref)
        return self.refresh()


def _authority_settlement_metadata(mg: Any, settlement: Any) -> JsonObject | None:
    if settlement is None:
        return None
    settlement_operation_id = getattr(settlement, "authority_settlement_operation_id", None)
    if settlement_operation_id is None:
        return None
    if not isinstance(settlement_operation_id, str) or not settlement_operation_id:
        raise WorkspaceControlError("run-output settlement receipt has malformed authority settlement id")
    resolver = getattr(mg, "resolve_operation_history", None)
    if not callable(resolver):
        raise WorkspaceControlError("VcsCore.resolve_operation_history is required for settlement evidence inspection")
    try:
        history = resolver(settlement_operation_id)
    except Exception as exc:
        raise WorkspaceControlError(
            f"run-output settlement authority history {settlement_operation_id!r} is not resolvable"
        ) from exc
    for commit in getattr(history, "commits", ()):
        metadata = getattr(commit, "metadata", None)
        if not isinstance(metadata, dict):
            continue
        if metadata.get("type") != "RetainedOutputAuthoritySettlement":
            continue
        authority_operation_id = getattr(settlement, "authority_operation_id", None)
        if isinstance(authority_operation_id, str) and metadata.get("authority_operation_id") != authority_operation_id:
            raise WorkspaceControlError("run-output settlement authority operation id disagrees with receipt")
        outcome = getattr(settlement, "authority_outcome", None)
        if isinstance(outcome, str) and metadata.get("outcome") != outcome:
            raise WorkspaceControlError("run-output settlement authority outcome disagrees with receipt")
        return dict(metadata)
    raise WorkspaceControlError(
        f"run-output settlement authority history {settlement_operation_id!r} has no settlement"
    )


def _validate_output_relative_path(path: str, *, field_name: str) -> None:
    if not isinstance(path, str):
        raise WorkspaceControlError(f"{field_name} must be a relative POSIX path")
    parsed = PurePosixPath(path)
    if path in {"", ".", ".."} or parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise WorkspaceControlError(f"{field_name} must be a relative POSIX path")
