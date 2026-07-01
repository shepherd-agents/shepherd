"""Shared runtime identity dataclasses."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

RUN_REF_SCHEMA = "shepherd.runtime.run_ref.v1"

__all__ = [
    "RUN_REF_SCHEMA",
    "VcsCoreExecutionLink",
    "RunRef",
    "run_ref_from_json",
    "run_ref_to_json",
]


@dataclass(frozen=True)
class VcsCoreExecutionLink:
    """Identity bridge from a Shepherd ``RunRef`` to its vcs-core execution.

    Pure data — no behavior, no vcs-core imports. Colocated with ``RunRef`` (per
    Phase 0a decision D-0a-1) so the one-way package dependency
    (``shepherd_runtime`` does not import the ``shepherd`` meta-package) is
    preserved. Populated by the canonical execution provider once a vcs-core run
    opens; ``None`` on ``RunRef`` until then. Shape per ``v1-integration.md`` §3.4.
    """

    workspace_root: str
    vcscore_repo: str
    parent_ref: str | None
    child_scope_ref: str
    # Durable run identity = the vcs-core world commit. ``merge()`` already
    # publishes a world commit and drives the operation-journal lifecycle, so the
    # runtime substrate cites the world-commit OIDs it produced rather than a
    # hand-driven journal entry (decision D-0a-4). ``output_world_oid`` is the
    # post-run ground world commit (the run's citable state identity);
    # ``input_world_oid`` is the pre-run ground world commit (the rewind handle).
    input_world_oid: str | None
    output_world_oid: str | None
    trace_revision_ref: str | None
    carrier_ref: str | None
    input_head: str | None  # pre-run workspace substrate head
    output_head: str | None  # post-run workspace substrate head (on success)
    terminal_status: str  # "merged" | "discarded" | "failed"


@dataclass(frozen=True)
class RunRef:
    """Durable identity of one Run.

    Process-local in the syntax nucleus; durable across processes once the
    durable-runs lane lands. The string ``id`` is the externally visible
    identifier; downstream code should treat ``RunRef`` as opaque. ``vcscore``
    is populated once a vcs-core-backed run opens (``None`` otherwise), so
    existing ``RunRef(id=...)`` construction stays valid.
    """

    id: str
    vcscore: VcsCoreExecutionLink | None = None

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.id, "RunRef id")
        if self.id == "@latest":
            raise ValueError("RunRef id must be an exact run id, not selector '@latest'")
        if self.vcscore is not None and not isinstance(self.vcscore, VcsCoreExecutionLink):
            raise TypeError("RunRef vcscore must be a VcsCoreExecutionLink or None")

    def __str__(self) -> str:
        return self.id

    def to_payload(self) -> dict[str, object]:
        """Return a JSON-shaped representation of this run identity."""
        return _run_ref_mapping(self, include_schema=True, omit_none_vcscore=False)

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> RunRef:
        """Rehydrate a run identity from its JSON-shaped representation."""
        return _run_ref_from_mapping(payload, require_schema=True)


def run_ref_to_json(run_ref: RunRef) -> dict[str, object]:
    """Return the legacy trace-JSON shape for a run identity.

    Trace JSON predates schema-shaped identity payloads, so ordinary run refs
    stay serialized as ``{"id": "..."}``. Vcs-core-backed run refs retain the
    nested execution link explicitly instead of relying on raw dataclass serde.
    """
    if not isinstance(run_ref, RunRef):
        raise TypeError("run_ref_to_json requires a RunRef")
    return _run_ref_mapping(run_ref, include_schema=False, omit_none_vcscore=True)


def run_ref_from_json(payload: Mapping[str, object]) -> RunRef:
    """Rehydrate a run identity from trace JSON or a schema-shaped payload."""
    return _run_ref_from_mapping(payload, require_schema=False)


def _run_ref_mapping(
    run_ref: RunRef,
    *,
    include_schema: bool,
    omit_none_vcscore: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {"id": run_ref.id}
    if include_schema:
        payload["schema"] = RUN_REF_SCHEMA
    if run_ref.vcscore is not None:
        payload["vcscore"] = asdict(run_ref.vcscore)
    elif not omit_none_vcscore:
        payload["vcscore"] = None
    return payload


def _run_ref_from_mapping(payload: Mapping[str, object], *, require_schema: bool) -> RunRef:
    if not isinstance(payload, Mapping):
        raise TypeError("RunRef payload must be an object")
    raw_schema = payload.get("schema")
    if require_schema and raw_schema != RUN_REF_SCHEMA:
        raise ValueError("RunRef payload schema is unsupported")
    if raw_schema is not None and raw_schema != RUN_REF_SCHEMA:
        raise ValueError("RunRef payload schema is unsupported")
    raw_id = payload.get("id")
    if not isinstance(raw_id, str):
        raise TypeError("RunRef payload id must be a string")
    raw_vcscore = payload.get("vcscore")
    if raw_vcscore is None:
        vcscore = None
    elif isinstance(raw_vcscore, Mapping):
        vcscore = _vcscore_execution_link_from_payload(raw_vcscore)
    else:
        raise TypeError("RunRef payload vcscore must be an object or null")
    return RunRef(id=raw_id, vcscore=vcscore)


def _vcscore_execution_link_from_payload(payload: Mapping[str, object]) -> VcsCoreExecutionLink:
    required = (
        "workspace_root",
        "vcscore_repo",
        "child_scope_ref",
        "terminal_status",
    )
    for field_name in required:
        value = payload.get(field_name)
        if not isinstance(value, str) or not value:
            raise TypeError(f"RunRef vcscore payload {field_name} must be a non-empty string")
    return VcsCoreExecutionLink(
        workspace_root=str(payload["workspace_root"]),
        vcscore_repo=str(payload["vcscore_repo"]),
        parent_ref=_optional_str(payload.get("parent_ref"), "parent_ref"),
        child_scope_ref=str(payload["child_scope_ref"]),
        input_world_oid=_optional_str(payload.get("input_world_oid"), "input_world_oid"),
        output_world_oid=_optional_str(payload.get("output_world_oid"), "output_world_oid"),
        trace_revision_ref=_optional_str(payload.get("trace_revision_ref"), "trace_revision_ref"),
        carrier_ref=_optional_str(payload.get("carrier_ref"), "carrier_ref"),
        input_head=_optional_str(payload.get("input_head"), "input_head"),
        output_head=_optional_str(payload.get("output_head"), "output_head"),
        terminal_status=str(payload["terminal_status"]),
    )


def _optional_str(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise TypeError(f"RunRef vcscore payload {field_name} must be a non-empty string or null")
    return value


def _validate_non_empty_str(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
