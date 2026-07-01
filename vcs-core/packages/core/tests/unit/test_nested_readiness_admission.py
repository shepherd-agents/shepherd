from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from vcs_core._materialization_coordinator import MaterializationAdmission
from vcs_core._query_readiness import (
    ReadinessOperationAuthority,
    ReadinessRequest,
    RuntimeAdmissionContext,
    evaluate_readiness,
)
from vcs_core.recording import NestedParentAuthorization

if TYPE_CHECKING:
    from vcs_core._runtime_types import OperationRefInfo
    from vcs_core.types import ScopeInfo
    from vcs_core.vcscore import VcsCore


def _authority(operation: OperationRefInfo) -> ReadinessOperationAuthority:
    return ReadinessOperationAuthority(
        operation_id=operation.durable_id,
        operation_ref=operation.ref,
        kind=operation.kind,
        scope_ref=operation.scope_ref,
        scope_instance_id=operation.scope_instance_id,
        session_id=operation.session_id,
    )


def _runtime_request(
    scope_ref: str,
    *,
    authorized_operations: tuple[ReadinessOperationAuthority, ...] = (),
) -> ReadinessRequest:
    return ReadinessRequest.create(
        command="vcscore.runtime",
        scope=scope_ref,
        requested_freshness="locked",
        allow_best_effort=False,
        authorized_operations=authorized_operations,
    )


def _abort_open_operations(mg: VcsCore) -> None:
    while mg._pipeline.current_operation() is not None:
        operation = mg._pipeline.current_operation()
        assert operation is not None
        mg._pipeline.abort_operation(handle_id=operation.handle_id)


def _open_ground_operation(mg: VcsCore) -> OperationRefInfo:
    mg._pipeline.set_execution_context(mg.ground)
    return mg._pipeline.begin_operation(
        handle_id="ground-runtime",
        kind="marker.runtime",
        scope=mg.ground,
        session_id=mg._session_id,
    )


def _open_nested_child_operation(
    mg: VcsCore,
    *,
    disposition: str = "adopt",
) -> tuple[ScopeInfo, OperationRefInfo, OperationRefInfo]:
    child = mg.fork(mg.ground, f"readiness-child-{disposition}")
    parent_operation = _open_ground_operation(mg)
    nested = NestedParentAuthorization(
        parent_scope_ref=mg.ground.ref,
        child_scope_ref=child.ref,
        ancestry_chain=(mg.ground.ref,),
    )
    child_operation = mg._pipeline.begin_operation(
        handle_id=f"child-runtime-{disposition}",
        kind="marker.runtime",
        scope=child,
        nested_parent=nested,
        world_disposition=disposition,
        session_id=mg._session_id,
    )
    return child, parent_operation, child_operation


def test_descendant_readiness_requires_owner_derived_authority_and_resolves_depth_two_refs(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    child = mg.fork(mg.ground, "readiness-depth-child")
    grandchild = mg.fork(child, "readiness-depth-grandchild")
    operation = _open_ground_operation(mg)
    request = _runtime_request(grandchild.ref, authorized_operations=(_authority(operation),))

    try:
        owner_payload = mg.query_readiness(request).to_json()
        request_only_payload = evaluate_readiness(
            mg._repo_path,
            request,
            owner=None,
            force_freshness="locked",
        ).to_json()
    finally:
        _abort_open_operations(mg)
        mg.discard(grandchild)
        mg.discard(child)

    assert owner_payload["readiness"]["allowed"] is True
    assert any(
        item["kind"] == "authorized_open_operation" and item["fields"]["scope_ref"] == mg.ground.ref
        for item in owner_payload["items"]
    )
    assert request_only_payload["readiness"]["allowed"] is False
    assert any(issue["code"] == "readiness_operation_scope_mismatch" for issue in request_only_payload["issues"])


def test_flag_off_stays_strict_despite_real_ancestry(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "0")
    child = mg.fork(mg.ground, "readiness-flag-off-child")
    operation = _open_ground_operation(mg)
    request = _runtime_request(child.ref, authorized_operations=(_authority(operation),))

    try:
        payload = mg.query_readiness(request).to_json()
    finally:
        _abort_open_operations(mg)
        mg.discard(child)

    assert payload["readiness"]["allowed"] is False
    assert any(issue["code"] == "readiness_operation_scope_mismatch" for issue in payload["issues"])


def test_unknown_scope_ref_is_strictly_missing(mg: VcsCore) -> None:
    payload = mg.query_readiness(_runtime_request("refs/vcscore/scopes/does-not-exist")).to_json()

    scope_item = next(item for item in payload["items"] if item["domain"] == "scope")
    assert payload["readiness"]["allowed"] is False
    assert scope_item["fields"]["scope_ref"] == "refs/vcscore/scopes/does-not-exist"
    assert scope_item["health"]["status"] == "absent"


def test_parent_target_blocks_on_adopting_nested_child_operation(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    child, _parent_operation, _child_operation = _open_nested_child_operation(mg, disposition="adopt")

    try:
        payload = mg.query_readiness(_runtime_request(mg.ground.ref)).to_json()
    finally:
        _abort_open_operations(mg)
        mg.discard(child)

    item = next(item for item in payload["items"] if item["kind"] == "nested_child_quiescence")
    assert payload["readiness"]["allowed"] is False
    assert item["health"]["presence"] == "present"
    assert item["health"]["validity"] == "invalid"
    assert any(issue["code"] == "readiness_nested_child_quiescence" for issue in payload["issues"])
    assert any(blocker["kind"] == "operation" and blocker["item_id"] == item["id"] for blocker in payload["blockers"])


def test_release_nested_child_operation_exempts_parent_target_with_source_identity(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    child, _parent_operation, _child_operation = _open_nested_child_operation(mg, disposition="release")

    try:
        payload = mg.query_readiness(_runtime_request(mg.ground.ref)).to_json()
    finally:
        _abort_open_operations(mg)
        mg.discard(child)

    item = next(item for item in payload["items"] if item["kind"] == "nested_child_quiescence_exempt")
    assert payload["readiness"]["allowed"] is True
    assert item["fields"]["world_disposition"] == "release"
    assert item["source_identity"]["operation_id"] == "child-runtime-release"
    assert item["source_identity"]["ref_target_oid"]


def test_record_class_is_private_runtime_context_not_request_input(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    child, _parent_operation, _child_operation = _open_nested_child_operation(mg, disposition="adopt")
    request = ReadinessRequest.from_json(
        {
            "command": "vcscore.runtime",
            "scope": mg.ground.ref,
            "requested_freshness": "locked",
            "allow_best_effort": False,
            "record_class": "trace_evidence",
        }
    )

    try:
        public_payload = mg.query_readiness(request).to_json()
        runtime_payload = mg._query_readiness_for_runtime(
            request,
            runtime_admission_context=RuntimeAdmissionContext(record_class="trace_evidence"),
        ).to_json()
    finally:
        _abort_open_operations(mg)
        mg.discard(child)

    assert not hasattr(request, "record_class")
    assert "record_class" not in request.to_json()
    assert public_payload["readiness"]["allowed"] is False
    assert any(item["kind"] == "nested_child_quiescence" for item in public_payload["items"])
    item = next(item for item in runtime_payload["items"] if item["kind"] == "nested_child_quiescence_exempt")
    assert runtime_payload["readiness"]["allowed"] is True
    assert item["fields"]["record_class"] == "trace_evidence"
    assert item["source_identity"]["operation_id"] == "child-runtime-adopt"


def test_owner_none_stays_strict_even_with_persisted_nested_edge(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    child, _parent_operation, child_operation = _open_nested_child_operation(mg, disposition="adopt")
    request = _runtime_request(mg.ground.ref, authorized_operations=(_authority(child_operation),))

    try:
        payload = evaluate_readiness(
            mg._repo_path,
            request,
            owner=None,
            force_freshness="locked",
        ).to_json()
    finally:
        _abort_open_operations(mg)
        mg.discard(child)

    assert payload["readiness"]["allowed"] is False
    assert not any(item["kind"] == "nested_child_quiescence_exempt" for item in payload["items"])
    assert any(issue["code"] == "readiness_operation_scope_mismatch" for issue in payload["issues"])


def test_materialization_admission_keeps_four_arg_readiness_callable() -> None:
    calls: list[tuple[str, str, tuple[ReadinessOperationAuthority, ...], str | None]] = []

    def readiness_admission(
        command: str,
        attempted: str,
        authorities: tuple[ReadinessOperationAuthority, ...],
        scope_selector: str | None,
    ) -> None:
        calls.append((command, attempted, authorities, scope_selector))

    admission = MaterializationAdmission(
        active_scope_names=lambda: (),
        ensure_no_interrupted_lifecycle=lambda _attempted: None,
        ensure_no_open_operation=lambda _attempted: None,
        readiness_admission=readiness_admission,
    )

    admission.require_reset_allowed()

    assert admission._mutation_admission().runtime_readiness_admission is None
    assert calls == [("vcscore.reset-materialized", "reset to materialized", (), None)]
