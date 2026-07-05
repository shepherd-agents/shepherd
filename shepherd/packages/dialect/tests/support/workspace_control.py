from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shepherd2.schemas.run_outputs import (
    RunOutputDescriptor,
    RunOutputDescriptorLocator,
    RunOutputIdentity,
    RunOutputOwner,
    RunOutputRef,
    run_output_id_for,
)

from shepherd_dialect.workspace_control.run_outputs import RunOutput


@dataclass(frozen=True)
class RunRecordStub:
    run_ref: str
    status: str
    # Lane C: real RunRecords always carry the field (possibly None);
    # WorkspaceRun._per_binding_roots reads it on the changeset path.
    authority_context: Any = None

    def to_json(self) -> dict[str, str]:
        return {"run_ref": self.run_ref, "status": self.status}


@dataclass
class FakeOutputVcsCore:
    handoff_calls: list[object] = field(default_factory=list)
    file_reads: list[tuple[str, str]] = field(default_factory=list)

    def retained_workspace_handoff(self, handle: object) -> object:
        self.handoff_calls.append(handle)
        return object()

    def read_retained_workspace_file(self, scope_name: str, path: str) -> tuple[bytes, int]:
        self.file_reads.append((scope_name, path))
        return (f"read:{path}\n".encode(), 0o100644)


@dataclass
class FakeRuns:
    output_refs: tuple[RunOutputRef, ...]
    shown: dict[str, RunRecordStub]
    workspace: FakeWorkspace | None = None

    def outputs(
        self,
        *,
        run_ref: str | None = None,
        binding: str | None = None,
        **_: object,
    ) -> tuple[RunOutput, ...]:
        assert self.workspace is not None
        return tuple(
            RunOutput(self.workspace, ref)
            for ref in self.output_refs
            if (run_ref is None or ref.owner.run_id == run_ref) and (binding is None or ref.identity.binding == binding)
        )

    def show(self, run_ref: str) -> RunRecordStub | None:
        return self.shown.get(run_ref)


@dataclass
class FakeWorkspace:
    runs: FakeRuns
    mg: FakeOutputVcsCore = field(default_factory=FakeOutputVcsCore)
    trace_store_path: Path = Path(":memory:")
    workspace_path: Path = Path()
    settlements: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.runs.workspace = self

    def select(self, output: RunOutput) -> tuple[str, str]:
        self.settlements.append(("select", output.output_id))
        return self.settlements[-1]

    def release(self, output: RunOutput) -> tuple[str, str]:
        self.settlements.append(("release", output.output_id))
        return self.settlements[-1]

    def discard(self, output: RunOutput) -> tuple[str, str]:
        self.settlements.append(("discard", output.output_id))
        return self.settlements[-1]


def run_output_ref(
    *,
    state: str = "unconsumed",
    run_id: str = "run-1",
    owner: RunOutputOwner | None = None,
    materialization_kind: str = "tree",
    settlement_ref: str | None = None,
) -> RunOutputRef:
    identity = run_output_identity()
    owner = owner or RunOutputOwner(kind="run", run_id=run_id, execution_id="exec-1", frontier_id="frontier-1")
    descriptor_locator = (
        RunOutputDescriptorLocator(
            execution_id="exec-1",
            output_name="workspace",
            frontier_id="frontier-1",
            descriptor_fact_id="fact-1",
        )
        if owner.kind == "run"
        else None
    )
    return RunOutputRef(
        identity=identity,
        owner=owner,
        descriptor=RunOutputDescriptor(
            output_name="workspace",
            world_binding="workspace",
            store_id="store-workspace",
            resource_id="workspace",
            materialization_kind=materialization_kind,  # type: ignore[arg-type]
        ),
        state=state,  # type: ignore[arg-type]
        parent_basis_world_oid="world-in",
        candidate_ref="refs/vcscore/candidates/1",
        store_id="store-workspace",
        resource_id="workspace",
        changed_paths=("candidate.txt",),
        settlement_ref=settlement_ref,
        descriptor_locator=descriptor_locator,
    )


def run_output_identity() -> RunOutputIdentity:
    values: dict[str, Any] = {
        "output_name": "workspace",
        "binding": "workspace",
        "parent_ref": "refs/vcscore/ground",
        "scope_ref": "refs/vcscore/scopes/child",
        "scope_instance_id": "scope-instance-1",
        "candidate_id": "candidate-1",
        "candidate_head": "candidate-head-1",
        "handoff_ref": "handoff-1",
        "output_world_oid": "world-out",
    }
    return RunOutputIdentity(
        output_id=run_output_id_for(**values),
        parent_scope_name="ground",
        parent_scope_instance_id=None,
        scope_name="child",
        **values,
    )
