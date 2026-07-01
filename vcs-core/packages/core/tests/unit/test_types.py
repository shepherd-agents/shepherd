"""Tests for vcs-core public DTOs."""

from __future__ import annotations

import pytest
import vcs_core.types as public_types
from vcs_core.types import (
    DRIVER_INGRESS_RESULT_VALUE_SCHEMA,
    FileChange,
    FileState,
    MaterializationPhase,
    MaterializationPlan,
    OperationSummary,
    RecordedCommandOutcome,
    ScopeInfo,
    normalize_command_value,
    normalize_git_filemode,
    normalize_recorded_command_outcome,
)


def test_scope_info_is_frozen() -> None:
    info = ScopeInfo(name="test", ref="refs/vcscore/scopes/test", instance_id="abc", creation_oid="def")
    assert info.name == "test"
    assert info.world_id is None


def test_public_types_module_does_not_expose_runtime_handles() -> None:
    assert not hasattr(public_types, "OperationRefInfo")
    assert not hasattr(public_types, "RuntimeContext")
    assert not hasattr(public_types, "BuiltInRuntimeBinding")
    assert not hasattr(public_types, "BuiltInSubstrateContext")


@pytest.mark.parametrize("mode", [0o100644, 100644, "100644", "0o100644"])
def test_normalize_git_filemode_accepts_regular_file_modes(mode: object) -> None:
    assert normalize_git_filemode(mode) == 0o100644


@pytest.mark.parametrize("mode", [0o100755, 100755, "100755", "0o100755"])
def test_normalize_git_filemode_accepts_executable_file_modes(mode: object) -> None:
    assert normalize_git_filemode(mode) == 0o100755


@pytest.mark.parametrize("mode", [False, 0o040000, 123, "bad"])
def test_normalize_git_filemode_rejects_unsupported_modes(mode: object) -> None:
    with pytest.raises((TypeError, ValueError), match="Git filemode must be 100644 or 100755"):
        normalize_git_filemode(mode)


def test_file_state_normalizes_user_facing_mode() -> None:
    assert FileState(content=b"payload", mode=100755).mode == 0o100755


def test_materialization_plan_total_operations() -> None:
    plan = MaterializationPlan(
        phases=[
            MaterializationPhase(
                reversibility="auto",
                file_changes=[FileChange(path="a.py", status="added")],
                intents=[],
            ),
            MaterializationPhase(
                reversibility="compensable",
                file_changes=[],
                intents=[{"substrate": "http", "method": "POST"}],
            ),
        ],
        commits_ahead=5,
    )
    assert plan.total_operations == 2
    assert not plan.has_irreversible


def test_materialization_plan_has_irreversible() -> None:
    plan = MaterializationPlan(
        phases=[
            MaterializationPhase(reversibility="none", file_changes=[], intents=[{"type": "email"}]),
        ],
        commits_ahead=1,
    )
    assert plan.has_irreversible
    assert plan.total_operations == 1


def test_operation_summary_uses_world_first_fields() -> None:
    summary = OperationSummary(
        operation_id="op_123",
        label="marker-step",
        kind="marker.runtime",
        status="ok",
        visibility="visible",
        world_id="world_task",
        world_name="task",
        world_ref="refs/vcscore/scopes/task",
        carrier_ref="refs/vcscore/scopes/task",
    )

    assert summary.world_name == "task"
    assert summary.world_ref == "refs/vcscore/scopes/task"
    assert not hasattr(summary, "scope_name")
    assert not hasattr(summary, "scope_ref")


def test_normalize_command_value_handles_nested_transport_shapes() -> None:
    normalized = normalize_command_value(
        {
            "rows": [
                ("alpha", 1, b"hello"),
                ("beta", 2, None),
            ],
            "meta": {
                "count": 2,
                "flags": (True, False),
            },
        }
    )

    assert normalized == {
        "rows": [
            ["alpha", 1, {"__type__": "bytes", "encoding": "base64", "data": "aGVsbG8="}],
            ["beta", 2, None],
        ],
        "meta": {
            "count": 2,
            "flags": [True, False],
        },
    }


def test_normalize_command_value_handles_populated_driver_ingress_result() -> None:
    from vcs_core._substrate_driver import (
        Diagnostic,
        DriverIngressResult,
        DriverSelectionRequirementDraft,
        ObservationDraft,
        RetentionHint,
        TransitionDraft,
    )
    from vcs_core._transition_kernel_records import PayloadDescriptorClaim, RelationshipRequirement

    descriptor = PayloadDescriptorClaim.for_json_payload({"schema": "test/payload/v1", "value": "ok"})
    retention = RetentionHint(kind="revision", target="refs/test/head", digest="sha256:abc", mandatory=True)
    result = DriverIngressResult(
        observations=(
            ObservationDraft(
                observation_id="obs-1",
                evidence_kind="test.evidence",
                stable_observation={"path": "README.md", "nested": {"__type__": "bytes"}},
                observed_head="a" * 40,
                mechanism="unit",
                evidence_payload_descriptor_claim=descriptor,
                metadata={"source": "test"},
            ),
        ),
        transitions=(
            TransitionDraft(
                transition_id="tr-1",
                semantic_op="append",
                payload={"schema": "test/transition/v1", "content": b"hello"},
                observation_ids=("obs-1",),
                evidence_citation_ids=("ev-1",),
                base_heads=("b" * 40,),
                payload_descriptor_claim=descriptor,
                relationship_requirements=(
                    RelationshipRequirement(
                        binding="workspace",
                        relation="descends-from",
                        target_binding="workspace",
                        target_head="c" * 40,
                    ),
                ),
                metadata={"phase": "unit"},
            ),
        ),
        retention_hints=(retention,),
        selection_requirements=(
            DriverSelectionRequirementDraft(
                binding="workspace",
                role="test.Workspace",
                selection_kind="head",
                transition_id="tr-1",
                retention_hints=(retention,),
            ),
        ),
        diagnostics=(Diagnostic(code="note", message="hello", subject="tr-1", detail={"count": 1}),),
    )

    normalized = normalize_command_value(result)

    assert normalized["schema"] == DRIVER_INGRESS_RESULT_VALUE_SCHEMA
    assert normalized["summary"] == {
        "observation_count": 1,
        "transition_count": 1,
        "effect_count": 0,
        "retention_hint_count": 1,
        "selection_requirement_count": 1,
        "diagnostic_count": 1,
    }
    assert normalized["observations"][0]["observation_id"] == "obs-1"
    assert normalized["observations"][0]["stable_observation"]["nested"] == {"__type__": "bytes"}
    assert normalized["transitions"][0]["semantic_op"] == "append"
    assert normalized["transitions"][0]["payload"]["content"] == {
        "__type__": "bytes",
        "encoding": "base64",
        "data": "aGVsbG8=",
    }
    assert normalized["transitions"][0]["relationship_requirements"][0]["relation"] == "descends-from"
    assert normalized["retention_hints"][0]["target"] == "refs/test/head"
    assert normalized["selection_requirements"][0]["transition_id"] == "tr-1"
    assert normalized["diagnostics"][0] == {
        "code": "note",
        "message": "hello",
        "subject": "tr-1",
        "detail": {"count": 1},
    }


def test_normalize_recorded_command_outcome_uses_normalized_value_shape() -> None:
    outcome = RecordedCommandOutcome(oids=("abc123",), value={"payload": b"hello"})

    assert normalize_recorded_command_outcome(outcome) == {
        "oids": ["abc123"],
        "value": {"payload": {"__type__": "bytes", "encoding": "base64", "data": "aGVsbG8="}},
    }


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_normalize_command_value_rejects_non_finite_floats(value: float) -> None:
    with pytest.raises(TypeError, match="NaN or infinity"):
        normalize_command_value(value)


def test_normalize_command_value_rejects_unsupported_container_types() -> None:
    with pytest.raises(TypeError, match="Unsupported command result type"):
        normalize_command_value({"items": {1, 2, 3}})
