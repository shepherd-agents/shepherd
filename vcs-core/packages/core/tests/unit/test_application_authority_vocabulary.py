"""D7 application-authority vocabulary (T1 W2.4 xiv, pending-record legs).

The pending-authority layer is closed vocabulary with a kind→route derivation
(`AUTHORITY_ROUTE_BY_TRANSACTION_KIND`): these tests pin the `retained_output_application`
column — kind-conditional settlements, the application/selection operation-id exclusivity, the
route derivation, and serde round-trip — so the apply verb's authority lane fails loudly at the
vocabulary layer rather than mysteriously at settlement time.
"""

from __future__ import annotations

import json

import pytest
from vcs_core._authority import (
    AUTHORITY_ROUTE_BY_TRANSACTION_KIND,
    PendingAuthoritySettlement,
    prepare_retained_output_selection_authority,
    retained_output_authority_settlement_metadata,
)
from vcs_core._permission_plan_evidence import permission_plan_digest


def _pending(**overrides: object) -> PendingAuthoritySettlement:
    base: dict[str, object] = {
        "settlement_operation_id": "settle-1",
        "authority_operation_id": "auth-1",
        "scope_name": "run-abc",
        "scope_ref": "refs/vcscore/scopes/run-abc",
        "scope_instance_id": "inst-1",
        "scope_world_id": None,
        "parent_scope_name": "parent",
        "parent_scope_ref": "refs/vcscore/scopes/parent",
        "parent_scope_instance_id": "pinst-1",
        "parent_scope_world_id": None,
        "cohort_id": "cohort-1",
        "candidate_digest": "deadbeef",
        "outcome": "allowed",
        "settlement": "applied",
        "commit_outcome": "pending",
        "decision_ids": ("dec-1",),
        "reason_code": "pending_retained_output_application",
        "transaction_kind": "retained_output_application",
        "application_operation_id": "apply_retained_1234",
    }
    base.update(overrides)
    return PendingAuthoritySettlement(**base)  # type: ignore[arg-type]


def _plan_descriptor(route: str) -> dict[str, object]:
    return {
        "schema": "shepherd.permission-plan.v1",
        "fallback": "refuse",
        "assignments": [
            {
                "monitor": "carrier_check_at_commit",
                "timing": "commit",
                "completeness_basis": "exact_tree_diff",
                "tamper_basis": "content_addressed_store",
                "route": route,
                "evidence": {
                    "effective_match_digest": "m" * 8,
                    "authority_surface_plan_digest": "p" * 8,
                },
            }
        ],
    }


def test_application_pending_record_round_trips() -> None:
    payload = json.loads(json.dumps(_pending().to_dict()))
    parsed = PendingAuthoritySettlement.from_dict(payload)
    assert parsed.transaction_kind == "retained_output_application"
    assert parsed.settlement == "applied"
    assert parsed.application_operation_id == "apply_retained_1234"
    assert parsed.selection_operation_id is None


@pytest.mark.parametrize("settlement", ["selected", "not_selected", "merged", "discarded"])
def test_application_kind_refuses_foreign_settlements(settlement: str) -> None:
    with pytest.raises(ValueError, match="retained_output_application authority settlement"):
        _pending(settlement=settlement)


def test_application_kind_requires_application_operation_id() -> None:
    with pytest.raises(ValueError, match="application_operation_id"):
        _pending(application_operation_id=None)


def test_application_kind_refuses_selection_operation_id() -> None:
    with pytest.raises(ValueError, match="cannot carry selection id"):
        _pending(selection_operation_id="sel-1")


def test_selection_kind_refuses_application_operation_id() -> None:
    with pytest.raises(ValueError, match="cannot carry application id"):
        _pending(
            transaction_kind="retained_output_selection",
            settlement="selected",
            selection_operation_id="sel-1",
            # application_operation_id still set by the fixture default
        )


def test_route_derivation_accepts_application_route_and_refuses_selection_route() -> None:
    """The kind→route derivation is the fail-closed layer E1 found: prove both directions."""
    good = _plan_descriptor("retained_output_application")
    record = _pending(
        permission_plan_digest=permission_plan_digest(good),
        permission_plan_descriptor=good,
    )
    assert record.permission_plan_descriptor is not None  # validated + normalized

    bad = _plan_descriptor("retained_output_selection")
    with pytest.raises(ValueError, match="PermissionPlan route mismatch"):
        _pending(
            permission_plan_digest=permission_plan_digest(bad),
            permission_plan_descriptor=bad,
        )


def test_route_map_is_total_over_transaction_kinds() -> None:
    from vcs_core._authority import AUTHORITY_TRANSACTION_KINDS

    assert set(AUTHORITY_ROUTE_BY_TRANSACTION_KIND) == set(AUTHORITY_TRANSACTION_KINDS)


def test_prepared_application_transaction_derives_application_route() -> None:
    class _Handoff:
        scope_ref = "refs/vcscore/scopes/run-abc"
        scope_name = "run-abc"
        scope_instance_id = "inst-1"
        handoff_ref = "refs/vcscore/handoffs/run-abc"
        candidate_id = "primary"
        candidate_head = "1" * 40
        parent_basis_world_oid = "2" * 40
        output_world_oid = "3" * 40
        binding = "workspace"

    class _Parent:
        ref = "refs/vcscore/scopes/parent"

    prepared = prepare_retained_output_selection_authority(
        selection_operation_id="apply_retained_1234",
        handoff=_Handoff(),
        parent=_Parent(),  # type: ignore[arg-type]
        changed_paths=("b.txt",),
        classification_basis="exact_tree_diff",
        transaction_kind="retained_output_application",
    )
    assert prepared.route == "retained_output_application"
    assert prepared.to_metadata(operation_id="auth-1")["transaction_kind"] == "retained_output_application"


def test_settlement_metadata_requires_exactly_one_settling_operation_id() -> None:
    common: dict[str, object] = {
        "operation_id": "auth-1",
        "cohort_id": "cohort-1",
        "candidate_digest": "deadbeef",
        "outcome": "allowed",
        "settlement": "applied",
        "commit_outcome": "applied",
        "decision_ids": ("dec-1",),
        "reason_code": "applied_after_allowed_decision",
    }
    metadata = retained_output_authority_settlement_metadata(
        **common,  # type: ignore[arg-type]
        application_operation_id="apply_retained_1234",
    )
    assert metadata["application_operation_id"] == "apply_retained_1234"
    assert "selection_operation_id" not in metadata

    with pytest.raises(ValueError, match="exactly one"):
        retained_output_authority_settlement_metadata(**common)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exactly one"):
        retained_output_authority_settlement_metadata(
            **common,  # type: ignore[arg-type]
            selection_operation_id="sel-1",
            application_operation_id="apply_retained_1234",
        )
