from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest
from shepherd2.schemas.run_outputs import (
    RUN_OUTPUT_DESCRIPTOR_LOCATOR_SCHEMA,
    RUN_OUTPUT_DESCRIPTOR_SCHEMA,
    RunOutputCitation,
    RunOutputDescriptor,
    RunOutputDescriptorLocator,
    RunOutputOwner,
    RunOutputRef,
    project_run_output_descriptor_payloads,
    project_run_output_descriptors,
    resolve_run_output_descriptor,
    resolve_run_output_descriptor_from_store,
    run_output_descriptor_fact,
    run_output_descriptor_locator_from_payload,
    run_output_descriptor_locator_payload,
    run_output_id_for,
    run_output_identity_for,
)

from shepherd2 import AppendBatch, AppendContext, AppendGroup, FactDraft, OwnerCutoffSpec, ReadContext, SQLiteTraceStore

TRUSTED = AppendContext(
    actor_ref="runtime:test",
    presented_witness_refs=("trusted:internal",),
    schema_version_set="shepherd2-slice-a",
    trust_mode="internal",
)
READER = ReadContext(actor_ref="reader")


def _identity_fields() -> dict[str, str]:
    return {
        "binding": "workspace",
        "parent_ref": "refs/vcscore/scopes/ground",
        "scope_ref": "refs/vcscore/scopes/child",
        "scope_instance_id": "scope-instance",
        "candidate_id": "candidate-1",
        "candidate_head": "sha256:candidate",
        "handoff_ref": "refs/vcscore/retained/handoff",
        "output_world_oid": "world-output",
    }


def _citation_payload(*, output_name: str = "workspace", binding: str = "workspace") -> dict[str, object]:
    return {
        "schema": "shepherd2.skeleton.run_output.v0",
        "output_name": output_name,
        "parent_scope_name": "ground",
        "parent_ref": "refs/vcscore/scopes/ground",
        "scope_name": "child",
        "scope_ref": "refs/vcscore/scopes/child",
        "scope_instance_id": "scope-instance",
        "binding": binding,
        "output_world_oid": "world-output",
        "handoff_ref": "refs/vcscore/retained/handoff",
        "candidate_id": "candidate-1",
        "candidate_ref": "refs/vcscore/candidates/1",
        "candidate_head": "sha256:candidate",
        "parent_basis_world_oid": "world-parent",
        "store_id": "store_workspace",
        "resource_id": "workspace",
        "materialization_kind": "tree",
        "retained_handle_head": "sha256:candidate",
        "changed_paths": ["candidate.txt"],
        "trace_run_id": "run-1",
        "trace_execution_id": "exec:run-1",
        "trace_frontier_id": "frontier:run-1",
    }


def _append_descriptor(
    store: SQLiteTraceStore,
    *,
    execution_id: str = "exec:run-1",
    output_name: str = "workspace",
    binding: str = "workspace",
    citation: dict[str, object] | None = None,
) -> tuple[str, str]:
    descriptor = run_output_descriptor_fact(
        execution_id=execution_id,
        output_name=output_name,
        world_binding=binding,
        citation=citation or _citation_payload(output_name=output_name, binding=binding),
    )
    receipt = store.append(
        TRUSTED,
        AppendBatch(
            append_intent_id=f"intent:run-output-descriptor:{execution_id}:{output_name}",
            groups=(
                AppendGroup(
                    trace_owner_id=execution_id,
                    fact_drafts=(descriptor,),
                ),
            ),
        ),
    )
    return receipt.fact_ids[0], descriptor.schema_ref


def _publish_descriptor_frontier(
    store: SQLiteTraceStore,
    *,
    execution_id: str = "exec:run-1",
    frontier_id: str = "frontier:run-1",
    through_fact_id: str,
) -> None:
    store.publish_frontier(
        TRUSTED,
        OwnerCutoffSpec(
            frontier_id=frontier_id,
            target_trace_owner_id=execution_id,
            through_fact_id=through_fact_id,
        ),
    )


def test_run_output_id_is_stable_for_same_custody_fields() -> None:
    fields = _identity_fields()
    reordered = dict(reversed(tuple(fields.items())))

    assert run_output_id_for(**fields) == run_output_id_for(**reordered)


def test_run_output_id_changes_when_binding_identity_changes() -> None:
    fields = _identity_fields()
    changed = {**fields, "binding": "backend"}

    assert run_output_id_for(**fields) != run_output_id_for(**changed)


def test_run_output_id_changes_when_output_name_changes() -> None:
    fields = _identity_fields()

    assert run_output_id_for(**fields) != run_output_id_for(**fields, output_name="backend")


def test_run_output_identity_carries_binding_and_parent_metadata() -> None:
    identity = run_output_identity_for(
        **_identity_fields(),
        parent_scope_name="ground",
        parent_scope_instance_id=None,
        scope_name="child",
    )

    assert identity.output_id.startswith("run-output:")
    assert identity.output_name == "workspace"
    assert identity.binding == "workspace"
    assert identity.parent_scope_name == "ground"
    assert identity.parent_scope_instance_id is None
    assert identity.scope_name == "child"


def test_run_output_ref_is_immutable_query_value() -> None:
    identity = run_output_identity_for(
        **_identity_fields(),
        parent_scope_name="ground",
        parent_scope_instance_id=None,
        scope_name="child",
    )
    descriptor = RunOutputDescriptor(
        output_name="workspace",
        world_binding="workspace",
        store_id="store",
        resource_id="resource",
        materialization_kind="tree",
    )
    ref = RunOutputRef(
        identity=identity,
        owner=RunOutputOwner(kind="run", run_id="run-1", execution_id="exec-1", frontier_id="frontier-1"),
        descriptor=descriptor,
        state="unconsumed",
        parent_basis_world_oid="world-parent",
        candidate_ref="refs/vcscore/candidates/1",
        store_id="store",
        resource_id="resource",
        changed_paths=("candidate.txt",),
        settlement_ref=None,
    )

    assert ref.owner.kind == "run"
    assert ref.descriptor == descriptor
    assert ref.changed_paths == ("candidate.txt",)
    with pytest.raises(FrozenInstanceError):
        ref.state = "selected"  # type: ignore[misc]


def test_run_output_citation_is_trace_value_without_custody_state() -> None:
    identity = run_output_identity_for(
        **_identity_fields(),
        parent_scope_name="ground",
        parent_scope_instance_id=None,
        scope_name="child",
    )
    descriptor = RunOutputDescriptor(
        output_name="workspace",
        world_binding="workspace",
        store_id="store",
        resource_id="resource",
        materialization_kind="tree",
    )
    citation = RunOutputCitation(
        identity=identity,
        owner=RunOutputOwner(kind="run", run_id="run-1", execution_id="exec-1", frontier_id="frontier-1"),
        descriptor=descriptor,
        parent_basis_world_oid="world-parent",
        candidate_ref="refs/vcscore/candidates/1",
        store_id="store",
        resource_id="resource",
        changed_paths=("candidate.txt",),
    )

    assert citation.identity == identity
    assert citation.owner.kind == "run"
    assert citation.descriptor.materialization_kind == "tree"
    assert not hasattr(citation, "state")
    assert not hasattr(citation, "settlement_ref")
    with pytest.raises(FrozenInstanceError):
        citation.candidate_ref = "refs/vcscore/candidates/2"  # type: ignore[misc]


def test_retained_query_owner_shape_is_distinct_from_run_owner() -> None:
    assert RunOutputOwner(kind="retained-query") != RunOutputOwner(
        kind="run",
        run_id="run-1",
        execution_id="exec-1",
        frontier_id="frontier-1",
    )


def test_run_output_owner_rejects_incomplete_or_mixed_identity() -> None:
    with pytest.raises(ValueError, match="owner run_id"):
        RunOutputOwner(kind="run", execution_id="exec-1", frontier_id="frontier-1")

    with pytest.raises(ValueError, match="must not carry run identity"):
        RunOutputOwner(kind="retained-query", run_id="run-1")

    with pytest.raises(ValueError, match="owner kind"):
        RunOutputOwner(kind="workspace")  # type: ignore[arg-type]


def test_run_output_descriptor_rejects_invalid_materialization_kind() -> None:
    with pytest.raises(ValueError, match="materialization_kind"):
        RunOutputDescriptor(
            output_name="workspace",
            world_binding="workspace",
            store_id="store",
            resource_id="resource",
            materialization_kind="blob",  # type: ignore[arg-type]
        )


def test_run_output_identity_rejects_forged_output_id() -> None:
    identity = run_output_identity_for(
        **_identity_fields(),
        parent_scope_name="ground",
        parent_scope_instance_id=None,
        scope_name="child",
    )

    with pytest.raises(ValueError, match="output_id disagrees"):
        replace(identity, output_id="run-output:forged")


def test_run_output_ref_rejects_invalid_state_and_reason_usage() -> None:
    identity = run_output_identity_for(
        **_identity_fields(),
        parent_scope_name="ground",
        parent_scope_instance_id=None,
        scope_name="child",
    )
    descriptor = RunOutputDescriptor(
        output_name="workspace",
        world_binding="workspace",
        store_id="store",
        resource_id="resource",
        materialization_kind="tree",
    )
    owner = RunOutputOwner(kind="run", run_id="run-1", execution_id="exec-1", frontier_id="frontier-1")

    with pytest.raises(ValueError, match="ref state"):
        RunOutputRef(
            identity=identity,
            owner=owner,
            descriptor=descriptor,
            state="pending",  # type: ignore[arg-type]
            parent_basis_world_oid="world-parent",
            candidate_ref="refs/vcscore/candidates/1",
            store_id="store",
            resource_id="resource",
        )

    with pytest.raises(ValueError, match="invalid_reason"):
        RunOutputRef(
            identity=identity,
            owner=owner,
            descriptor=descriptor,
            state="invalid",
            parent_basis_world_oid="world-parent",
            candidate_ref="refs/vcscore/candidates/1",
            store_id="store",
            resource_id="resource",
        )

    with pytest.raises(ValueError, match="only valid for invalid state"):
        RunOutputRef(
            identity=identity,
            owner=owner,
            descriptor=descriptor,
            state="unconsumed",
            parent_basis_world_oid="world-parent",
            candidate_ref="refs/vcscore/candidates/1",
            store_id="store",
            resource_id="resource",
            invalid_reason="not invalid",
        )

    with pytest.raises(TypeError, match="changed_paths"):
        RunOutputRef(
            identity=identity,
            owner=owner,
            descriptor=descriptor,
            state="unconsumed",
            parent_basis_world_oid="world-parent",
            candidate_ref="refs/vcscore/candidates/1",
            store_id="store",
            resource_id="resource",
            changed_paths=["candidate.txt"],  # type: ignore[arg-type]
        )


def test_run_output_citation_rejects_descriptor_locator_owner_mismatch() -> None:
    identity = run_output_identity_for(
        **_identity_fields(),
        parent_scope_name="ground",
        parent_scope_instance_id=None,
        scope_name="child",
    )
    descriptor = RunOutputDescriptor(
        output_name="workspace",
        world_binding="workspace",
        store_id="store",
        resource_id="resource",
        materialization_kind="tree",
    )

    with pytest.raises(ValueError, match="execution_id disagrees"):
        RunOutputCitation(
            identity=identity,
            owner=RunOutputOwner(kind="run", run_id="run-1", execution_id="exec-1", frontier_id="frontier-1"),
            descriptor=descriptor,
            parent_basis_world_oid="world-parent",
            candidate_ref="refs/vcscore/candidates/1",
            store_id="store",
            resource_id="resource",
            descriptor_locator=RunOutputDescriptorLocator(
                execution_id="exec-2",
                output_name="workspace",
                frontier_id="frontier-1",
                descriptor_fact_id="fact-1",
            ),
        )

    with pytest.raises(ValueError, match="requires a run owner"):
        RunOutputCitation(
            identity=identity,
            owner=RunOutputOwner(kind="retained-query"),
            descriptor=descriptor,
            parent_basis_world_oid="world-parent",
            candidate_ref="refs/vcscore/candidates/1",
            store_id="store",
            resource_id="resource",
            descriptor_locator=RunOutputDescriptorLocator(
                execution_id="exec-1",
                output_name="workspace",
                frontier_id="frontier-1",
                descriptor_fact_id="fact-1",
            ),
        )


def test_run_output_citation_locator_must_match_run_owner_and_output() -> None:
    identity = run_output_identity_for(
        **_identity_fields(),
        parent_scope_name="ground",
        parent_scope_instance_id=None,
        scope_name="child",
    )
    descriptor = RunOutputDescriptor(
        output_name="workspace",
        world_binding="workspace",
        store_id="store",
        resource_id="resource",
        materialization_kind="tree",
    )
    locator = RunOutputDescriptorLocator(
        execution_id="exec-1",
        output_name="workspace",
        frontier_id="frontier-1",
        descriptor_fact_id="fact:descriptor",
    )

    citation = RunOutputCitation(
        identity=identity,
        owner=RunOutputOwner(kind="run", run_id="run-1", execution_id="exec-1", frontier_id="frontier-1"),
        descriptor=descriptor,
        parent_basis_world_oid="world-parent",
        candidate_ref="refs/vcscore/candidates/1",
        store_id="store",
        resource_id="resource",
        descriptor_locator=locator,
    )

    assert citation.descriptor_locator == locator
    with pytest.raises(ValueError, match="output_name"):
        RunOutputCitation(
            identity=identity,
            owner=RunOutputOwner(kind="run", run_id="run-1", execution_id="exec-1", frontier_id="frontier-1"),
            descriptor=descriptor,
            parent_basis_world_oid="world-parent",
            candidate_ref="refs/vcscore/candidates/1",
            store_id="store",
            resource_id="resource",
            descriptor_locator=RunOutputDescriptorLocator(
                execution_id="exec-1",
                output_name="patch",
                frontier_id="frontier-1",
                descriptor_fact_id="fact:descriptor",
            ),
        )


def test_run_output_ref_locator_must_match_run_owner() -> None:
    identity = run_output_identity_for(
        **_identity_fields(),
        parent_scope_name="ground",
        parent_scope_instance_id=None,
        scope_name="child",
    )
    descriptor = RunOutputDescriptor(
        output_name="workspace",
        world_binding="workspace",
        store_id="store",
        resource_id="resource",
        materialization_kind="tree",
    )

    with pytest.raises(ValueError, match="frontier_id"):
        RunOutputRef(
            identity=identity,
            owner=RunOutputOwner(kind="run", run_id="run-1", execution_id="exec-1", frontier_id="frontier-other"),
            descriptor=descriptor,
            state="unconsumed",
            parent_basis_world_oid="world-parent",
            candidate_ref="refs/vcscore/candidates/1",
            store_id="store",
            resource_id="resource",
            descriptor_locator=RunOutputDescriptorLocator(
                execution_id="exec-1",
                output_name="workspace",
                frontier_id="frontier-1",
                descriptor_fact_id="fact:descriptor",
            ),
        )


def test_run_output_descriptor_locator_payload_round_trips_frontier_backed_locator() -> None:
    locator = RunOutputDescriptorLocator(
        execution_id="exec:run-1",
        output_name="workspace",
        frontier_id="frontier:run-1",
        descriptor_fact_id="fact:descriptor",
    )

    payload = run_output_descriptor_locator_payload(locator)

    assert payload == {
        "schema": RUN_OUTPUT_DESCRIPTOR_LOCATOR_SCHEMA,
        "execution_id": "exec:run-1",
        "output_name": "workspace",
        "frontier_id": "frontier:run-1",
        "descriptor_fact_id": "fact:descriptor",
        "schema_ref": RUN_OUTPUT_DESCRIPTOR_SCHEMA,
    }
    assert run_output_descriptor_locator_from_payload(payload) == locator


def test_run_output_descriptor_locator_payload_rejects_prefix_read_locator() -> None:
    locator = RunOutputDescriptorLocator(
        execution_id="exec:run-1",
        output_name="workspace",
        frontier_id=None,
        descriptor_fact_id="fact:descriptor",
    )

    with pytest.raises(ValueError, match="frontier_id"):
        run_output_descriptor_locator_payload(locator)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema": "unsupported",
            "execution_id": "exec:run-1",
            "output_name": "workspace",
            "frontier_id": "frontier:run-1",
            "descriptor_fact_id": "fact:descriptor",
            "schema_ref": RUN_OUTPUT_DESCRIPTOR_SCHEMA,
        },
        {
            "schema": RUN_OUTPUT_DESCRIPTOR_LOCATOR_SCHEMA,
            "execution_id": "exec:run-1",
            "output_name": "workspace",
            "descriptor_fact_id": "fact:descriptor",
            "schema_ref": RUN_OUTPUT_DESCRIPTOR_SCHEMA,
        },
        {
            "schema": RUN_OUTPUT_DESCRIPTOR_LOCATOR_SCHEMA,
            "execution_id": 17,
            "output_name": "workspace",
            "frontier_id": "frontier:run-1",
            "descriptor_fact_id": "fact:descriptor",
            "schema_ref": RUN_OUTPUT_DESCRIPTOR_SCHEMA,
        },
        {
            "schema": RUN_OUTPUT_DESCRIPTOR_LOCATOR_SCHEMA,
            "execution_id": "exec:run-1",
            "output_name": "workspace",
            "frontier_id": "frontier:run-1",
            "descriptor_fact_id": "fact:descriptor",
            "schema_ref": "unsupported",
        },
    ],
)
def test_run_output_descriptor_locator_from_payload_rejects_malformed_payload(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        run_output_descriptor_locator_from_payload(payload)


def test_run_output_descriptor_fact_projects_by_output_name() -> None:
    store = SQLiteTraceStore()
    citation = _citation_payload()
    descriptor = run_output_descriptor_fact(
        execution_id="exec:run-1",
        output_name="workspace",
        world_binding="workspace",
        citation=citation,
    )

    assert descriptor.schema_ref == RUN_OUTPUT_DESCRIPTOR_SCHEMA
    assert descriptor.fact_kind == "run_output_descriptor"

    receipt = store.append(
        TRUSTED,
        AppendBatch(
            append_intent_id="intent:run-output-descriptor",
            groups=(
                AppendGroup(
                    trace_owner_id="exec:run-1",
                    fact_drafts=(descriptor,),
                ),
            ),
        ),
    )
    trace_slice = store.read_owner_prefix(READER, "exec:run-1", len(receipt.fact_ids))

    records = project_run_output_descriptors(trace_slice, "exec:run-1", frontier_id="frontier:run-1")
    record = records["workspace"]

    assert record.citation_payload == citation
    assert record.locator.execution_id == "exec:run-1"
    assert record.locator.output_name == "workspace"
    assert record.locator.frontier_id == "frontier:run-1"
    assert record.locator.descriptor_fact_id == receipt.fact_ids[0]
    assert record.locator.schema_ref == RUN_OUTPUT_DESCRIPTOR_SCHEMA
    assert project_run_output_descriptor_payloads(trace_slice, "exec:run-1") == {"workspace": citation}

    cutoff = store.publish_frontier(
        TRUSTED,
        OwnerCutoffSpec(
            frontier_id="frontier:run-1",
            target_trace_owner_id="exec:run-1",
            through_fact_id=receipt.fact_ids[0],
        ),
    )
    frontier_slice = store.resolve_frontier(READER, cutoff.frontier_id)
    frontier_records = project_run_output_descriptors(frontier_slice, "exec:run-1")
    assert frontier_records["workspace"].locator.frontier_id == "frontier:run-1"
    assert (
        resolve_run_output_descriptor(frontier_slice, frontier_records["workspace"].locator)
        == frontier_records["workspace"]
    )
    assert (
        resolve_run_output_descriptor_from_store(store, READER, frontier_records["workspace"].locator)
        == (frontier_records["workspace"])
    )
    with pytest.raises(ValueError, match="frontier_id disagrees"):
        project_run_output_descriptors(frontier_slice, "exec:run-1", frontier_id="frontier:other")


def test_run_output_descriptor_locator_resolution_requires_frontier_backed_locator() -> None:
    store = SQLiteTraceStore()
    fact_id, _schema_ref = _append_descriptor(store)
    prefix_slice = store.read_owner_prefix(READER, "exec:run-1", 1)
    locator = RunOutputDescriptorLocator(
        execution_id="exec:run-1",
        output_name="workspace",
        frontier_id=None,
        descriptor_fact_id=fact_id,
    )

    with pytest.raises(ValueError, match="requires frontier_id"):
        resolve_run_output_descriptor(prefix_slice, locator)
    with pytest.raises(ValueError, match="requires frontier_id"):
        resolve_run_output_descriptor_from_store(store, READER, locator)


def test_run_output_descriptor_locator_resolution_rejects_frontier_and_owner_mismatch() -> None:
    store = SQLiteTraceStore()
    fact_id, _schema_ref = _append_descriptor(store)
    _publish_descriptor_frontier(store, through_fact_id=fact_id)
    frontier_slice = store.resolve_frontier(READER, "frontier:run-1")
    locator = project_run_output_descriptors(frontier_slice, "exec:run-1")["workspace"].locator

    with pytest.raises(ValueError, match="frontier_id disagrees"):
        resolve_run_output_descriptor(frontier_slice, replace(locator, frontier_id="frontier:other"))
    with pytest.raises(ValueError, match="execution_id disagrees"):
        resolve_run_output_descriptor(
            frontier_slice,
            RunOutputDescriptorLocator(
                execution_id="exec:other",
                output_name="workspace",
                frontier_id="frontier:run-1",
                descriptor_fact_id=locator.descriptor_fact_id,
            ),
        )


def test_run_output_descriptor_locator_resolution_rejects_fact_outside_frontier_or_owner_path() -> None:
    store = SQLiteTraceStore()
    first_fact_id, _schema_ref = _append_descriptor(store)
    _publish_descriptor_frontier(store, through_fact_id=first_fact_id)
    second_fact_id, _schema_ref = _append_descriptor(
        store,
        output_name="backend",
        binding="backend",
        citation=_citation_payload(output_name="backend", binding="backend"),
    )
    other_fact_id, _schema_ref = _append_descriptor(store, execution_id="exec:other")
    frontier_slice = store.resolve_frontier(READER, "frontier:run-1")

    with pytest.raises(ValueError, match="not visible"):
        resolve_run_output_descriptor(
            frontier_slice,
            RunOutputDescriptorLocator(
                execution_id="exec:run-1",
                output_name="backend",
                frontier_id="frontier:run-1",
                descriptor_fact_id=second_fact_id,
            ),
        )
    with pytest.raises(ValueError, match="not visible"):
        resolve_run_output_descriptor(
            frontier_slice,
            RunOutputDescriptorLocator(
                execution_id="exec:run-1",
                output_name="workspace",
                frontier_id="frontier:run-1",
                descriptor_fact_id=other_fact_id,
            ),
        )


def test_run_output_descriptor_locator_resolution_rejects_output_name_mismatch() -> None:
    store = SQLiteTraceStore()
    fact_id, _schema_ref = _append_descriptor(store)
    _publish_descriptor_frontier(store, through_fact_id=fact_id)
    frontier_slice = store.resolve_frontier(READER, "frontier:run-1")
    locator = project_run_output_descriptors(frontier_slice, "exec:run-1")["workspace"].locator

    with pytest.raises(ValueError, match="output_name disagrees"):
        resolve_run_output_descriptor(frontier_slice, replace(locator, output_name="patch"))


def test_run_output_descriptor_locator_resolution_rejects_malformed_citation() -> None:
    store = SQLiteTraceStore()
    receipt = store.append(
        TRUSTED,
        AppendBatch(
            append_intent_id="intent:malformed-run-output-descriptor",
            groups=(
                AppendGroup(
                    trace_owner_id="exec:run-1",
                    fact_drafts=(
                        FactDraft(
                            mode="capture",
                            schema_ref=RUN_OUTPUT_DESCRIPTOR_SCHEMA,
                            kind_label="run_output_descriptor",
                            payload={
                                "execution_id": "exec:run-1",
                                "output_name": "workspace",
                                "world_binding": "workspace",
                                "citation": [],
                            },
                        ),
                    ),
                ),
            ),
        ),
    )
    _publish_descriptor_frontier(store, through_fact_id=receipt.fact_ids[0])
    frontier_slice = store.resolve_frontier(READER, "frontier:run-1")

    with pytest.raises(TypeError, match="citation must be an object"):
        resolve_run_output_descriptor(
            frontier_slice,
            RunOutputDescriptorLocator(
                execution_id="exec:run-1",
                output_name="workspace",
                frontier_id="frontier:run-1",
                descriptor_fact_id=receipt.fact_ids[0],
            ),
        )


def test_run_output_descriptor_projection_rejects_duplicate_output_names() -> None:
    store = SQLiteTraceStore()
    first = run_output_descriptor_fact(
        execution_id="exec:run-1",
        output_name="workspace",
        world_binding="workspace",
        citation=_citation_payload(),
    )
    second = run_output_descriptor_fact(
        execution_id="exec:run-1",
        output_name="workspace",
        world_binding="workspace",
        citation=_citation_payload(),
    )
    receipt = store.append(
        TRUSTED,
        AppendBatch(
            append_intent_id="intent:duplicate-run-output-descriptor",
            groups=(
                AppendGroup(
                    trace_owner_id="exec:run-1",
                    fact_drafts=(first, second),
                ),
            ),
        ),
    )
    trace_slice = store.read_owner_prefix(READER, "exec:run-1", len(receipt.fact_ids))

    with pytest.raises(ValueError, match="duplicate RunOutput descriptor"):
        project_run_output_descriptor_payloads(trace_slice, "exec:run-1")


def test_run_output_descriptor_locator_resolution_rejects_duplicate_output_names() -> None:
    store = SQLiteTraceStore()
    first = run_output_descriptor_fact(
        execution_id="exec:run-1",
        output_name="workspace",
        world_binding="workspace",
        citation=_citation_payload(),
    )
    second = run_output_descriptor_fact(
        execution_id="exec:run-1",
        output_name="workspace",
        world_binding="workspace",
        citation=_citation_payload(),
    )
    receipt = store.append(
        TRUSTED,
        AppendBatch(
            append_intent_id="intent:duplicate-run-output-descriptor-frontier",
            groups=(
                AppendGroup(
                    trace_owner_id="exec:run-1",
                    fact_drafts=(first, second),
                ),
            ),
        ),
    )
    _publish_descriptor_frontier(store, through_fact_id=receipt.fact_ids[-1])
    trace_slice = store.resolve_frontier(READER, "frontier:run-1")

    with pytest.raises(ValueError, match="duplicate RunOutput descriptor"):
        resolve_run_output_descriptor(
            trace_slice,
            RunOutputDescriptorLocator(
                execution_id="exec:run-1",
                output_name="workspace",
                frontier_id="frontier:run-1",
                descriptor_fact_id=receipt.fact_ids[0],
            ),
        )


def test_run_output_descriptor_fact_rejects_citation_mismatch() -> None:
    with pytest.raises(ValueError, match="output_name disagrees"):
        run_output_descriptor_fact(
            execution_id="exec:run-1",
            output_name="patch",
            world_binding="workspace",
            citation=_citation_payload(output_name="workspace", binding="workspace"),
        )


def test_run_output_descriptor_fact_can_represent_alias_shape() -> None:
    citation = _citation_payload(output_name="patch", binding="workspace")
    descriptor = run_output_descriptor_fact(
        execution_id="exec:run-1",
        output_name="patch",
        world_binding="workspace",
        citation=citation,
    )

    assert descriptor.payload["output_name"] == "patch"
    assert descriptor.payload["world_binding"] == "workspace"
    assert descriptor.payload["citation"] == citation


# --- W4.2: durable-state compatibility fails closed on unknown vocabulary -----
# Pins the D3 claim (durable-state-compatibility.md): opening store state
# written by an incompatible Shepherd version must raise a diagnosable refusal
# that NAMES the offending vocabulary — never a silent misread.


def _valid_locator_payload() -> dict[str, str]:
    return run_output_descriptor_locator_payload(
        RunOutputDescriptorLocator(
            execution_id="exec:run-1",
            output_name="workspace",
            frontier_id="frontier:run-1",
            descriptor_fact_id="fact:desc-1",
            schema_ref=RUN_OUTPUT_DESCRIPTOR_SCHEMA,
        )
    )


def test_durable_state_fails_closed_on_unknown_vocabulary() -> None:
    """An unknown persisted schema string is refused, and the error names it."""
    payload = _valid_locator_payload()
    payload["schema"] = "legacy.run_output_descriptor_locator.v0"  # an older/foreign vocabulary

    with pytest.raises(ValueError) as exc:
        run_output_descriptor_locator_from_payload(payload)

    message = str(exc.value)
    # fail-closed AND diagnosable: the offending value is named, and the
    # current expected vocabulary is cited so the mismatch is actionable.
    assert "legacy.run_output_descriptor_locator.v0" in message
    assert RUN_OUTPUT_DESCRIPTOR_LOCATOR_SCHEMA in message


def test_valid_vocabulary_still_round_trips() -> None:
    """The fail-closed guard does not reject current, well-formed payloads."""
    locator = run_output_descriptor_locator_from_payload(_valid_locator_payload())
    assert locator.schema_ref == RUN_OUTPUT_DESCRIPTOR_SCHEMA
