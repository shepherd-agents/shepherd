"""Thin public run wrapper for the first workspace-control facade."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from shepherd_runtime.identities import RunRef

from shepherd_dialect.workspace_control.errors import WorkspaceControlError

if TYPE_CHECKING:
    from shepherd_dialect.workspace_control.authority_read_model import RunAuthority
    from shepherd_dialect.workspace_control.changesets import Changeset
    from shepherd_dialect.workspace_control.run_outputs import RunOutput
    from shepherd_dialect.workspace_control.schemas import RunRecord
    from shepherd_dialect.workspace_control.workspace import ShepherdWorkspace


@dataclass(frozen=True, eq=False)
class WorkspaceRun:
    """Resolved run plus live output views for the handle-in workspace facade.

    This wrapper does not own run state. The run ledger remains the authority
    for the record, and retained-output custody remains the authority for
    output state.
    """

    _workspace: ShepherdWorkspace = field(repr=False, compare=False)
    record: RunRecord

    @property
    def run_ref(self) -> str:
        return self.record.run_ref

    @property
    def ref(self) -> RunRef:
        """Return this run's typed public identity value."""
        return RunRef(id=self.record.run_ref)

    @property
    def status(self) -> str:
        return self.record.status

    @property
    def outputs(self) -> dict[str, RunOutput]:
        """Return current output wrappers keyed by output name."""
        return {output.output_name: output for output in self._workspace.runs.outputs(run_ref=self.run_ref)}

    def output(self, name: str = "workspace") -> RunOutput:
        """Return one current output wrapper by output name."""
        try:
            return self.outputs[name]
        except KeyError as exc:
            raise WorkspaceControlError(f"run {self.run_ref!r} has no output named {name!r}") from exc

    def changeset(self, name: str = "workspace") -> Changeset:
        """Return the read-only changeset view for one run output, or one bound sub-root.

        ``name`` is an output name (``"workspace"``, the whole delta) or — for a Lane C
        multi-binding run — a bound binding name, which returns the whole-delta changeset narrowed
        to that binding's root prefix (a free prefix-filter VIEW; custody is unchanged). An unknown
        name fails closed.
        """
        per_binding = self._per_binding_roots()
        if name in per_binding:
            return self.output("workspace").changeset().narrowed_to_binding(name=name, root=per_binding[name])
        return self.output(name).changeset()

    def _per_binding_roots(self) -> dict[str, str]:
        """Return the run's recorded ``binding name -> workspace-relative sub-root`` map (Lane C)."""
        from collections.abc import Mapping

        context = self.record.authority_context
        per_binding = None if context is None else context.per_binding_authority
        if not isinstance(per_binding, Mapping):
            return {}
        roots: dict[str, str] = {}
        for name, entry in per_binding.items():
            if isinstance(entry, Mapping) and isinstance(entry.get("root"), str):
                roots[str(name)] = str(entry["root"])
        return roots

    def authority(self) -> RunAuthority:
        """Return the validated persisted authority view for this run."""
        from shepherd_dialect.workspace_control.authority_read_model import run_authority_from_record

        return run_authority_from_record(self.record)

    def refresh(self) -> WorkspaceRun:
        """Re-read the run record through the owning workspace."""
        record = self._workspace.runs.show(self.run_ref)
        if record is None:
            raise WorkspaceControlError(f"run {self.run_ref!r} is no longer visible")
        return WorkspaceRun(self._workspace, record)

    def to_json(self) -> dict[str, object]:
        """Return a compact JSON-shaped projection."""
        return {
            "run_ref": self.run_ref,
            "status": self.status,
            "record": self.record.to_json(),
            "outputs": {name: output.to_json() for name, output in self.outputs.items()},
        }
