"""RunOutput identity and query value shapes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from ..kernel.facts import Fact, FactDraft, ReadContext, TraceOwnerId, TraceSlice, TraceStore

RunOutputSettlementAction = Literal["selected", "applied", "released", "discarded"]
RunOutputState = Literal["unconsumed", "selected", "applied", "released", "discarded", "invalid"]
RunOutputOwnerKind = Literal["run", "retained-query"]
RunOutputMaterializationKind = Literal["tree", "external"]
RUN_OUTPUT_SCHEMA = "shepherd2.run_output.v1"
RUN_OUTPUT_DESCRIPTOR_SCHEMA = "shepherd2.skeleton.run_output_descriptor.v1"
RUN_OUTPUT_DESCRIPTOR_LOCATOR_SCHEMA = "shepherd2.skeleton.run_output_descriptor_locator.v1"
_RUN_OUTPUT_SETTLEMENT_ACTIONS = frozenset({"selected", "applied", "released", "discarded"})
_RUN_OUTPUT_STATES = frozenset({"unconsumed", *_RUN_OUTPUT_SETTLEMENT_ACTIONS, "invalid"})
_RUN_OUTPUT_OWNER_KINDS = frozenset({"run", "retained-query"})
_RUN_OUTPUT_MATERIALIZATION_KINDS = frozenset({"tree", "external"})


@dataclass(frozen=True)
class RunOutputOwner:
    """Owner citation for a retained output settlement right."""

    kind: RunOutputOwnerKind
    run_id: str | None = None
    execution_id: str | None = None
    frontier_id: str | None = None

    def __post_init__(self) -> None:
        _require_member(self.kind, _RUN_OUTPUT_OWNER_KINDS, "owner kind")
        if self.kind == "run":
            _require_non_empty_str(self.run_id, "owner run_id")
            _require_non_empty_str(self.execution_id, "owner execution_id")
            _require_non_empty_str(self.frontier_id, "owner frontier_id")
            return
        if self.run_id is not None or self.execution_id is not None or self.frontier_id is not None:
            raise ValueError("RunOutput retained-query owner must not carry run identity fields")


@dataclass(frozen=True)
class RunOutputDescriptor:
    """Projection metadata for one run output.

    ``output_name`` is the product key exposed by a run. ``world_binding`` is
    the selected-world binding consumed by boundary settlement.
    """

    output_name: str
    world_binding: str
    store_id: str
    resource_id: str
    materialization_kind: RunOutputMaterializationKind

    def __post_init__(self) -> None:
        _require_non_empty_str(self.output_name, "descriptor output_name")
        _require_non_empty_str(self.world_binding, "descriptor world_binding")
        _require_non_empty_str(self.store_id, "descriptor store_id")
        _require_non_empty_str(self.resource_id, "descriptor resource_id")
        _require_member(
            self.materialization_kind,
            _RUN_OUTPUT_MATERIALIZATION_KINDS,
            "descriptor materialization_kind",
        )


@dataclass(frozen=True)
class RunOutputDescriptorLocator:
    """Trace-owned locator for one RunOutput descriptor fact."""

    execution_id: TraceOwnerId
    output_name: str
    frontier_id: str | None
    descriptor_fact_id: str
    schema_ref: str = RUN_OUTPUT_DESCRIPTOR_SCHEMA

    def __post_init__(self) -> None:
        _require_non_empty_str(self.execution_id, "execution_id")
        _require_non_empty_str(self.output_name, "output_name")
        _require_non_empty_str(self.descriptor_fact_id, "descriptor_fact_id")
        _require_non_empty_str(self.schema_ref, "schema_ref")
        if self.frontier_id is not None:
            _require_non_empty_str(self.frontier_id, "frontier_id")
        if self.schema_ref != RUN_OUTPUT_DESCRIPTOR_SCHEMA:
            raise ValueError(
                f"RunOutput descriptor locator schema_ref is unsupported: {self.schema_ref!r} "
                f"(expected {RUN_OUTPUT_DESCRIPTOR_SCHEMA!r}). This store may have been written "
                f"by an incompatible Shepherd version; see durable-state-compatibility (D3)."
            )


def run_output_descriptor_locator_payload(locator: RunOutputDescriptorLocator) -> dict[str, str]:
    """Return the durable JSON-shaped payload for a descriptor locator."""
    if not isinstance(locator, RunOutputDescriptorLocator):
        raise TypeError("RunOutput descriptor locator payload requires a RunOutputDescriptorLocator")
    if locator.frontier_id is None:
        raise ValueError("Durable RunOutput descriptor locator payload requires frontier_id")
    return {
        "schema": RUN_OUTPUT_DESCRIPTOR_LOCATOR_SCHEMA,
        "execution_id": locator.execution_id,
        "output_name": locator.output_name,
        "frontier_id": locator.frontier_id,
        "descriptor_fact_id": locator.descriptor_fact_id,
        "schema_ref": locator.schema_ref,
    }


def run_output_descriptor_locator_from_payload(payload: dict[str, Any]) -> RunOutputDescriptorLocator:
    """Rehydrate a durable RunOutput descriptor locator payload."""
    if payload.get("schema") != RUN_OUTPUT_DESCRIPTOR_LOCATOR_SCHEMA:
        raise ValueError(
            f"RunOutput descriptor locator payload has unsupported schema: "
            f"{payload.get('schema')!r} (expected {RUN_OUTPUT_DESCRIPTOR_LOCATOR_SCHEMA!r}). "
            f"This store may have been written by an incompatible Shepherd version; "
            f"see durable-state-compatibility (D3)."
        )
    return RunOutputDescriptorLocator(
        execution_id=_payload_str(payload, "execution_id"),
        output_name=_payload_str(payload, "output_name"),
        frontier_id=_payload_str(payload, "frontier_id"),
        descriptor_fact_id=_payload_str(payload, "descriptor_fact_id"),
        schema_ref=_payload_str(payload, "schema_ref"),
    )


@dataclass(frozen=True)
class ProjectedRunOutputDescriptor:
    """Projected trace descriptor record for one named run output."""

    locator: RunOutputDescriptorLocator
    citation_payload: dict[str, Any]

    def __post_init__(self) -> None:
        _require_citation_descriptor_agrees(
            self.citation_payload,
            output_name=self.locator.output_name,
            world_binding=_payload_str(self.citation_payload, "binding"),
        )
        _require_locator_matches_payload(self.locator, self.citation_payload)


@dataclass(frozen=True)
class RunOutputIdentity:
    """Stable product identity for one binding output."""

    output_id: str
    output_name: str
    binding: str
    parent_scope_name: str
    parent_ref: str
    parent_scope_instance_id: str | None
    scope_name: str
    scope_ref: str
    scope_instance_id: str
    candidate_id: str
    candidate_head: str
    output_world_oid: str
    handoff_ref: str

    def __post_init__(self) -> None:
        _require_non_empty_str(self.output_id, "identity output_id")
        _require_non_empty_str(self.output_name, "identity output_name")
        _require_non_empty_str(self.binding, "identity binding")
        _require_non_empty_str(self.parent_scope_name, "identity parent_scope_name")
        _require_non_empty_str(self.parent_ref, "identity parent_ref")
        if self.parent_scope_instance_id is not None:
            _require_non_empty_str(self.parent_scope_instance_id, "identity parent_scope_instance_id")
        _require_non_empty_str(self.scope_name, "identity scope_name")
        _require_non_empty_str(self.scope_ref, "identity scope_ref")
        _require_non_empty_str(self.scope_instance_id, "identity scope_instance_id")
        _require_non_empty_str(self.candidate_id, "identity candidate_id")
        _require_non_empty_str(self.candidate_head, "identity candidate_head")
        _require_non_empty_str(self.output_world_oid, "identity output_world_oid")
        _require_non_empty_str(self.handoff_ref, "identity handoff_ref")
        expected = run_output_id_for(
            output_name=self.output_name,
            binding=self.binding,
            parent_ref=self.parent_ref,
            scope_ref=self.scope_ref,
            scope_instance_id=self.scope_instance_id,
            candidate_id=self.candidate_id,
            candidate_head=self.candidate_head,
            handoff_ref=self.handoff_ref,
            output_world_oid=self.output_world_oid,
        )
        if self.output_id != expected:
            raise ValueError("RunOutput identity output_id disagrees with custody tuple")


@dataclass(frozen=True)
class RunOutputCitation:
    """Trace-owned citation for one run output.

    This value says that a run produced an output with the cited custody tuple.
    It deliberately does not carry current retained-output state; liveness and
    settlement remain custody facts.
    """

    identity: RunOutputIdentity
    owner: RunOutputOwner
    descriptor: RunOutputDescriptor
    parent_basis_world_oid: str
    candidate_ref: str
    store_id: str
    resource_id: str
    changed_paths: tuple[str, ...] = ()
    descriptor_locator: RunOutputDescriptorLocator | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.parent_basis_world_oid, "citation parent_basis_world_oid")
        _require_non_empty_str(self.candidate_ref, "citation candidate_ref")
        _require_non_empty_str(self.store_id, "citation store_id")
        _require_non_empty_str(self.resource_id, "citation resource_id")
        _require_changed_paths(self.changed_paths, "citation changed_paths")
        _require_descriptor_matches(
            self.descriptor,
            identity=self.identity,
            store_id=self.store_id,
            resource_id=self.resource_id,
        )
        _require_descriptor_locator_matches(
            self.descriptor_locator,
            identity=self.identity,
            owner=self.owner,
        )


@dataclass(frozen=True)
class RunOutputRef:
    """Immutable query value for one retained run output."""

    identity: RunOutputIdentity
    owner: RunOutputOwner
    descriptor: RunOutputDescriptor
    state: RunOutputState
    parent_basis_world_oid: str
    candidate_ref: str
    store_id: str
    resource_id: str
    changed_paths: tuple[str, ...] = ()
    settlement_ref: str | None = None
    invalid_reason: str | None = None
    descriptor_locator: RunOutputDescriptorLocator | None = None

    def __post_init__(self) -> None:
        _require_member(self.state, _RUN_OUTPUT_STATES, "ref state")
        _require_non_empty_str(self.parent_basis_world_oid, "ref parent_basis_world_oid")
        _require_non_empty_str(self.candidate_ref, "ref candidate_ref")
        _require_non_empty_str(self.store_id, "ref store_id")
        _require_non_empty_str(self.resource_id, "ref resource_id")
        _require_changed_paths(self.changed_paths, "ref changed_paths")
        if self.settlement_ref is not None:
            _require_non_empty_str(self.settlement_ref, "ref settlement_ref")
        if self.state == "invalid":
            _require_non_empty_str(self.invalid_reason, "ref invalid_reason")
        elif self.invalid_reason is not None:
            raise ValueError("RunOutput ref invalid_reason is only valid for invalid state")
        _require_descriptor_matches(
            self.descriptor,
            identity=self.identity,
            store_id=self.store_id,
            resource_id=self.resource_id,
        )
        _require_descriptor_locator_matches(
            self.descriptor_locator,
            identity=self.identity,
            owner=self.owner,
        )


def run_output_id_for(
    *,
    output_name: str | None = None,
    binding: str,
    parent_ref: str,
    scope_ref: str,
    scope_instance_id: str,
    candidate_id: str,
    candidate_head: str,
    handoff_ref: str,
    output_world_oid: str,
) -> str:
    """Return the stable id for one binding output custody tuple."""
    stable_output_name = output_name or binding
    stable_fields = {
        "output_name": stable_output_name,
        "binding": binding,
        "parent_ref": parent_ref,
        "scope_ref": scope_ref,
        "scope_instance_id": scope_instance_id,
        "candidate_id": candidate_id,
        "candidate_head": candidate_head,
        "handoff_ref": handoff_ref,
        "output_world_oid": output_world_oid,
    }
    digest = hashlib.sha256(
        json.dumps(stable_fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"run-output:{digest}"


def run_output_identity_for(
    *,
    output_name: str | None = None,
    binding: str,
    parent_scope_name: str,
    parent_ref: str,
    parent_scope_instance_id: str | None,
    scope_name: str,
    scope_ref: str,
    scope_instance_id: str,
    candidate_id: str,
    candidate_head: str,
    output_world_oid: str,
    handoff_ref: str,
) -> RunOutputIdentity:
    """Build a stable identity for one binding output."""
    stable_output_name = output_name or binding
    return RunOutputIdentity(
        output_id=run_output_id_for(
            output_name=stable_output_name,
            binding=binding,
            parent_ref=parent_ref,
            scope_ref=scope_ref,
            scope_instance_id=scope_instance_id,
            candidate_id=candidate_id,
            candidate_head=candidate_head,
            handoff_ref=handoff_ref,
            output_world_oid=output_world_oid,
        ),
        output_name=stable_output_name,
        binding=binding,
        parent_scope_name=parent_scope_name,
        parent_ref=parent_ref,
        parent_scope_instance_id=parent_scope_instance_id,
        scope_name=scope_name,
        scope_ref=scope_ref,
        scope_instance_id=scope_instance_id,
        candidate_id=candidate_id,
        candidate_head=candidate_head,
        output_world_oid=output_world_oid,
        handoff_ref=handoff_ref,
    )


def _require_descriptor_matches(
    descriptor: RunOutputDescriptor,
    *,
    identity: RunOutputIdentity,
    store_id: str,
    resource_id: str,
) -> None:
    if descriptor.output_name != identity.output_name:
        raise ValueError("RunOutput descriptor output_name disagrees with identity")
    if descriptor.world_binding != identity.binding:
        raise ValueError("RunOutput descriptor world_binding disagrees with identity")
    if descriptor.store_id != store_id:
        raise ValueError("RunOutput descriptor store_id disagrees with citation")
    if descriptor.resource_id != resource_id:
        raise ValueError("RunOutput descriptor resource_id disagrees with citation")


def run_output_descriptor_fact(
    *,
    execution_id: TraceOwnerId,
    output_name: str,
    world_binding: str,
    citation: dict[str, Any],
) -> FactDraft:
    """Create the trace-owned product-output descriptor fact for one RunOutput."""
    _require_non_empty_str(execution_id, "execution_id")
    _require_non_empty_str(output_name, "output_name")
    _require_non_empty_str(world_binding, "world_binding")
    normalized_citation = dict(citation)
    _require_citation_descriptor_agrees(
        normalized_citation,
        output_name=output_name,
        world_binding=world_binding,
    )
    return FactDraft(
        mode="capture",
        schema_ref=RUN_OUTPUT_DESCRIPTOR_SCHEMA,
        kind_label="run_output_descriptor",
        payload={
            "execution_id": execution_id,
            "output_name": output_name,
            "world_binding": world_binding,
            "citation": normalized_citation,
        },
    )


def project_run_output_descriptors(
    trace_slice: TraceSlice,
    execution_id: TraceOwnerId,
    *,
    frontier_id: str | None = None,
) -> dict[str, ProjectedRunOutputDescriptor]:
    """Project output-name descriptor records from one execution's trace path."""
    locator_frontier_id = _locator_frontier_id(trace_slice, execution_id, frontier_id)
    descriptors: dict[str, ProjectedRunOutputDescriptor] = {}
    for fact_id in trace_slice.owner_paths.get(execution_id, ()):
        fact = trace_slice.visible_facts_by_id.get(fact_id)
        if not isinstance(fact, Fact):
            raise TypeError("project_run_output_descriptors requires payload-visible facts")
        if fact.envelope.schema_ref != RUN_OUTPUT_DESCRIPTOR_SCHEMA:
            continue
        if fact.body.payload.get("execution_id") != execution_id:
            continue
        output_name = _payload_str(fact.body.payload, "output_name")
        world_binding = _payload_str(fact.body.payload, "world_binding")
        raw_citation = fact.body.payload.get("citation")
        if not isinstance(raw_citation, dict):
            raise TypeError("RunOutput descriptor citation must be an object")
        citation = dict(raw_citation)
        _require_citation_descriptor_agrees(
            citation,
            output_name=output_name,
            world_binding=world_binding,
        )
        if output_name in descriptors:
            raise ValueError(f"duplicate RunOutput descriptor for output {output_name!r}")
        descriptors[output_name] = ProjectedRunOutputDescriptor(
            locator=RunOutputDescriptorLocator(
                execution_id=execution_id,
                output_name=output_name,
                frontier_id=locator_frontier_id,
                descriptor_fact_id=fact_id,
                schema_ref=fact.envelope.schema_ref,
            ),
            citation_payload=citation,
        )
    return descriptors


def project_run_output_descriptor_payloads(
    trace_slice: TraceSlice,
    execution_id: TraceOwnerId,
) -> dict[str, dict[str, Any]]:
    """Compatibility projection returning only descriptor citation payloads."""
    return {
        output_name: dict(record.citation_payload)
        for output_name, record in project_run_output_descriptors(trace_slice, execution_id).items()
    }


def resolve_run_output_descriptor(
    trace_slice: TraceSlice,
    locator: RunOutputDescriptorLocator,
) -> ProjectedRunOutputDescriptor:
    """Resolve one trace-owned RunOutput descriptor from a durable locator."""
    if locator.frontier_id is None:
        raise ValueError("RunOutput descriptor locator resolution requires frontier_id")
    frontier = trace_slice.frontier
    if frontier is None:
        raise ValueError("RunOutput descriptor locator resolution requires a resolved frontier")
    if frontier.frontier_id != locator.frontier_id:
        raise ValueError("RunOutput descriptor locator frontier_id disagrees with trace slice")
    if frontier.target_trace_owner_id != locator.execution_id:
        raise ValueError("RunOutput descriptor locator execution_id disagrees with frontier target")
    descriptors = project_run_output_descriptors(
        trace_slice,
        locator.execution_id,
        frontier_id=locator.frontier_id,
    )
    record = descriptors.get(locator.output_name)
    if record is None:
        for projected in descriptors.values():
            if projected.locator.descriptor_fact_id == locator.descriptor_fact_id:
                raise ValueError("RunOutput descriptor locator output_name disagrees with descriptor fact")
        raise ValueError("RunOutput descriptor locator output_name is not visible in the execution owner path")
    if record.locator.descriptor_fact_id != locator.descriptor_fact_id:
        raise ValueError("RunOutput descriptor locator descriptor_fact_id is not visible for output_name")
    if record.locator.schema_ref != locator.schema_ref:
        raise ValueError("RunOutput descriptor locator schema_ref disagrees with descriptor fact")
    if record.locator != locator:
        raise ValueError("RunOutput descriptor locator disagrees with projected descriptor")
    return record


def resolve_run_output_descriptor_from_store(
    store: TraceStore,
    read_context: ReadContext,
    locator: RunOutputDescriptorLocator,
) -> ProjectedRunOutputDescriptor:
    """Resolve one trace-owned RunOutput descriptor by locator from a trace store."""
    if locator.frontier_id is None:
        raise ValueError("RunOutput descriptor locator resolution requires frontier_id")
    return resolve_run_output_descriptor(
        store.resolve_frontier(read_context, locator.frontier_id),
        locator,
    )


def _require_citation_descriptor_agrees(
    citation: dict[str, Any],
    *,
    output_name: str,
    world_binding: str,
) -> None:
    if citation.get("output_name") != output_name:
        raise ValueError("RunOutput descriptor output_name disagrees with citation")
    if citation.get("binding") != world_binding:
        raise ValueError("RunOutput descriptor world_binding disagrees with citation")


def _locator_frontier_id(
    trace_slice: TraceSlice,
    execution_id: TraceOwnerId,
    requested_frontier_id: str | None,
) -> str | None:
    frontier = trace_slice.frontier
    if frontier is None:
        return requested_frontier_id
    if frontier.target_trace_owner_id != execution_id:
        raise ValueError("RunOutput descriptor frontier target disagrees with execution_id")
    if requested_frontier_id is not None and requested_frontier_id != frontier.frontier_id:
        raise ValueError("RunOutput descriptor frontier_id disagrees with trace slice")
    return frontier.frontier_id


def _require_non_empty_str(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"RunOutput descriptor {field_name} must be a non-empty string")


def _require_member(value: object, allowed: frozenset[str], field_name: str) -> None:
    if value not in allowed:
        raise ValueError(f"RunOutput descriptor {field_name} is unsupported: {value!r}")


def _require_changed_paths(value: object, field_name: str) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"RunOutput descriptor {field_name} must be a tuple")
    for path in value:
        _require_non_empty_str(path, field_name)


def _require_descriptor_locator_matches(
    locator: RunOutputDescriptorLocator | None,
    *,
    identity: RunOutputIdentity,
    owner: RunOutputOwner,
) -> None:
    if locator is None:
        return
    if locator.output_name != identity.output_name:
        raise ValueError("RunOutput descriptor locator output_name disagrees with identity")
    if owner.kind != "run":
        raise ValueError("RunOutput descriptor locator requires a run owner")
    if owner.kind == "run":
        if locator.execution_id != owner.execution_id:
            raise ValueError("RunOutput descriptor locator execution_id disagrees with owner")
        if locator.frontier_id is not None and locator.frontier_id != owner.frontier_id:
            raise ValueError("RunOutput descriptor locator frontier_id disagrees with owner")


def _require_locator_matches_payload(locator: RunOutputDescriptorLocator, payload: dict[str, Any]) -> None:
    trace_execution_id = payload.get("trace_execution_id")
    if trace_execution_id is not None and trace_execution_id != locator.execution_id:
        raise ValueError("RunOutput descriptor locator execution_id disagrees with citation")
    trace_frontier_id = payload.get("trace_frontier_id")
    if locator.frontier_id is not None and trace_frontier_id is not None and trace_frontier_id != locator.frontier_id:
        raise ValueError("RunOutput descriptor locator frontier_id disagrees with citation")


def _payload_str(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"RunOutput descriptor {field_name} must be a non-empty string")
    return value


__all__ = [
    "RUN_OUTPUT_DESCRIPTOR_LOCATOR_SCHEMA",
    "RUN_OUTPUT_DESCRIPTOR_SCHEMA",
    "RUN_OUTPUT_SCHEMA",
    "ProjectedRunOutputDescriptor",
    "RunOutputCitation",
    "RunOutputDescriptor",
    "RunOutputDescriptorLocator",
    "RunOutputIdentity",
    "RunOutputMaterializationKind",
    "RunOutputOwner",
    "RunOutputOwnerKind",
    "RunOutputRef",
    "RunOutputSettlementAction",
    "RunOutputState",
    "project_run_output_descriptor_payloads",
    "project_run_output_descriptors",
    "resolve_run_output_descriptor",
    "resolve_run_output_descriptor_from_store",
    "run_output_descriptor_fact",
    "run_output_descriptor_locator_from_payload",
    "run_output_descriptor_locator_payload",
    "run_output_id_for",
    "run_output_identity_for",
]
