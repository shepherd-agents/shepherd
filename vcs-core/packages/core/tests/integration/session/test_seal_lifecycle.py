"""Capability-C seal lifecycle integration tests."""

from __future__ import annotations

import contextlib
import copy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pygit2
import pytest
import vcs_core._retained_output_selection as selection_module
import vcs_core._vcscore_lifecycle as lifecycle
import vcs_core._vcscore_seal as seal_module
from vcs_core import Store, VcsCore, build_builtin_substrate_context
from vcs_core._authority import AuthorityDecision, read_pending_authority_settlement
from vcs_core._errors import InvalidRepositoryStateError, SiblingGroupRecoveryRequiredError
from vcs_core._permission_plan_evidence import permission_plan_digest
from vcs_core._projection_store import SCOPE_REGISTRY_CURRENT_REF, SEAL_AND_SELECT_ENV
from vcs_core._retained_output_settlement import (
    SETTLEMENT_PATH,
    read_retained_output_settlement,
    retained_output_settlement_ref,
    write_retained_output_settlement,
)
from vcs_core._seal_handoff import SEAL_HANDOFF_PATH, read_seal_handoff, write_seal_handoff
from vcs_core._sibling_groups import (
    CarrierLeaseRecord,
    SiblingGroupRecord,
    SiblingHandleRecord,
    sibling_machine_scope_name,
)
from vcs_core._substrate_tree_read import read_substrate_workspace_file
from vcs_core._world_refs import candidate_ref
from vcs_core._world_substrate_adapters import TaskTraceSubstrateDriver
from vcs_core.git_store import build_tree, create_commit_with_recovery, create_or_update_reference, create_signature
from vcs_core.substrates import FilesystemSubstrate, MarkerSubstrate
from vcs_core.types import RetainedOutputSettlement

from ...support.overlays import MockOverlayBackend


def _authority_effects(history: Any) -> list[dict[str, object]]:
    return [commit.metadata for commit in history.commits if str(commit.metadata.get("type", "")).startswith(("Authority", "RetainedOutput", "Prepared"))]


_EFFECTIVE_MATCH_DIGEST = "test-effective-match-digest"
_AUTHORITY_SURFACE_PLAN_DIGEST = "test-authority-surface-plan-digest"
_PERMISSION_PLAN_DESCRIPTOR = {
    "schema": "shepherd.permission-plan.v1",
    "fallback": "enforce",
    "assignments": [
        {
            "monitor": "carrier_check_at_commit",
            "timing": "commit",
            "route": "retained_output_selection",
            "completeness_basis": "test exact-tree-diff retained-output selection evidence",
            "tamper_basis": "test coordinator-owned carrier settlement",
            "confinement": None,
            "evidence": {
                "effective_match_digest": _EFFECTIVE_MATCH_DIGEST,
                "authority_surface_plan_digest": _AUTHORITY_SURFACE_PLAN_DIGEST,
            },
        }
    ],
}
_PERMISSION_PLAN_DIGEST = permission_plan_digest(_PERMISSION_PLAN_DESCRIPTOR)


def _permission_plan_kwargs() -> dict[str, object]:
    return {
        "effective_match_digest": _EFFECTIVE_MATCH_DIGEST,
        "authority_surface_plan_digest": _AUTHORITY_SURFACE_PLAN_DIGEST,
        "permission_plan_digest": _PERMISSION_PLAN_DIGEST,
        "permission_plan_descriptor": _PERMISSION_PLAN_DESCRIPTOR,
    }


def _permission_plan_kwargs_with_descriptor(descriptor: dict[str, object]) -> dict[str, object]:
    return {
        "effective_match_digest": _EFFECTIVE_MATCH_DIGEST,
        "authority_surface_plan_digest": _AUTHORITY_SURFACE_PLAN_DIGEST,
        "permission_plan_digest": permission_plan_digest(descriptor),
        "permission_plan_descriptor": descriptor,
    }


class _CloseRetainedSubstrate:
    def __init__(self, *, fail: bool = False) -> None:
        self.name = "close-retained"
        self.calls: list[tuple[str, str]] = []
        self._fail = fail

    def activate(self) -> None:
        pass

    def deactivate(self) -> None:
        pass

    def close_retained(self, scope_id: str, *, parent_scope: Any) -> None:
        self.calls.append((scope_id, parent_scope.name))
        if self._fail:
            self._fail = False
            raise RuntimeError("close_retained failure")


def _make_mg(
    root: Path,
    *,
    activate: bool = True,
    recover_lifecycle: str | None = None,
    extra_substrates: tuple[object,...] = (),
) -> VcsCore:
    root.mkdir(parents=True, exist_ok=True)
    store = Store(str(root / ".vcscore"))
    context = build_builtin_substrate_context(store, workspace=root, config={})
    mg = VcsCore(
        str(root),
        substrates=[
            MarkerSubstrate(context),
            FilesystemSubstrate(context, backend=MockOverlayBackend()),
            TaskTraceSubstrateDriver(),
            *extra_substrates,
        ],
        store=store,
    )
    if activate:
        mg.activate(recover_lifecycle=recover_lifecycle)
    return mg


def _produce_child_workspace_output(mg: VcsCore, *, child_hints: dict[str, Any] | None = None) -> tuple[Any, Any]:
    mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
    parent = mg.fork(mg.ground, "seal-parent")
    child = mg.fork(parent, "seal-child", hints=child_hints)
    with mg.runtime_activity(
        scope=parent,
        operation_label="seal-parent-run",
        operation_kind="test.seal.parent",
        operation_id="seal-parent-run",
    ):
        mg._execute_recorded_in_child_operation(
            "filesystem",
            "write",
            scope=child,
            operation_id="seal-child-run",
            operation_kind="test.seal.child",
            path="child.txt",
            content=b"child output\n",
        )
    return parent, child


def _produce_ground_child_workspace_output(mg: VcsCore) -> Any:
    mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
    child = mg.fork(mg.ground, "ground-child")
    with mg.runtime_activity(
        scope=mg.ground,
        operation_label="ground-parent-run",
        operation_kind="test.seal.ground-parent",
        operation_id="ground-parent-run",
    ):
        mg._execute_recorded_in_child_operation(
            "filesystem",
            "write",
            scope=child,
            operation_id="ground-child-run",
            operation_kind="test.seal.ground-child",
            path="ground-child.txt",
            content=b"ground child output\n",
        )
    return child


def _produce_named_child_workspace_output(
    mg: VcsCore,
    parent: Any,
    *,
    child_name: str,
    operation_suffix: str,
    path: str,
    content: bytes,
    child_hints: dict[str, Any] | None = None,
) -> Any:
    child = mg.fork(parent, child_name, hints=child_hints)
    with mg.runtime_activity(
        scope=parent,
        operation_label=f"seal-parent-run-{operation_suffix}",
        operation_kind="test.seal.parent",
        operation_id=f"seal-parent-run-{operation_suffix}",
    ):
        mg._execute_recorded_in_child_operation(
            "filesystem",
            "write",
            scope=child,
            operation_id=f"seal-child-run-{operation_suffix}",
            operation_kind="test.seal.child",
            path=path,
            content=content,
        )
    return child


def _write_forged_retained_output_settlement(
    mg: VcsCore,
    handoff: Any,
    *,
    action: str,
    operation_id: str,
    parent_world_before: str,
    parent_world_after: str,
) -> RetainedOutputSettlement:
    settlement_ref = retained_output_settlement_ref(
        scope_name=handoff.scope_name,
        scope_instance_id=handoff.scope_instance_id,
        binding=handoff.binding,
        candidate_id=handoff.candidate_id,
    )
    settlement = RetainedOutputSettlement(
        scope_name=handoff.scope_name,
        scope_ref=handoff.scope_ref,
        scope_instance_id=handoff.scope_instance_id,
        parent_ref=handoff.parent_ref,
        handoff_ref=handoff.handoff_ref,
        output_world_oid=handoff.output_world_oid,
        binding=handoff.binding,
        store_id=handoff.store_id,
        resource_id=handoff.resource_id,
        candidate_id=handoff.candidate_id,
        candidate_head=handoff.candidate_head,
        action=action, # type: ignore[arg-type]
        operation_id=operation_id,
        parent_world_before=parent_world_before,
        parent_world_after=parent_world_after,
        settlement_ref=settlement_ref,
    )
    return write_retained_output_settlement(mg.store, settlement)


def _rewrite_ref_with_malformed_json(mg: VcsCore, ref: str, path: str) -> None:
    repo = mg.store._repo
    tree_oid = build_tree(repo, None, [(path, b"{not json")])
    sig = create_signature("malformed-json")
    oid = create_commit_with_recovery(repo, None, sig, sig, f"malformed-json:{ref}", tree_oid, [])
    create_or_update_reference(repo, ref, oid, force=True)


def _parent_oid(store: Store) -> str:
    return store.log(ref=Store.GROUND_REF, max_count=1)[0].oid


def _sibling(store: Store, *, group_id: str, ordinal: int) -> SiblingHandleRecord:
    machine_scope_name = sibling_machine_scope_name(group_id, ordinal)
    return SiblingHandleRecord(
        world_id=f"{group_id}-world-{ordinal}",
        machine_scope_name=machine_scope_name,
        display_label=f"attempt-{ordinal}",
        scope_ref=f"refs/vcscore/scopes/{machine_scope_name}",
        parent_ref=Store.GROUND_REF,
        creation_oid=_parent_oid(store),
        state="admitted",
        instance_id=f"inst-{ordinal}",
    )


def _sibling_group_record(store: Store, *, group_id: str, status: str = "admitted") -> SiblingGroupRecord:
    siblings = (_sibling(store, group_id=group_id, ordinal=0), _sibling(store, group_id=group_id, ordinal=1))
    return SiblingGroupRecord(
        group_id=group_id,
        parent_ref=Store.GROUND_REF,
        parent_world_id="ground-world",
        admitted_parent_oid=_parent_oid(store),
        status=status, # type: ignore[arg-type]
        siblings=siblings,
        leases=(
            CarrierLeaseRecord(
                lease_id=f"{group_id}-lease-0",
                world_id=siblings[0].world_id,
                substrate="filesystem",
                target_id="workspace",
                mode="writable_carrier",
                resource_key="workspace",
                state="planned",
                carrier_ref=siblings[0].scope_ref,
            ),
        ),
        created_at=1.0,
        updated_at=2.0,
    )


def _publish_sibling_group_blocker(mg: VcsCore, *, group_id: str) -> None:
    assert mg.store._publish_sibling_group_for_recovery_test(
        _sibling_group_record(mg.store, group_id=group_id),
        expected_head_oid=None,
    )


def _read_world_workspace_file(mg: VcsCore, world_oid: str, path: str) -> tuple[bytes, int] | None:
    manager = mg._world_storage()
    world = manager.read_world(world_oid)
    head = world.snapshot.head_for("workspace")
    substrate = manager.store(head.store_id)
    return read_substrate_workspace_file(substrate.repo, head.head, path)


def _trace_payload(frontier: str) -> dict[str, object]:
    return {
        "trace_runtime": "shepherd.trace.provider-neutral.v1",
        "trace_owner_id": "task:seal-child:run",
        "frontier_id": frontier,
        "run_ref": "run_seal_child",
        "identity_domain": "vcscore.canonical.v2",
        "events": [{"id": "e1", "kind": "run.lifecycle", "transition": "finished"}],
        "causal_edges": [],
        "owner_paths": {"task:seal-child:run": ["e1"]},
    }


def test_seal_flag_off_rejects_live_scope(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SEAL_AND_SELECT_ENV, raising=False)
    mg = _make_mg(workspace)
    try:
        child = mg.fork(mg.ground, "seal-child")
        with pytest.raises(Exception, match="VCS_CORE_SEAL_AND_SELECT"):
            mg.seal(child)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_seal_refusal_does_not_persist_lifecycle_run(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    mg = _make_mg(workspace)
    try:
        child = mg.fork(mg.ground, "seal-child")

        with pytest.raises(Exception, match="no v2 child world is published"):
            mg.seal(child)

        assert mg._lifecycle_run is None
        assert mg.discard(child) == "seal-child"
    finally:
        mg.deactivate(warn_on_open_scopes=False)

    fresh = _make_mg(workspace)
    try:
        next_child = fresh.fork(fresh.ground, "after-refused-seal")
        assert next_child.name == "after-refused-seal"
    finally:
        fresh.deactivate(warn_on_open_scopes=False)


def test_seal_retains_scope_and_exposes_retained_workspace_read(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        parent, child = _produce_child_workspace_output(mg)
        child_world = mg.world_oid(child)
        assert child_world is not None
        assert mg.store.read_workspace_file(child.ref, "child.txt") is None

        result = mg.seal(child)

        assert result.scope == child
        assert result.parent == parent
        assert result.handoff.output_world_oid == child_world
        assert result.handoff.producer_operation_id.startswith("wv_python_runtime_capture_seal-child-run_")
        assert "child.txt" in result.handoff.changed_paths
        assert mg.lookup_scope(child.name) is None
        assert mg.store.ref_exists(child.ref)
        retained = mg.store.scope_registry_entry(child.name, status="retained")
        assert retained is not None
        assert not mg.store.scope_registry_projection_mismatches()

        loaded = read_seal_handoff(mg.store, child)
        assert loaded is not None
        assert loaded.handoff == result.handoff
        assert loaded.candidate_tuple.candidate.ref == result.handoff.candidate_ref

        handle = mg.retained_workspace_handle(child.name)
        assert handle.output_world_oid == child_world
        assert handle.basis_ref == result.handoff.handoff_ref
        assert mg.read_retained_workspace_file(child.name, "child.txt") == (b"child output\n", 0o100644)

        next_child = mg.fork(parent, "seal-child-2")
        assert next_child.name == "seal-child-2"
    finally:
        mg.deactivate(warn_on_open_scopes=False)

    fresh = _make_mg(workspace)
    try:
        handle = fresh.retained_workspace_handle("seal-child")
        assert handle.output_world_oid == child_world
        assert fresh.read_retained_workspace_file("seal-child", "child.txt") == (b"child output\n", 0o100644)
    finally:
        fresh.deactivate(warn_on_open_scopes=False)


def test_seal_allows_multiple_retained_siblings_under_one_parent(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "seal-parent")
        child_a = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="seal-child-a",
            operation_suffix="a",
            path="candidate.txt",
            content=b"candidate A\n",
        )
        result_a = mg.seal(child_a)

        child_b = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="seal-child-b",
            operation_suffix="b",
            path="candidate.txt",
            content=b"candidate B\n",
        )
        result_b = mg.seal(child_b)

        assert result_a.handoff.handoff_ref != result_b.handoff.handoff_ref
        assert mg.lookup_scope("seal-child-a") is None
        assert mg.lookup_scope("seal-child-b") is None
        assert mg.store.scope_registry_entry("seal-child-a", status="retained") is not None
        assert mg.store.scope_registry_entry("seal-child-b", status="retained") is not None
        assert read_seal_handoff(mg.store, child_a).handoff == result_a.handoff
        assert read_seal_handoff(mg.store, child_b).handoff == result_b.handoff
        assert mg.read_retained_workspace_file("seal-child-a", "candidate.txt") == (b"candidate A\n", 0o100644)
        assert mg.read_retained_workspace_file("seal-child-b", "candidate.txt") == (b"candidate B\n", 0o100644)
    finally:
        mg.deactivate(warn_on_open_scopes=False)

    fresh = _make_mg(workspace)
    try:
        assert fresh.read_retained_workspace_file("seal-child-a", "candidate.txt") == (b"candidate A\n", 0o100644)
        assert fresh.read_retained_workspace_file("seal-child-b", "candidate.txt") == (b"candidate B\n", 0o100644)
    finally:
        fresh.deactivate(warn_on_open_scopes=False)


def test_retained_name_reuse_fails_without_disturbing_custody(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        parent, child = _produce_child_workspace_output(mg)
        result = mg.seal(child)
        loaded_before = read_seal_handoff(mg.store, child)
        handle_before = mg.retained_workspace_handle(child.name)

        with pytest.raises(Exception, match="retained"):
            mg.fork(parent, child.name)

        assert read_seal_handoff(mg.store, child) == loaded_before
        assert mg.retained_workspace_handle(child.name) == handle_before
        assert mg.store.scope_registry_entry(child.name, status="retained") is not None
        assert mg.store.ref_exists(child.ref)
        assert result.handoff == loaded_before.handoff
        assert mg.read_retained_workspace_file(child.name, "child.txt") == (b"child output\n", 0o100644)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_select_retained_output_advances_parent_binding_without_scalar_merge(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "select-parent")
        trace_outcome = mg.exec("trace", "append", scope=parent, payload=_trace_payload("frontier:parent"))
        parent_world_before = mg.world_oid(parent)
        assert parent_world_before is not None
        parent_trace_head = mg._world_storage().read_world(parent_world_before).snapshot.head_for("trace")

        child = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="select-child",
            operation_suffix="select",
            path="candidate.txt",
            content=b"selected candidate\n",
        )
        seal_result = mg.seal(child)
        parent_scalar_before = mg.store.read_workspace_file(parent.ref, "candidate.txt")

        selection = mg.select_retained_output(child.name, parent=parent)

        assert selection.scope == child
        assert selection.parent == parent
        assert selection.output_world_oid == seal_result.handoff.output_world_oid
        assert selection.parent_world_before == parent_world_before
        assert selection.parent_world_after == mg.world_oid(parent)
        assert selection.settlement.action == "selected"
        assert selection.settlement.handoff_ref == seal_result.handoff.handoff_ref
        assert read_retained_output_settlement(mg.store, selection.settlement.settlement_ref) == selection.settlement

        parent_world_after = mg._world_storage().read_world(selection.parent_world_after)
        assert parent_world_after.snapshot.head_for("workspace").head == seal_result.handoff.candidate_head
        assert parent_world_after.snapshot.head_for("trace") == parent_trace_head
        assert trace_outcome.oids == (parent_trace_head.head,)
        assert _read_world_workspace_file(mg, selection.parent_world_after, "candidate.txt") == (
            b"selected candidate\n",
            0o100644,
        )
        assert mg.store.read_workspace_file(parent.ref, "candidate.txt") == parent_scalar_before
        assert mg.store.scope_registry_entry(child.name, status="retained") is not None
        assert read_seal_handoff(mg.store, child) is not None
        assert mg.read_retained_workspace_file(child.name, "candidate.txt") == (b"selected candidate\n", 0o100644)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_retained_output_authority_allowed_selection_records_evidence(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "retained-allow-parent")
        parent_world_before = mg.world_oid(parent)
        assert parent_world_before is not None
        child = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="retained-allow-child",
            operation_suffix="retained-allow",
            path="candidate.txt",
            content=b"selected candidate\n",
        )
        seal_result = mg.seal(child)

        def decide(request: Any) -> AuthorityDecision:
            assert request.match_view.route == "retained_output_selection"
            assert request.match_view.binding_ref == "workspace"
            assert request.match_view.path == "candidate.txt"
            assert request.classification_basis == "exact_tree_diff"
            assert request.match_view.classification_basis == "exact_tree_diff"
            return AuthorityDecision(
                outcome="allowed",
                reason_code="test_allowed",
                monitor_basis=request.match_view.monitor_basis,
                completeness="complete",
            )

        selection = mg.select_retained_output(
            child.name,
            parent=parent,
            decide=decide,
            authority_operation_id="op_retained_select_allowed",
            **_permission_plan_kwargs(),
        )

        assert selection.settlement.action == "selected"
        assert selection.authority_operation_id == "op_retained_select_allowed"
        assert selection.authority_settlement_operation_id == "op_retained_select_allowed_settlement"
        assert selection.authority_outcome == "allowed"
        assert selection.parent_world_before == parent_world_before
        assert mg.world_oid(parent) == selection.parent_world_after
        assert _read_world_workspace_file(mg, selection.parent_world_after, "candidate.txt") == (
            b"selected candidate\n",
            0o100644,
        )

        history = mg.resolve_operation_history("op_retained_select_allowed", scope=parent)
        effects = _authority_effects(history)
        assert sorted(effect["type"] for effect in effects) == [
            "PreparedRetainedOutputSelection",
            "RetainedOutputAuthorityDecision",
        ]
        decision = next(effect for effect in effects if effect["type"] == "RetainedOutputAuthorityDecision")
        assert decision["outcome"] == "allowed"
        assert decision["monitor_basis"] == "carrier_check_at_commit"
        assert decision["permission_plan_digest"] == _PERMISSION_PLAN_DIGEST
        assert decision["permission_plan_descriptor"] == _PERMISSION_PLAN_DESCRIPTOR
        assert decision["completeness"] == "complete"
        assert decision["request"]["handoff_ref"] == seal_result.handoff.handoff_ref
        assert decision["request"]["classification_basis"] == "exact_tree_diff"
        assert decision["request"]["match_view"]["route"] == "retained_output_selection"
        assert decision["request"]["match_view"]["monitor_basis"] == "carrier_check_at_commit"
        assert decision["request"]["match_view"]["classification_basis"] == "exact_tree_diff"
        settlement_history = mg.resolve_operation_history("op_retained_select_allowed_settlement", scope=parent)
        settlement = next(
            effect
            for effect in _authority_effects(settlement_history)
            if effect["type"] == "RetainedOutputAuthoritySettlement"
        )
        assert settlement["authority_operation_id"] == "op_retained_select_allowed"
        assert settlement["selection_operation_id"] == selection.settlement.operation_id
        assert settlement["permission_plan_digest"] == _PERMISSION_PLAN_DIGEST
        assert settlement["permission_plan_descriptor"] == _PERMISSION_PLAN_DESCRIPTOR
        assert settlement["settlement"] == "selected"
        assert settlement["commit_outcome"] == "selected"
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_retained_output_authority_changed_paths_fallback_is_explicit(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "retained-fallback-parent")
        child = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="retained-fallback-child",
            operation_suffix="retained-fallback",
            path="candidate.txt",
            content=b"fallback candidate\n",
        )
        mg.seal(child)
        monkeypatch.setattr(selection_module, "_retained_selection_authority_file_changes", lambda *a, **k: None)

        def decide(request: Any) -> AuthorityDecision:
            assert request.classification_basis == "changed_paths_fallback"
            assert request.match_view.classification_basis == "changed_paths_fallback"
            assert request.match_view.path == "candidate.txt"
            assert request.match_view.mutates is True
            return AuthorityDecision(
                outcome="allowed",
                reason_code="test_changed_paths_fallback_allowed",
                monitor_basis=request.match_view.monitor_basis,
                completeness="advisory",
            )

        selection = mg.select_retained_output(
            child.name,
            parent=parent,
            decide=decide,
            authority_operation_id="op_retained_select_fallback",
            **_permission_plan_kwargs(),
        )

        assert selection.settlement.action == "selected"
        history = mg.resolve_operation_history("op_retained_select_fallback", scope=parent)
        effects = _authority_effects(history)
        prepared = next(effect for effect in effects if effect["type"] == "PreparedRetainedOutputSelection")
        assert prepared["classification_basis"] == "changed_paths_fallback"
        decision = next(effect for effect in effects if effect["type"] == "RetainedOutputAuthorityDecision")
        assert decision["outcome"] == "allowed"
        assert decision["monitor_basis"] == "carrier_check_at_commit"
        assert decision["permission_plan_digest"] == _PERMISSION_PLAN_DIGEST
        assert decision["completeness"] == "advisory"
        assert decision["request"]["classification_basis"] == "changed_paths_fallback"
        assert decision["request"]["match_view"]["monitor_basis"] == "carrier_check_at_commit"
        assert decision["request"]["match_view"]["classification_basis"] == "changed_paths_fallback"
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_retained_output_authority_requires_permission_plan_evidence(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "retained-missing-plan-parent")
        child = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="retained-missing-plan-child",
            operation_suffix="retained-missing-plan",
            path="candidate.txt",
            content=b"candidate\n",
        )
        seal_result = mg.seal(child)

        def decide(request: Any) -> AuthorityDecision:
            del request
            raise AssertionError("decision provider should not be called without PermissionPlan evidence")

        with pytest.raises(
            InvalidRepositoryStateError, match=r"PermissionPlan evidence invalid: permission_plan_digest"
        ):
            mg.select_retained_output(
                child.name,
                parent=parent,
                decide=decide,
                authority_operation_id="op_retained_missing_plan",
            )

        settlement_ref = retained_output_settlement_ref(
            scope_name=seal_result.handoff.scope_name,
            scope_instance_id=seal_result.handoff.scope_instance_id,
            binding=seal_result.handoff.binding,
            candidate_id=seal_result.handoff.candidate_id,
        )
        assert read_retained_output_settlement(mg.store, settlement_ref, missing_ok=True) is None
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_retained_output_authority_rejects_forged_permission_plan_evidence(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    provider_called = False
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "retained-forged-plan-parent")
        child = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="retained-forged-plan-child",
            operation_suffix="retained-forged-plan",
            path="candidate.txt",
            content=b"candidate\n",
        )
        seal_result = mg.seal(child)
        forged_descriptor = copy.deepcopy(_PERMISSION_PLAN_DESCRIPTOR)
        forged_descriptor["assignments"][0]["route"] = "carrier_diff"

        def decide(request: Any) -> AuthorityDecision:
            nonlocal provider_called
            provider_called = True
            return AuthorityDecision(outcome="allowed", reason_code="should_not_be_called")

        with pytest.raises(InvalidRepositoryStateError, match=r"PermissionPlan.*route"):
            mg.select_retained_output(
                child.name,
                parent=parent,
                decide=decide,
                authority_operation_id="op_retained_forged_plan",
                **_permission_plan_kwargs_with_descriptor(forged_descriptor),
            )

        assert provider_called is False
        settlement_ref = retained_output_settlement_ref(
            scope_name=seal_result.handoff.scope_name,
            scope_instance_id=seal_result.handoff.scope_instance_id,
            binding=seal_result.handoff.binding,
            candidate_id=seal_result.handoff.candidate_id,
        )
        assert read_retained_output_settlement(mg.store, settlement_ref, missing_ok=True) is None
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_retained_output_authority_unclassifiable_selection_refuses_without_consuming(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    provider_called = False
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "retained-unclassifiable-parent")
        parent_world_before = mg.world_oid(parent)
        assert parent_world_before is not None
        child = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="retained-unclassifiable-child",
            operation_suffix="retained-unclassifiable",
            path="candidate.txt",
            content=b"unclassifiable candidate\n",
        )
        monkeypatch.setattr(seal_module, "_changed_paths", lambda *a, **k: ())
        seal_result = mg.seal(child)
        monkeypatch.setattr(selection_module, "_retained_selection_authority_file_changes", lambda *a, **k: None)

        def decide(request: Any) -> AuthorityDecision:
            nonlocal provider_called
            provider_called = True
            return AuthorityDecision(outcome="allowed", reason_code="should_not_be_called")

        with pytest.raises(InvalidRepositoryStateError, match="retained-output selection refused by authority"):
            mg.select_retained_output(
                child.name,
                parent=parent,
                decide=decide,
                authority_operation_id="op_retained_select_unclassifiable",
                **_permission_plan_kwargs(),
            )

        assert provider_called is False
        assert mg.world_oid(parent) == parent_world_before
        settlement_ref = retained_output_settlement_ref(
            scope_name=seal_result.handoff.scope_name,
            scope_instance_id=seal_result.handoff.scope_instance_id,
            binding=seal_result.handoff.binding,
            candidate_id=seal_result.handoff.candidate_id,
        )
        assert read_retained_output_settlement(mg.store, settlement_ref, missing_ok=True) is None
        (row,) = mg.list_retained_outputs(parent=parent, binding="workspace", state="unconsumed")
        assert row.scope_name == child.name

        history = mg.resolve_operation_history("op_retained_select_unclassifiable", scope=parent)
        effects = _authority_effects(history)
        prepared = next(effect for effect in effects if effect["type"] == "PreparedRetainedOutputSelection")
        assert prepared["classification_basis"] == "unclassifiable"
        decision = next(effect for effect in effects if effect["type"] == "RetainedOutputAuthorityDecision")
        assert decision["outcome"] == "refused"
        assert decision["reason_code"] == "unclassifiable_retained_output"
        assert decision["monitor_basis"] == "carrier_check_at_commit"
        assert decision["permission_plan_digest"] == _PERMISSION_PLAN_DIGEST
        assert decision["completeness"] == "incomplete"
        assert decision["request"]["classification_basis"] == "unclassifiable"
        settlement_history = mg.resolve_operation_history(
            "op_retained_select_unclassifiable_settlement",
            scope=parent,
        )
        settlement = next(
            effect
            for effect in _authority_effects(settlement_history)
            if effect["type"] == "RetainedOutputAuthoritySettlement"
        )
        assert settlement["settlement"] == "not_selected"
        assert settlement["commit_outcome"] == "not_selected_refused"
        assert settlement["reason_code"] == "refused_decision"
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_retained_output_authority_denied_selection_records_evidence_without_consuming(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "retained-deny-parent")
        parent_world_before = mg.world_oid(parent)
        assert parent_world_before is not None
        child = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="retained-deny-child",
            operation_suffix="retained-deny",
            path="candidate.txt",
            content=b"blocked candidate\n",
        )
        seal_result = mg.seal(child)

        def decide(request: Any) -> AuthorityDecision:
            assert request.match_view.mutates is True
            return AuthorityDecision(outcome="denied", reason_code="test_readonly_denied")

        with pytest.raises(InvalidRepositoryStateError, match="retained-output selection denied by authority"):
            mg.select_retained_output(
                child.name,
                parent=parent,
                decide=decide,
                authority_operation_id="op_retained_select_denied",
                **_permission_plan_kwargs(),
            )

        assert mg.world_oid(parent) == parent_world_before
        settlement_ref = retained_output_settlement_ref(
            scope_name=seal_result.handoff.scope_name,
            scope_instance_id=seal_result.handoff.scope_instance_id,
            binding=seal_result.handoff.binding,
            candidate_id=seal_result.handoff.candidate_id,
        )
        assert read_retained_output_settlement(mg.store, settlement_ref, missing_ok=True) is None
        (row,) = mg.list_retained_outputs(parent=parent, binding="workspace", state="unconsumed")
        assert row.scope_name == child.name

        history = mg.resolve_operation_history("op_retained_select_denied", scope=parent)
        effects = _authority_effects(history)
        assert any(
            effect["type"] == "RetainedOutputAuthorityDecision" and effect["outcome"] == "denied"
            for effect in effects
        )
        settlement_history = mg.resolve_operation_history("op_retained_select_denied_settlement", scope=parent)
        settlement = next(
            effect
            for effect in _authority_effects(settlement_history)
            if effect["type"] == "RetainedOutputAuthoritySettlement"
        )
        assert settlement["authority_operation_id"] == "op_retained_select_denied"
        assert settlement["settlement"] == "not_selected"
        assert settlement["commit_outcome"] == "not_selected_denied"
        assert settlement["reason_code"] == "denied_decision"
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_retained_output_authority_preflights_settlement_operation_id_before_selection(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "retained-collision-parent")
        parent_world_before = mg.world_oid(parent)
        assert parent_world_before is not None
        child = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="retained-collision-child",
            operation_suffix="retained-collision",
            path="candidate.txt",
            content=b"candidate\n",
        )
        seal_result = mg.seal(child)
        with mg.runtime_activity(
            scope=parent,
            operation_id="op_retained_collision_settlement",
            operation_label="existing retained authority settlement id",
            operation_kind="test.operation-id-collision",
        ):
            pass

        def decide(request: Any) -> AuthorityDecision:
            del request
            raise AssertionError("decision provider should not be called after preflight failure")

        with pytest.raises(ValueError, match="op_retained_collision_settlement"):
            mg.select_retained_output(
                child.name,
                parent=parent,
                decide=decide,
                authority_operation_id="op_retained_collision",
                **_permission_plan_kwargs(),
            )

        settlement_ref = retained_output_settlement_ref(
            scope_name=seal_result.handoff.scope_name,
            scope_instance_id=seal_result.handoff.scope_instance_id,
            binding=seal_result.handoff.binding,
            candidate_id=seal_result.handoff.candidate_id,
        )
        assert read_retained_output_settlement(mg.store, settlement_ref, missing_ok=True) is None
        assert mg.world_oid(parent) == parent_world_before
        assert mg.list_authority_settlement_pending() == ()
        with pytest.raises(ValueError, match="No operation matches"):
            mg.resolve_operation_history("op_retained_collision", scope=parent)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_retained_output_authority_settlement_failure_leaves_recoverable_pending(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    original_record = selection_module.record_retained_output_authority_final_settlement
    fail_next = True
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "retained-recover-parent")
        child = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="retained-recover-child",
            operation_suffix="retained-recover",
            path="candidate.txt",
            content=b"selected candidate\n",
        )
        mg.seal(child)

        def decide(request: Any) -> AuthorityDecision:
            assert request.match_view.path == "candidate.txt"
            return AuthorityDecision(outcome="allowed", reason_code="test_allowed")

        def fail_first_settlement(*args: Any, **kwargs: Any) -> None:
            nonlocal fail_next
            if fail_next:
                fail_next = False
                raise RuntimeError("simulated retained authority settlement failure")
            original_record(*args, **kwargs)

        monkeypatch.setattr(
            selection_module, "record_retained_output_authority_final_settlement", fail_first_settlement
        )
        with pytest.raises(RuntimeError, match="simulated retained authority settlement failure"):
            mg.select_retained_output(
                child.name,
                parent=parent,
                decide=decide,
                authority_operation_id="op_retained_settlement_recovery",
                **_permission_plan_kwargs(),
            )

        pending = read_pending_authority_settlement(
            mg._repo_path,
            "op_retained_settlement_recovery_settlement",
        )
        assert pending.transaction_kind == "retained_output_selection"
        assert pending.phase == "adopted"
        assert pending.commit_outcome == "selected"
        assert pending.permission_plan_digest == _PERMISSION_PLAN_DIGEST
        assert pending.permission_plan_descriptor == _PERMISSION_PLAN_DESCRIPTOR
        assert mg.list_authority_settlement_pending() == ("op_retained_settlement_recovery_settlement",)
        with pytest.raises(InvalidRepositoryStateError, match="pending authority settlement"):
            mg.fork(mg.ground, "blocked-by-retained-authority-pending")

        assert mg.recover_authority_settlements() == ("op_retained_settlement_recovery_settlement",)
        assert mg.list_authority_settlement_pending() == ()
        settlement_history = mg.resolve_operation_history(
            "op_retained_settlement_recovery_settlement", scope=parent
        )
        settlement = next(
            effect
            for effect in _authority_effects(settlement_history)
            if effect["type"] == "RetainedOutputAuthoritySettlement"
        )
        assert settlement["settlement"] == "selected"
        assert settlement["commit_outcome"] == "selected"
        assert settlement["permission_plan_digest"] == _PERMISSION_PLAN_DIGEST
        assert settlement["permission_plan_descriptor"] == _PERMISSION_PLAN_DESCRIPTOR
    finally:
        monkeypatch.setattr(selection_module, "record_retained_output_authority_final_settlement", original_record)
        mg.deactivate(warn_on_open_scopes=False)


def test_retained_output_authority_recovers_pending_before_selection_action(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    original_publish = selection_module.WorldAuthorityFinalizer.publish_or_recover
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "retained-preaction-parent")
        parent_world_before = mg.world_oid(parent)
        assert parent_world_before is not None
        child = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="retained-preaction-child",
            operation_suffix="retained-preaction",
            path="candidate.txt",
            content=b"candidate\n",
        )
        seal_result = mg.seal(child)

        def decide(request: Any) -> AuthorityDecision:
            assert request.match_view.path == "candidate.txt"
            return AuthorityDecision(outcome="allowed", reason_code="test_allowed")

        def fail_publish(*args: Any, **kwargs: Any) -> object:
            raise RuntimeError("simulated crash before retained selection action")

        monkeypatch.setattr(selection_module.WorldAuthorityFinalizer, "publish_or_recover", fail_publish)
        with pytest.raises(RuntimeError, match="simulated crash before retained selection action"):
            mg.select_retained_output(
                child.name,
                parent=parent,
                decide=decide,
                authority_operation_id="op_retained_preaction",
                **_permission_plan_kwargs(),
            )

        assert mg.world_oid(parent) == parent_world_before
        settlement_ref = retained_output_settlement_ref(
            scope_name=seal_result.handoff.scope_name,
            scope_instance_id=seal_result.handoff.scope_instance_id,
            binding=seal_result.handoff.binding,
            candidate_id=seal_result.handoff.candidate_id,
        )
        assert read_retained_output_settlement(mg.store, settlement_ref, missing_ok=True) is None
        pending = read_pending_authority_settlement(mg._repo_path, "op_retained_preaction_settlement")
        assert pending.phase == "pending_action"

        monkeypatch.setattr(selection_module.WorldAuthorityFinalizer, "publish_or_recover", original_publish)
        assert mg.recover_authority_settlements() == ("op_retained_preaction_settlement",)
        assert mg.list_authority_settlement_pending() == ()
        settlement_history = mg.resolve_operation_history("op_retained_preaction_settlement", scope=parent)
        settlement = next(
            effect
            for effect in _authority_effects(settlement_history)
            if effect["type"] == "RetainedOutputAuthoritySettlement"
        )
        assert settlement["outcome"] == "allowed"
        assert settlement["settlement"] == "not_selected"
        assert settlement["commit_outcome"] == "commit_failed_non_authority"
        assert settlement["reason_code"] == "recovered_before_retained_output_selection"
    finally:
        monkeypatch.setattr(selection_module.WorldAuthorityFinalizer, "publish_or_recover", original_publish)
        mg.deactivate(warn_on_open_scopes=False)


def test_select_retained_output_is_consume_once(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        parent, child = _produce_child_workspace_output(mg)
        mg.seal(child)
        selection = mg.select_retained_output(child.name, parent=parent)
        parent_world_after = mg.world_oid(parent)

        with pytest.raises(InvalidRepositoryStateError, match="already settled"):
            mg.select_retained_output(child.name, parent=parent)

        assert mg.world_oid(parent) == parent_world_after
        assert read_retained_output_settlement(mg.store, selection.settlement.settlement_ref) == selection.settlement
    finally:
        mg.deactivate(warn_on_open_scopes=False)


@pytest.mark.parametrize(
    ("method_name", "action"),
    [
        ("release_retained_output", "released"),
        ("discard_retained_output", "discarded"),
    ],
)
def test_retained_output_receipt_only_settlement_is_consume_once(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    action: str,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        parent, child = _produce_child_workspace_output(mg)
        seal_result = mg.seal(child)
        parent_world_before = mg.world_oid(parent)
        assert parent_world_before is not None

        settlement_result = getattr(mg, method_name)(child.name, parent=parent)

        assert settlement_result.scope == child
        assert settlement_result.parent == parent
        assert settlement_result.output_world_oid == seal_result.handoff.output_world_oid
        assert settlement_result.parent_world_before == parent_world_before
        assert settlement_result.parent_world_after == parent_world_before
        assert mg.world_oid(parent) == parent_world_before
        assert settlement_result.settlement.action == action
        assert (
            read_retained_output_settlement(
                mg.store,
                settlement_result.settlement.settlement_ref,
            )
            == settlement_result.settlement
        )
        assert mg.read_retained_workspace_file(child.name, "child.txt") == (b"child output\n", 0o100644)

        with pytest.raises(InvalidRepositoryStateError, match="already settled"):
            getattr(mg, method_name)(child.name, parent=parent)
        with pytest.raises(InvalidRepositoryStateError, match="already settled"):
            mg.select_retained_output(child.name, parent=parent)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


@pytest.mark.parametrize(
    ("first_method", "expected_action", "blocked_methods"),
    [
        ("select_retained_output", "selected", ("release_retained_output", "discard_retained_output")),
        ("release_retained_output", "released", ("select_retained_output", "discard_retained_output")),
        ("discard_retained_output", "discarded", ("select_retained_output", "release_retained_output")),
    ],
)
def test_retained_output_terminal_settlement_blocks_other_terminal_verbs(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_method: str,
    expected_action: str,
    blocked_methods: tuple[str, str],
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        parent, child = _produce_child_workspace_output(mg)
        seal_result = mg.seal(child)
        first_result = getattr(mg, first_method)(child.name, parent=parent)

        assert first_result.settlement.action == expected_action
        for blocked_method in blocked_methods:
            with pytest.raises(InvalidRepositoryStateError, match="already settled"):
                getattr(mg, blocked_method)(child.name, parent=parent)

        settlement_ref = retained_output_settlement_ref(
            scope_name=seal_result.handoff.scope_name,
            scope_instance_id=seal_result.handoff.scope_instance_id,
            binding=seal_result.handoff.binding,
            candidate_id=seal_result.handoff.candidate_id,
        )
        assert read_retained_output_settlement(mg.store, settlement_ref) == first_result.settlement
    finally:
        mg.deactivate(warn_on_open_scopes=False)


@pytest.mark.parametrize(
    ("method_name", "expected_state"),
    [
        ("select_retained_output", "selected"),
        ("release_retained_output", "released"),
        ("discard_retained_output", "discarded"),
    ],
)
def test_list_retained_outputs_classifies_terminal_receipts(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    expected_state: str,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        parent, child = _produce_child_workspace_output(mg)
        seal_result = mg.seal(child)

        before = mg.list_retained_outputs(parent=parent, binding="workspace")

        assert len(before) == 1
        assert before[0].scope_name == child.name
        assert before[0].parent_scope_name == parent.name
        assert before[0].parent_scope_instance_id == parent.instance_id
        assert before[0].state == "unconsumed"
        assert before[0].handoff_ref == seal_result.handoff.handoff_ref
        assert before[0].settlement is None
        assert before[0].invalid_reason is None

        settlement_result = getattr(mg, method_name)(child.name, parent=parent)
        after = mg.list_retained_outputs(parent=parent, binding="workspace")
        filtered = mg.list_retained_outputs(parent=parent, binding="workspace", state=expected_state)

        assert len(after) == 1
        assert len(filtered) == 1
        assert after[0] == filtered[0]
        assert after[0].state == expected_state
        assert after[0].settlement == settlement_result.settlement
        assert after[0].settlement_ref == settlement_result.settlement.settlement_ref
        assert after[0].candidate_head == seal_result.handoff.candidate_head
        assert mg.list_retained_outputs(parent=parent, binding="trace") == ()
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_list_retained_outputs_reports_invalid_retained_custody(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        parent, child = _produce_child_workspace_output(mg)
        seal_result = mg.seal(child)
        mg.store._repo.references[seal_result.handoff.handoff_ref].delete()

        rows = mg.list_retained_outputs(parent=parent, state="invalid")

        assert len(rows) == 1
        assert rows[0].scope_name == child.name
        assert rows[0].parent_scope_name == parent.name
        assert rows[0].parent_scope_instance_id == parent.instance_id
        assert rows[0].state == "invalid"
        assert rows[0].handoff_ref is None
        assert rows[0].invalid_reason is not None
        assert "seal handoff ref is missing" in rows[0].invalid_reason
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_list_retained_outputs_reports_malformed_handoff_payload_as_invalid(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        parent, child = _produce_child_workspace_output(mg)
        seal_result = mg.seal(child)
        _rewrite_ref_with_malformed_json(mg, seal_result.handoff.handoff_ref, SEAL_HANDOFF_PATH)

        rows = mg.list_retained_outputs(parent=parent, state="invalid")

        assert len(rows) == 1
        assert rows[0].scope_name == child.name
        assert rows[0].parent_scope_name == parent.name
        assert rows[0].parent_scope_instance_id == parent.instance_id
        assert rows[0].state == "invalid"
        assert rows[0].handoff_ref is None
        assert rows[0].invalid_reason is not None
        assert "seal handoff" in rows[0].invalid_reason
        assert "malformed JSON" in rows[0].invalid_reason
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_list_retained_outputs_requires_scope_registry_projection(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    registry_target: pygit2.Oid | None = None
    try:
        parent, child = _produce_child_workspace_output(mg)
        mg.seal(child)
        registry_target = mg.store._repo.references[SCOPE_REGISTRY_CURRENT_REF].target
        mg.store._repo.references[SCOPE_REGISTRY_CURRENT_REF].delete()

        with pytest.raises(InvalidRepositoryStateError, match="Scope registry projection is missing"):
            mg.list_retained_outputs(parent=parent)
    finally:
        if registry_target is not None and SCOPE_REGISTRY_CURRENT_REF not in mg.store._repo.references:
            create_or_update_reference(mg.store._repo, SCOPE_REGISTRY_CURRENT_REF, registry_target, force=True)
        mg.deactivate(warn_on_open_scopes=False)


def test_list_retained_outputs_reports_malformed_terminal_receipt_as_invalid(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        parent, child = _produce_child_workspace_output(mg)
        seal_result = mg.seal(child)
        selection = mg.select_retained_output(child.name, parent=parent)
        _rewrite_ref_with_malformed_json(mg, selection.settlement.settlement_ref, SETTLEMENT_PATH)

        rows = mg.list_retained_outputs(parent=parent, state="invalid")

        assert len(rows) == 1
        assert rows[0].scope_name == seal_result.handoff.scope_name
        assert rows[0].state == "invalid"
        assert rows[0].settlement is None
        assert rows[0].settlement_ref == selection.settlement.settlement_ref
        assert rows[0].invalid_reason is not None
        assert "retained output settlement" in rows[0].invalid_reason
        assert "malformed JSON" in rows[0].invalid_reason
    finally:
        mg.deactivate(warn_on_open_scopes=False)


@pytest.mark.parametrize(
    ("action", "bad_operation_id", "expected_reason"),
    [
        ("selected", "forged-selected-operation", "selection world"),
        ("released", "forged-release-operation", "receipt-only settlement operation_id"),
    ],
)
def test_list_retained_outputs_reports_invalid_forged_terminal_receipt(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    bad_operation_id: str,
    expected_reason: str,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        parent, child = _produce_child_workspace_output(mg)
        seal_result = mg.seal(child)
        parent_world_oid = mg.world_oid(parent)
        assert parent_world_oid is not None
        parent_world_after = seal_result.handoff.output_world_oid if action == "selected" else parent_world_oid
        _write_forged_retained_output_settlement(
            mg,
            seal_result.handoff,
            action=action,
            operation_id=bad_operation_id,
            parent_world_before=parent_world_oid,
            parent_world_after=parent_world_after,
        )

        rows = mg.list_retained_outputs(parent=parent, state="invalid")

        assert len(rows) == 1
        assert rows[0].state == "invalid"
        assert rows[0].settlement is None
        assert rows[0].invalid_reason is not None
        assert expected_reason in rows[0].invalid_reason
    finally:
        mg.deactivate(warn_on_open_scopes=False)


@pytest.mark.parametrize("method_name", ["release_retained_output", "discard_retained_output"])
def test_retained_output_receipt_only_settlement_allows_parent_workspace_drift(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        parent, child = _produce_child_workspace_output(mg)
        mg.seal(child)
        mg.exec("filesystem", "write", scope=parent, path="parent.txt", content=b"parent\n")
        parent_world_after_drift = mg.world_oid(parent)
        assert parent_world_after_drift is not None

        settlement_result = getattr(mg, method_name)(child.name, parent=parent)

        assert settlement_result.parent_world_before == parent_world_after_drift
        assert settlement_result.parent_world_after == parent_world_after_drift
        assert mg.world_oid(parent) == parent_world_after_drift
        assert mg.read_retained_workspace_file(child.name, "child.txt") == (b"child output\n", 0o100644)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


@pytest.mark.parametrize("method_name", ["release_retained_output", "discard_retained_output"])
def test_retained_output_receipt_only_settlement_rejects_forged_handle_identity(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        parent, child = _produce_child_workspace_output(mg)
        seal_result = mg.seal(child)
        handle = mg.retained_workspace_handle(child.name)
        parent_world_before = mg.world_oid(parent)

        with pytest.raises(InvalidRepositoryStateError, match="handle disagrees"):
            getattr(mg, method_name)(replace(handle, scope_instance_id="forged-instance"), parent=parent)

        settlement_ref = retained_output_settlement_ref(
            scope_name=seal_result.handoff.scope_name,
            scope_instance_id=seal_result.handoff.scope_instance_id,
            binding=seal_result.handoff.binding,
            candidate_id=seal_result.handoff.candidate_id,
        )
        assert read_retained_output_settlement(mg.store, settlement_ref, missing_ok=True) is None
        assert mg.world_oid(parent) == parent_world_before
    finally:
        mg.deactivate(warn_on_open_scopes=False)


@pytest.mark.parametrize("method_name", ["release_retained_output", "discard_retained_output"])
def test_retained_output_receipt_only_settlement_recovers_missing_selected_receipt_first(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    original_write = selection_module.write_retained_output_settlement
    try:
        parent, child = _produce_child_workspace_output(mg)
        seal_result = mg.seal(child)
        settlement_ref = retained_output_settlement_ref(
            scope_name=seal_result.handoff.scope_name,
            scope_instance_id=seal_result.handoff.scope_instance_id,
            binding=seal_result.handoff.binding,
            candidate_id=seal_result.handoff.candidate_id,
        )
        failed = False

        def fail_once_write(*args: Any, **kwargs: Any) -> object:
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("simulated crash after retained output publication")
            return original_write(*args, **kwargs)

        monkeypatch.setattr(selection_module, "write_retained_output_settlement", fail_once_write)
        with pytest.raises(RuntimeError, match="simulated crash after retained output publication"):
            mg.select_retained_output(child.name, parent=parent)
        assert read_retained_output_settlement(mg.store, settlement_ref, missing_ok=True) is None

        monkeypatch.setattr(selection_module, "write_retained_output_settlement", original_write)
        with pytest.raises(InvalidRepositoryStateError, match="already settled"):
            getattr(mg, method_name)(child.name, parent=parent)

        recovered = read_retained_output_settlement(mg.store, settlement_ref)
        assert recovered is not None
        assert recovered.action == "selected"
    finally:
        monkeypatch.setattr(selection_module, "write_retained_output_settlement", original_write)
        mg.deactivate(warn_on_open_scopes=False)


def test_select_retained_output_rejects_forged_handle_identity(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        parent, child = _produce_child_workspace_output(mg)
        seal_result = mg.seal(child)
        handle = mg.retained_workspace_handle(child.name)
        parent_world_before = mg.world_oid(parent)

        with pytest.raises(InvalidRepositoryStateError, match="handle disagrees"):
            mg.select_retained_output(replace(handle, scope_instance_id="forged-instance"), parent=parent)

        settlement_ref = retained_output_settlement_ref(
            scope_name=seal_result.handoff.scope_name,
            scope_instance_id=seal_result.handoff.scope_instance_id,
            binding=seal_result.handoff.binding,
            candidate_id=seal_result.handoff.candidate_id,
        )
        assert read_retained_output_settlement(mg.store, settlement_ref, missing_ok=True) is None
        assert mg.world_oid(parent) == parent_world_before
        assert mg.read_retained_workspace_file(child.name, "child.txt") == (b"child output\n", 0o100644)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_select_retained_output_allows_unrelated_parent_binding_progress_after_seal(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        parent, child = _produce_child_workspace_output(mg)
        seal_result = mg.seal(child)
        parent_basis_world = seal_result.handoff.parent_basis_world_oid
        parent_after_trace = mg.exec(
            "trace",
            "append",
            scope=parent,
            payload=_trace_payload("frontier:parent-advanced"),
        )
        parent_world_before_selection = mg.world_oid(parent)
        assert parent_world_before_selection is not None
        parent_trace_head = mg._world_storage().read_world(parent_world_before_selection).snapshot.head_for("trace")

        selection = mg.select_retained_output(child.name, parent=parent)

        assert selection.parent_world_before == parent_world_before_selection
        assert selection.parent_world_before != parent_basis_world
        assert selection.parent_world_after == mg.world_oid(parent)
        assert read_retained_output_settlement(mg.store, selection.settlement.settlement_ref) == selection.settlement
        parent_world_after = mg._world_storage().read_world(selection.parent_world_after)
        assert parent_world_after.snapshot.head_for("workspace").head == seal_result.handoff.candidate_head
        assert parent_world_after.snapshot.head_for("trace") == parent_trace_head
        assert parent_after_trace.oids == (parent_trace_head.head,)
        assert mg.read_retained_workspace_file(child.name, "child.txt") == (b"child output\n", 0o100644)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_select_retained_output_fails_closed_when_target_binding_advanced_since_child_fork(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        parent, child = _produce_child_workspace_output(mg)
        seal_result = mg.seal(child)
        mg.exec("filesystem", "write", scope=parent, path="parent.txt", content=b"parent\n")
        parent_world_after_parent_write = mg.world_oid(parent)

        with pytest.raises(InvalidRepositoryStateError, match="binding 'workspace' advanced"):
            mg.select_retained_output(child.name, parent=parent)

        assert mg.world_oid(parent) == parent_world_after_parent_write
        settlement_ref = retained_output_settlement_ref(
            scope_name=seal_result.handoff.scope_name,
            scope_instance_id=seal_result.handoff.scope_instance_id,
            binding=seal_result.handoff.binding,
            candidate_id=seal_result.handoff.candidate_id,
        )
        assert (
            read_retained_output_settlement(
                mg.store,
                settlement_ref,
                missing_ok=True,
            )
            is None
        )
        assert mg.read_retained_workspace_file(child.name, "child.txt") == (b"child output\n", 0o100644)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_select_retained_output_fresh_publication_blocks_on_sibling_group_recovery_debt(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        parent, child = _produce_child_workspace_output(mg)
        seal_result = mg.seal(child)
        settlement_ref = retained_output_settlement_ref(
            scope_name=seal_result.handoff.scope_name,
            scope_instance_id=seal_result.handoff.scope_instance_id,
            binding=seal_result.handoff.binding,
            candidate_id=seal_result.handoff.candidate_id,
        )
        parent_world_before = mg.world_oid(parent)

        _publish_sibling_group_blocker(mg, group_id="sg-selectblock")

        with pytest.raises(SiblingGroupRecoveryRequiredError, match="sg-selectblock"):
            mg.select_retained_output(child.name, parent=parent)

        assert mg.world_oid(parent) == parent_world_before
        assert read_retained_output_settlement(mg.store, settlement_ref, missing_ok=True) is None
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_later_live_sibling_fails_when_selected_output_already_advanced_target_binding(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "select-parent")
        child_a = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="select-child-a",
            operation_suffix="a",
            path="candidate.txt",
            content=b"candidate A\n",
        )
        seal_a = mg.seal(child_a)
        child_b = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="select-child-b",
            operation_suffix="b",
            path="candidate.txt",
            content=b"candidate B\n",
        )
        assert mg.lookup_scope(child_b.name) == child_b

        selection_a = mg.select_retained_output(child_a.name, parent=parent)
        assert selection_a.parent_world_after == mg.world_oid(parent)
        assert _read_world_workspace_file(mg, selection_a.parent_world_after, "candidate.txt") == (
            b"candidate A\n",
            0o100644,
        )
        seal_b = mg.seal(child_b)
        assert seal_b.handoff.parent_basis_world_oid == seal_a.handoff.parent_basis_world_oid
        with pytest.raises(InvalidRepositoryStateError, match="binding 'workspace' advanced"):
            mg.select_retained_output(child_b.name, parent=parent)

        settlement_b_ref = retained_output_settlement_ref(
            scope_name=seal_b.handoff.scope_name,
            scope_instance_id=seal_b.handoff.scope_instance_id,
            binding=seal_b.handoff.binding,
            candidate_id=seal_b.handoff.candidate_id,
        )
        assert read_retained_output_settlement(mg.store, settlement_b_ref, missing_ok=True) is None
        assert mg.read_retained_workspace_file(child_b.name, "candidate.txt") == (b"candidate B\n", 0o100644)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_multi_candidate_query_allows_non_selected_release_after_selection(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "cohort-parent")
        child_a = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="cohort-child-a",
            operation_suffix="a",
            path="candidate-a.txt",
            content=b"candidate A\n",
        )
        seal_a = mg.seal(child_a)
        child_b = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="cohort-child-b",
            operation_suffix="b",
            path="candidate-b.txt",
            content=b"candidate B\n",
        )
        seal_b = mg.seal(child_b)
        assert seal_a.handoff.parent_basis_world_oid == seal_b.handoff.parent_basis_world_oid

        before = {row.scope_name: row for row in mg.list_retained_outputs(parent=parent)}
        assert before["cohort-child-a"].state == "unconsumed"
        assert before["cohort-child-b"].state == "unconsumed"

        mg.select_retained_output(child_a.name, parent=parent)
        with pytest.raises(InvalidRepositoryStateError, match="binding 'workspace' advanced"):
            mg.select_retained_output(child_b.name, parent=parent)
        mg.release_retained_output(child_b.name, parent=parent)
        after = {row.scope_name: row for row in mg.list_retained_outputs(parent=parent)}

        assert after["cohort-child-a"].state == "selected"
        assert after["cohort-child-b"].state == "released"
        assert mg.read_retained_workspace_file(child_b.name, "candidate-b.txt") == (b"candidate B\n", 0o100644)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_private_candidate_set_select_records_archived_candidate_before_release(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "cohort-archive-parent")
        child_a = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="cohort-archive-child-a",
            operation_suffix="archive-a",
            path="candidate.txt",
            content=b"candidate A\n",
        )
        seal_a = mg.seal(child_a)
        child_b = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="cohort-archive-child-b",
            operation_suffix="archive-b",
            path="candidate.txt",
            content=b"candidate B\n",
        )
        seal_b = mg.seal(child_b)
        assert seal_a.handoff.parent_basis_world_oid == seal_b.handoff.parent_basis_world_oid

        selection_a = selection_module._select_retained_candidate_set(
            mg,
            child_a.name,
            parent=parent,
            archived=(child_b.name,),
        )
        selected_world = mg._world_storage().read_world(selection_a.parent_world_after)
        outcomes = {
            (str(outcome["candidate"]), str(outcome["outcome"])): outcome
            for outcome in selected_world.operation_final["candidate_outcomes"]
        }

        assert selected_world.operation_final["selected"]["workspace"] == seal_a.handoff.candidate_head
        assert (seal_a.handoff.candidate_head, "selected") in outcomes
        assert (seal_b.handoff.candidate_head, "archived") in outcomes
        assert _read_world_workspace_file(mg, selection_a.parent_world_after, "candidate.txt") == (
            b"candidate A\n",
            0o100644,
        )

        before_release = {row.scope_name: row for row in mg.list_retained_outputs(parent=parent)}
        assert before_release[child_a.name].state == "selected"
        assert before_release[child_b.name].state == "unconsumed"
        with pytest.raises(InvalidRepositoryStateError, match="binding 'workspace' advanced"):
            mg.select_retained_output(child_b.name, parent=parent)

        mg.release_retained_output(child_b.name, parent=parent)
        after_release = {row.scope_name: row for row in mg.list_retained_outputs(parent=parent)}
        assert after_release[child_a.name].state == "selected"
        assert after_release[child_b.name].state == "released"
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_retained_candidate_set_capstone_selects_one_of_four_after_reactivation(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    original_store_merge = mg.store.merge
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent_world_before = mg.world_oid(mg.ground)
        assert parent_world_before is not None
        children = []
        seal_results = []
        for index in range(4):
            child = _produce_named_child_workspace_output(
                mg,
                mg.ground,
                child_name=f"capstone-child-{index}",
                operation_suffix=f"capstone-{index}",
                path="candidate.txt",
                content=f"candidate {index}\n".encode(),
            )
            children.append(child)
            seal_results.append(mg.seal(child))
        assert {result.handoff.parent_basis_world_oid for result in seal_results} == {parent_world_before}

        def fail_store_merge(*args: object, **kwargs: object) -> str:
            raise AssertionError("retained candidate-set selection must not call child lifecycle merge")

        monkeypatch.setattr(mg.store, "merge", fail_store_merge)
        selected_index = 2
        archived_children = tuple(child.name for index, child in enumerate(children) if index != selected_index)

        selection = selection_module._select_retained_candidate_set(
            mg,
            children[selected_index].name,
            parent=mg.ground,
            archived=archived_children,
        )
        selected_world = mg._world_storage().read_world(selection.parent_world_after)
        outcomes = {
            (str(outcome["candidate"]), str(outcome["outcome"]))
            for outcome in selected_world.operation_final["candidate_outcomes"]
        }
        expected_outcomes = {
            (
                result.handoff.candidate_head,
                "selected" if index == selected_index else "archived",
            )
            for index, result in enumerate(seal_results)
        }

        assert outcomes == expected_outcomes
        assert selected_world.transition["input_world"] == parent_world_before
        assert selected_world.transition["archived_handoff_refs"] == [
            seal_results[index].handoff.handoff_ref for index in (0, 1, 3)
        ]
        assert selection.settlement.action == "selected"
        assert read_retained_output_settlement(mg.store, selection.settlement.settlement_ref) == selection.settlement
        assert selected_world.snapshot.head_for("workspace").head == seal_results[selected_index].handoff.candidate_head
        assert selected_world.snapshot.head_for("workspace").head not in {
            result.handoff.candidate_head for index, result in enumerate(seal_results) if index != selected_index
        }
        assert _read_world_workspace_file(mg, selection.parent_world_after, "candidate.txt") == (
            b"candidate 2\n",
            0o100644,
        )

        before_cleanup = {row.scope_name: row for row in mg.list_retained_outputs(parent=mg.ground)}
        assert before_cleanup[children[selected_index].name].state == "selected"
        for index, child in enumerate(children):
            if index == selected_index:
                continue
            assert before_cleanup[child.name].state == "unconsumed"
            settlement_ref = retained_output_settlement_ref(
                scope_name=seal_results[index].handoff.scope_name,
                scope_instance_id=seal_results[index].handoff.scope_instance_id,
                binding=seal_results[index].handoff.binding,
                candidate_id=seal_results[index].handoff.candidate_id,
            )
            assert read_retained_output_settlement(mg.store, settlement_ref, missing_ok=True) is None
            assert mg.read_retained_workspace_file(child.name, "candidate.txt") == (
                f"candidate {index}\n".encode(),
                0o100644,
            )
    finally:
        monkeypatch.setattr(mg.store, "merge", original_store_merge)
        mg.deactivate(warn_on_open_scopes=False)

    fresh = _make_mg(workspace)
    try:
        assert fresh.world_oid(fresh.ground) == selection.parent_world_after
        release_result = fresh.release_retained_output("capstone-child-0", parent=fresh.ground)
        discard_result_1 = fresh.discard_retained_output("capstone-child-1", parent=fresh.ground)
        discard_result_3 = fresh.discard_retained_output("capstone-child-3", parent=fresh.ground)
        assert release_result.settlement.action == "released"
        assert discard_result_1.settlement.action == "discarded"
        assert discard_result_3.settlement.action == "discarded"
        after_cleanup = {row.scope_name: row for row in fresh.list_retained_outputs(parent=fresh.ground)}
        assert after_cleanup["capstone-child-0"].state == "released"
        assert after_cleanup["capstone-child-1"].state == "discarded"
        assert after_cleanup["capstone-child-2"].state == "selected"
        assert after_cleanup["capstone-child-3"].state == "discarded"
        assert fresh.read_retained_workspace_file("capstone-child-0", "candidate.txt") == (
            b"candidate 0\n",
            0o100644,
        )
        assert fresh.read_retained_workspace_file("capstone-child-1", "candidate.txt") == (
            b"candidate 1\n",
            0o100644,
        )
        assert fresh.read_retained_workspace_file("capstone-child-3", "candidate.txt") == (
            b"candidate 3\n",
            0o100644,
        )
    finally:
        fresh.deactivate(warn_on_open_scopes=False)


def test_private_candidate_set_selection_recovery_requires_same_archived_candidates(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    original_write = selection_module.write_retained_output_settlement
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "cohort-recovery-parent")
        child_a = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="cohort-recovery-child-a",
            operation_suffix="recovery-a",
            path="candidate.txt",
            content=b"candidate A\n",
        )
        seal_a = mg.seal(child_a)
        child_b = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="cohort-recovery-child-b",
            operation_suffix="recovery-b",
            path="candidate.txt",
            content=b"candidate B\n",
        )
        seal_b = mg.seal(child_b)
        child_c = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="cohort-recovery-child-c",
            operation_suffix="recovery-c",
            path="candidate.txt",
            content=b"candidate C\n",
        )
        seal_c = mg.seal(child_c)
        assert seal_a.handoff.parent_basis_world_oid == seal_b.handoff.parent_basis_world_oid
        assert seal_a.handoff.parent_basis_world_oid == seal_c.handoff.parent_basis_world_oid
        settlement_ref = retained_output_settlement_ref(
            scope_name=seal_a.handoff.scope_name,
            scope_instance_id=seal_a.handoff.scope_instance_id,
            binding=seal_a.handoff.binding,
            candidate_id=seal_a.handoff.candidate_id,
        )
        failed = False

        def fail_once_write(*args: object, **kwargs: object) -> object:
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("simulated crash after retained candidate-set publication")
            return original_write(*args, **kwargs)

        monkeypatch.setattr(selection_module, "write_retained_output_settlement", fail_once_write)
        with pytest.raises(RuntimeError, match="simulated crash after retained candidate-set publication"):
            selection_module._select_retained_candidate_set(
                mg,
                child_a.name,
                parent=parent,
                archived=(child_b.name,),
            )
        parent_world_after_publication = mg.world_oid(parent)
        assert parent_world_after_publication is not None
        assert read_retained_output_settlement(mg.store, settlement_ref, missing_ok=True) is None

        monkeypatch.setattr(selection_module, "write_retained_output_settlement", original_write)
        with pytest.raises(InvalidRepositoryStateError, match="binding 'workspace' advanced"):
            selection_module._select_retained_candidate_set(
                mg,
                child_a.name,
                parent=parent,
                archived=(child_c.name,),
            )
        assert read_retained_output_settlement(mg.store, settlement_ref, missing_ok=True) is None

        recovered = selection_module._select_retained_candidate_set(
            mg,
            child_a.name,
            parent=parent,
            archived=(child_b.name,),
        )
        assert recovered.parent_world_after == parent_world_after_publication
        assert read_retained_output_settlement(mg.store, settlement_ref) == recovered.settlement
    finally:
        monkeypatch.setattr(selection_module, "write_retained_output_settlement", original_write)
        mg.deactivate(warn_on_open_scopes=False)


def test_receipt_only_settlement_recovers_missing_private_candidate_set_selection(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    original_write = selection_module.write_retained_output_settlement
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "cohort-receipt-recovery-parent")
        child_a = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="cohort-receipt-recovery-child-a",
            operation_suffix="receipt-recovery-a",
            path="candidate.txt",
            content=b"candidate A\n",
        )
        seal_a = mg.seal(child_a)
        child_b = _produce_named_child_workspace_output(
            mg,
            parent,
            child_name="cohort-receipt-recovery-child-b",
            operation_suffix="receipt-recovery-b",
            path="candidate.txt",
            content=b"candidate B\n",
        )
        mg.seal(child_b)
        settlement_ref = retained_output_settlement_ref(
            scope_name=seal_a.handoff.scope_name,
            scope_instance_id=seal_a.handoff.scope_instance_id,
            binding=seal_a.handoff.binding,
            candidate_id=seal_a.handoff.candidate_id,
        )
        failed = False

        def fail_once_write(*args: object, **kwargs: object) -> object:
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("simulated crash after retained candidate-set publication")
            return original_write(*args, **kwargs)

        monkeypatch.setattr(selection_module, "write_retained_output_settlement", fail_once_write)
        with pytest.raises(RuntimeError, match="simulated crash after retained candidate-set publication"):
            selection_module._select_retained_candidate_set(
                mg,
                child_a.name,
                parent=parent,
                archived=(child_b.name,),
            )
        assert read_retained_output_settlement(mg.store, settlement_ref, missing_ok=True) is None

        monkeypatch.setattr(selection_module, "write_retained_output_settlement", original_write)
        with pytest.raises(InvalidRepositoryStateError, match="retained output is already settled"):
            mg.release_retained_output(child_a.name, parent=parent)
        recovered = read_retained_output_settlement(mg.store, settlement_ref)
        assert recovered.action == "selected"
    finally:
        monkeypatch.setattr(selection_module, "write_retained_output_settlement", original_write)
        mg.deactivate(warn_on_open_scopes=False)


def test_select_retained_output_recovers_ground_parent_missing_settlement_after_reactivation(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    child_name: str
    parent_world_after_publication: str
    settlement_ref: str
    original_write = selection_module.write_retained_output_settlement
    try:
        child = _produce_ground_child_workspace_output(mg)
        child_name = child.name
        seal_result = mg.seal(child)
        retained_rows = mg.list_retained_outputs(parent=mg.ground, binding="workspace")
        assert len(retained_rows) == 1
        assert retained_rows[0].parent_scope_name == mg.ground.name
        assert retained_rows[0].parent_scope_instance_id is None
        settlement_ref = retained_output_settlement_ref(
            scope_name=seal_result.handoff.scope_name,
            scope_instance_id=seal_result.handoff.scope_instance_id,
            binding=seal_result.handoff.binding,
            candidate_id=seal_result.handoff.candidate_id,
        )
        failed = False

        def fail_once_write(*args: object, **kwargs: object) -> object:
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("simulated crash after retained output publication")
            return original_write(*args, **kwargs)

        monkeypatch.setattr(selection_module, "write_retained_output_settlement", fail_once_write)
        with pytest.raises(RuntimeError, match="simulated crash after retained output publication"):
            mg.select_retained_output(child.name, parent=mg.ground)
        published_world = mg.world_oid(mg.ground)
        assert published_world is not None
        parent_world_after_publication = published_world
        assert read_retained_output_settlement(mg.store, settlement_ref, missing_ok=True) is None
    finally:
        mg.deactivate(warn_on_open_scopes=False)

    monkeypatch.setattr(selection_module, "write_retained_output_settlement", original_write)
    fresh = _make_mg(workspace)
    try:
        recovered = fresh.select_retained_output(child_name, parent=fresh.ground)

        assert recovered.parent_world_after == parent_world_after_publication
        assert fresh.world_oid(fresh.ground) == parent_world_after_publication
        assert read_retained_output_settlement(fresh.store, settlement_ref) == recovered.settlement
        assert _read_world_workspace_file(fresh, recovered.parent_world_after, "ground-child.txt") == (
            b"ground child output\n",
            0o100644,
        )
    finally:
        fresh.deactivate(warn_on_open_scopes=False)


def test_select_retained_output_recovers_missing_settlement_after_parent_world_publish(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        parent, child = _produce_child_workspace_output(mg)
        seal_result = mg.seal(child)
        settlement_ref = retained_output_settlement_ref(
            scope_name=seal_result.handoff.scope_name,
            scope_instance_id=seal_result.handoff.scope_instance_id,
            binding=seal_result.handoff.binding,
            candidate_id=seal_result.handoff.candidate_id,
        )
        original_write = selection_module.write_retained_output_settlement
        failed = False

        def fail_once_write(*args: object, **kwargs: object) -> object:
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("simulated crash after retained output publication")
            return original_write(*args, **kwargs)

        monkeypatch.setattr(selection_module, "write_retained_output_settlement", fail_once_write)
        with pytest.raises(RuntimeError, match="simulated crash after retained output publication"):
            mg.select_retained_output(child.name, parent=parent)
        parent_world_after_publication = mg.world_oid(parent)
        assert parent_world_after_publication is not None
        assert read_retained_output_settlement(mg.store, settlement_ref, missing_ok=True) is None

        monkeypatch.setattr(selection_module, "write_retained_output_settlement", original_write)
        recovered = mg.select_retained_output(child.name, parent=parent)

        assert recovered.parent_world_after == parent_world_after_publication
        assert mg.world_oid(parent) == parent_world_after_publication
        assert read_retained_output_settlement(mg.store, settlement_ref) == recovered.settlement
        assert _read_world_workspace_file(mg, recovered.parent_world_after, "child.txt") == (
            b"child output\n",
            0o100644,
        )
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_select_retained_output_missing_settlement_recovery_requires_parent_authority_ref(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        parent, child = _produce_child_workspace_output(mg)
        parent_world_before_publication = mg.world_oid(parent)
        assert parent_world_before_publication is not None
        seal_result = mg.seal(child)
        settlement_ref = retained_output_settlement_ref(
            scope_name=seal_result.handoff.scope_name,
            scope_instance_id=seal_result.handoff.scope_instance_id,
            binding=seal_result.handoff.binding,
            candidate_id=seal_result.handoff.candidate_id,
        )
        original_write = selection_module.write_retained_output_settlement
        failed = False

        def fail_once_write(*args: object, **kwargs: object) -> object:
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("simulated crash after retained output publication")
            return original_write(*args, **kwargs)

        monkeypatch.setattr(selection_module, "write_retained_output_settlement", fail_once_write)
        with pytest.raises(RuntimeError, match="simulated crash after retained output publication"):
            mg.select_retained_output(child.name, parent=parent)
        parent_world_after_publication = mg.world_oid(parent)
        assert parent_world_after_publication is not None
        assert parent_world_after_publication != parent_world_before_publication
        assert read_retained_output_settlement(mg.store, settlement_ref, missing_ok=True) is None

        manager = mg._world_storage()
        create_or_update_reference(
            manager.world_store.repo,
            parent.ref,
            pygit2.Oid(hex=parent_world_before_publication),
            force=True,
        )
        assert mg.world_oid(parent) == parent_world_before_publication

        monkeypatch.setattr(selection_module, "write_retained_output_settlement", original_write)
        with pytest.raises(InvalidRepositoryStateError, match="not protected by target ref"):
            mg.select_retained_output(child.name, parent=parent)

        assert mg.world_oid(parent) == parent_world_before_publication
        assert read_retained_output_settlement(mg.store, settlement_ref, missing_ok=True) is None
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_select_retained_output_missing_settlement_recovery_bypasses_fresh_admission(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        parent, child = _produce_child_workspace_output(mg)
        seal_result = mg.seal(child)
        settlement_ref = retained_output_settlement_ref(
            scope_name=seal_result.handoff.scope_name,
            scope_instance_id=seal_result.handoff.scope_instance_id,
            binding=seal_result.handoff.binding,
            candidate_id=seal_result.handoff.candidate_id,
        )
        original_write = selection_module.write_retained_output_settlement
        failed = False

        def fail_once_write(*args: object, **kwargs: object) -> object:
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("simulated crash after retained output publication")
            return original_write(*args, **kwargs)

        monkeypatch.setattr(selection_module, "write_retained_output_settlement", fail_once_write)
        with pytest.raises(RuntimeError, match="simulated crash after retained output publication"):
            mg.select_retained_output(child.name, parent=parent)
        parent_world_after_publication = mg.world_oid(parent)
        assert parent_world_after_publication is not None
        assert read_retained_output_settlement(mg.store, settlement_ref, missing_ok=True) is None

        _publish_sibling_group_blocker(mg, group_id="sg-recoverok")
        monkeypatch.setattr(selection_module, "write_retained_output_settlement", original_write)
        recovered = mg.select_retained_output(child.name, parent=parent)

        assert recovered.parent_world_after == parent_world_after_publication
        assert mg.world_oid(parent) == parent_world_after_publication
        assert read_retained_output_settlement(mg.store, settlement_ref) == recovered.settlement
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_seal_uses_workspace_producer_when_later_operation_carries_head_forward(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        parent, child = _produce_child_workspace_output(mg)
        workspace_world = mg.world_oid(child)
        assert workspace_world is not None
        manager = mg._world_storage()
        workspace_head = manager.read_world(workspace_world).snapshot.head_for("workspace")

        mg.exec("trace", "append", scope=child, payload=_trace_payload("frontier:after-workspace"))

        carried_world = mg.world_oid(child)
        assert carried_world is not None
        assert carried_world != workspace_world
        carried = manager.read_world(carried_world)
        trace_operation_id = carried.operation_final["operation_id"]
        assert carried.snapshot.head_for("workspace") == workspace_head

        result = mg.seal(child)

        assert result.parent == parent
        assert result.handoff.output_world_oid == carried_world
        assert result.handoff.producer_operation_id != trace_operation_id
        assert result.handoff.producer_operation_id.startswith("wv_python_runtime_capture_seal-child-run_")
        assert mg.read_retained_workspace_file(child.name, "child.txt") == (b"child output\n", 0o100644)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_retained_workspace_read_rejects_missing_v2_scope_ref(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        _parent, child = _produce_child_workspace_output(mg)
        mg.seal(child)
        manager = mg._world_storage()
        manager.world_store.repo.references[child.ref].delete()

        with pytest.raises(InvalidRepositoryStateError, match="retained v2 scope ref is missing"):
            mg.retained_workspace_handle(child.name)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_retained_workspace_read_rejects_v2_scope_ref_target_mismatch(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        _parent, child = _produce_child_workspace_output(mg)
        result = mg.seal(child)
        parent_world_oid = result.handoff.parent_basis_world_oid
        manager = mg._world_storage()
        create_or_update_reference(
            manager.world_store.repo,
            child.ref,
            pygit2.Oid(hex=parent_world_oid),
            force=True,
        )

        with pytest.raises(InvalidRepositoryStateError, match="target disagrees"):
            mg.retained_workspace_handle(child.name)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_retained_workspace_read_rejects_missing_candidate_ref(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        _parent, child = _produce_child_workspace_output(mg)
        result = mg.seal(child)
        manager = mg._world_storage()
        substrate = manager.store(result.handoff.store_id)
        substrate.repo.references[result.handoff.candidate_ref].delete()

        with pytest.raises(InvalidRepositoryStateError, match="candidate ref"):
            mg.retained_workspace_handle(child.name)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_retained_workspace_read_rejects_candidate_ref_target_mismatch(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        _parent, child = _produce_child_workspace_output(mg)
        result = mg.seal(child)
        substrate = mg._world_storage().store(result.handoff.store_id)
        wrong_oid = substrate.repo.create_blob(b"wrong retained candidate target")
        create_or_update_reference(
            substrate.repo,
            result.handoff.candidate_ref,
            wrong_oid,
            force=True,
        )

        with pytest.raises(InvalidRepositoryStateError, match="candidate ref target disagrees"):
            mg.retained_workspace_handle(child.name)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_retained_workspace_read_rejects_handoff_tuple_not_selected_head_provenance(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        _parent, child = _produce_child_workspace_output(mg)
        mg.seal(child)
        loaded = read_seal_handoff(mg.store, child)
        assert loaded is not None
        tampered_tree_oid = "f" * 40
        if loaded.candidate_tuple.plan.git_tree_oid == tampered_tree_oid:
            tampered_tree_oid = "e" * 40
        tampered_tuple = replace(
            loaded.candidate_tuple,
            plan=replace(loaded.candidate_tuple.plan, git_tree_oid=tampered_tree_oid),
        )
        tampered_handoff = replace(
            loaded.handoff,
            candidate_tuple_digest=tampered_tuple.tuple_digest(),
        )
        mg.store._repo.references[loaded.handoff.handoff_ref].delete()
        write_seal_handoff(mg.store, handoff=tampered_handoff, candidate_tuple=tampered_tuple)

        with pytest.raises(InvalidRepositoryStateError, match="selected-head provenance"):
            mg.retained_workspace_handle(child.name)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_retained_workspace_read_rejects_handoff_candidate_alias_not_selected_head_provenance(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        _parent, child = _produce_child_workspace_output(mg)
        mg.seal(child)
        loaded = read_seal_handoff(mg.store, child)
        assert loaded is not None
        original_tuple = loaded.candidate_tuple
        wrong_operation_id = f"{original_tuple.candidate.operation_id}-alias"
        wrong_candidate_ref = candidate_ref(
            wrong_operation_id,
            original_tuple.candidate.binding,
            original_tuple.candidate.candidate_id,
        )
        substrate = mg._world_storage().store(original_tuple.candidate.store_id)
        create_or_update_reference(
            substrate.repo,
            wrong_candidate_ref,
            pygit2.Oid(hex=original_tuple.candidate.head),
            force=True,
        )
        tampered_preparation = replace(original_tuple.preparation, operation_id=wrong_operation_id)
        tampered_tuple = replace(
            original_tuple,
            candidate=replace(
                original_tuple.candidate,
                operation_id=wrong_operation_id,
                ref=wrong_candidate_ref,
            ),
            preparation=tampered_preparation,
            candidate_commit=replace(
                original_tuple.candidate_commit,
                operation_id=wrong_operation_id,
                candidate_ref=wrong_candidate_ref,
                revision_preparation_digest=tampered_preparation.revision_preparation_digest(),
            ),
        )
        tampered_handoff = replace(
            loaded.handoff,
            producer_operation_id=wrong_operation_id,
            candidate_ref=wrong_candidate_ref,
            candidate_tuple_digest=tampered_tuple.tuple_digest(),
        )
        mg.store._repo.references[loaded.handoff.handoff_ref].delete()
        write_seal_handoff(mg.store, handoff=tampered_handoff, candidate_tuple=tampered_tuple)

        with pytest.raises(InvalidRepositoryStateError, match="selected-head provenance"):
            mg.retained_workspace_handle(child.name)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_retained_workspace_read_rejects_parent_ref_mismatch(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        _parent, child = _produce_child_workspace_output(mg)
        result = mg.seal(child)
        assert result.handoff.parent_ref != mg.ground.ref
        snapshot = mg.store.require_scope_registry_projection()
        tampered = tuple(
            replace(entry, parent_ref=mg.ground.ref) if entry.name == child.name else entry
            for entry in snapshot.entries
        )
        assert mg.store.publish_scope_registry_projection(entries=tampered, expected_head_oid=snapshot.head_oid)
        assert not mg.store.scope_registry_projection_mismatches()

        with pytest.raises(InvalidRepositoryStateError, match="parent_ref disagrees"):
            mg.retained_workspace_handle(child.name)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_seal_closes_isolated_filesystem_runtime_layer(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        _parent, child = _produce_child_workspace_output(mg, child_hints={"isolated": True})
        filesystem = next(
            substrate for substrate in mg.lifecycle_substrates if isinstance(substrate, FilesystemSubstrate)
        )
        backend = filesystem._backend
        assert isinstance(backend, MockOverlayBackend)
        assert filesystem.has_overlay_layer(child.name)
        assert backend.has_layer(child.name)

        mg.seal(child)

        assert not filesystem.has_overlay_layer(child.name)
        assert not backend.has_layer(child.name)
        assert child.name in backend.discarded
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_seal_retains_ordinary_isolated_filesystem_write_after_runtime_close(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "seal-parent")
        child = mg.fork(parent, "seal-child", hints={"isolated": True})
        filesystem = next(
            substrate for substrate in mg.lifecycle_substrates if isinstance(substrate, FilesystemSubstrate)
        )
        backend = filesystem._backend
        assert isinstance(backend, MockOverlayBackend)
        assert filesystem.has_overlay_layer(child.name)

        mg.exec("filesystem", "write", scope=child, path="ordinary.txt", content=b"ordinary output\n")
        assert backend.read_file(child.name, "ordinary.txt") == b"ordinary output\n"
        pre_seal_world = mg.world_oid(child)
        assert pre_seal_world is not None

        result = mg.seal(child)

        assert result.handoff.output_world_oid != pre_seal_world
        assert "ordinary.txt" in result.handoff.changed_paths
        assert not filesystem.has_overlay_layer(child.name)
        assert not backend.has_layer(child.name)
        assert mg.read_retained_workspace_file(child.name, "ordinary.txt") == (b"ordinary output\n", 0o100644)
    finally:
        mg.deactivate(warn_on_open_scopes=False)

    fresh = _make_mg(workspace)
    try:
        assert fresh.read_retained_workspace_file("seal-child", "ordinary.txt") == (b"ordinary output\n", 0o100644)
    finally:
        fresh.deactivate(warn_on_open_scopes=False)


def test_failed_seal_runtime_close_recovers_before_retained_registry_publish(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    failing_close = _CloseRetainedSubstrate(fail=True)
    mg = _make_mg(workspace, extra_substrates=(failing_close,))
    try:
        _parent, child = _produce_child_workspace_output(mg)

        with pytest.raises(RuntimeError, match="Scope remains active for recovery"):
            mg.seal(child)

        assert read_seal_handoff(mg.store, child) is not None
        assert mg.store.scope_registry_entry(child.name, status="retained") is None
        assert mg.lookup_scope(child.name) == child
        assert mg._lifecycle_run is not None
        assert mg._lifecycle_run.phase == "seal_runtime_close"
        assert "filesystem" in mg._lifecycle_run.completed_substrates
    finally:
        with contextlib.suppress(Exception):
            mg.deactivate(warn_on_open_scopes=False)

    recovered_close = _CloseRetainedSubstrate()
    recovered = _make_mg(workspace, recover_lifecycle="resume", extra_substrates=(recovered_close,))
    try:
        retained = recovered.store.scope_registry_entry("seal-child", status="retained")
        assert retained is not None
        assert recovered.lookup_scope("seal-child") is None
        assert recovered_close.calls == [("seal-child", "seal-parent")]
        assert recovered.read_retained_workspace_file("seal-child", "child.txt") == (b"child output\n", 0o100644)
    finally:
        recovered.deactivate(warn_on_open_scopes=False)


def test_failed_seal_runtime_close_recovers_via_direct_recover_lifecycle(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    failing_close = _CloseRetainedSubstrate(fail=True)
    mg = _make_mg(workspace, extra_substrates=(failing_close,))
    try:
        _parent, child = _produce_child_workspace_output(mg)

        with pytest.raises(RuntimeError, match="Scope remains active for recovery"):
            mg.seal(child)

        assert mg.recover_lifecycle() == child.name
        assert mg.store.scope_registry_entry(child.name, status="retained") is not None
        assert mg.lookup_scope(child.name) is None
        assert failing_close.calls == [(child.name, "seal-parent"), (child.name, "seal-parent")]
        assert mg.read_retained_workspace_file(child.name, "child.txt") == (b"child output\n", 0o100644)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_interrupted_seal_recovery_requires_seal_flag(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    failing_close = _CloseRetainedSubstrate(fail=True)
    mg = _make_mg(workspace, extra_substrates=(failing_close,))
    try:
        _parent, child = _produce_child_workspace_output(mg)

        with pytest.raises(RuntimeError, match="Scope remains active for recovery"):
            mg.seal(child)
    finally:
        with contextlib.suppress(Exception):
            mg.deactivate(warn_on_open_scopes=False)

    monkeypatch.delenv(SEAL_AND_SELECT_ENV, raising=False)
    with pytest.raises(InvalidRepositoryStateError, match=SEAL_AND_SELECT_ENV):
        _make_mg(workspace, recover_lifecycle="resume", extra_substrates=(_CloseRetainedSubstrate(),))

    store = Store(str(workspace / ".vcscore"))
    assert store.scope_registry_entry(child.name, status="retained") is None
    assert read_seal_handoff(store, child) is not None

    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    recovered_close = _CloseRetainedSubstrate()
    recovered = _make_mg(workspace, recover_lifecycle="resume", extra_substrates=(recovered_close,))
    try:
        assert recovered.store.scope_registry_entry(child.name, status="retained") is not None
        assert recovered_close.calls == [(child.name, "seal-parent")]
        assert recovered.read_retained_workspace_file(child.name, "child.txt") == (b"child output\n", 0o100644)
    finally:
        recovered.deactivate(warn_on_open_scopes=False)


def test_interrupted_seal_recovers_when_handoff_write_failed(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        _parent, child = _produce_child_workspace_output(mg)
        original_write = seal_module.write_prepared_seal_handoff
        failed = False

        def fail_once_write(*args: object, **kwargs: object) -> object:
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("simulated crash before seal handoff write")
            return original_write(*args, **kwargs)

        monkeypatch.setattr(seal_module, "write_prepared_seal_handoff", fail_once_write)
        with pytest.raises(RuntimeError, match="simulated crash before seal handoff write"):
            mg.seal(child)

        assert failed
        assert read_seal_handoff(mg.store, child, missing_ok=True) is None
        assert mg._lifecycle_run is not None
        assert mg._lifecycle_run.phase == "seal_handoff"
    finally:
        with contextlib.suppress(Exception):
            mg.deactivate(warn_on_open_scopes=False)

    recovered = _make_mg(workspace, recover_lifecycle="resume")
    try:
        retained = recovered.store.scope_registry_entry(child.name, status="retained")
        assert retained is not None
        assert recovered.lookup_scope(child.name) is None
        assert read_seal_handoff(recovered.store, child) is not None
        assert recovered.read_retained_workspace_file(child.name, "child.txt") == (b"child output\n", 0o100644)
    finally:
        recovered.deactivate(warn_on_open_scopes=False)


def test_public_retained_workspace_read_requires_seal_flag(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        _parent, child = _produce_child_workspace_output(mg)
        mg.seal(child)
    finally:
        mg.deactivate(warn_on_open_scopes=False)

    monkeypatch.delenv(SEAL_AND_SELECT_ENV, raising=False)
    fresh = _make_mg(workspace)
    try:
        with pytest.raises(Exception, match=SEAL_AND_SELECT_ENV):
            fresh.retained_workspace_handle("seal-child")
        with pytest.raises(Exception, match=SEAL_AND_SELECT_ENV):
            fresh.read_retained_workspace_file("seal-child", "child.txt")
    finally:
        fresh.deactivate(warn_on_open_scopes=False)


def test_public_retained_workspace_read_rejects_registry_mismatch(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        _parent, child = _produce_child_workspace_output(mg)
        mg.seal(child)
        mg.store.discard(child)

        with pytest.raises(Exception, match="scope-registry mismatches"):
            mg.retained_workspace_handle("seal-child")
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_interrupted_seal_recovers_from_persisted_handoff(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        _parent, child = _produce_child_workspace_output(mg)
        original_publish = mg.store.publish_scope_registry_projection
        failed = False

        def fail_once_publish(*args: object, **kwargs: object) -> bool:
            nonlocal failed
            if not failed:
                failed = True
                return False
            return original_publish(*args, **kwargs)

        monkeypatch.setattr(mg.store, "publish_scope_registry_projection", fail_once_publish)
        with pytest.raises(RuntimeError, match="Failed to publish"):
            mg.seal(child)
        assert failed
        assert read_seal_handoff(mg.store, child) is not None
    finally:
        with contextlib.suppress(Exception):
            mg.deactivate(warn_on_open_scopes=False)

    recovered = _make_mg(workspace, recover_lifecycle="resume")
    try:
        retained = recovered.store.scope_registry_entry("seal-child", status="retained")
        assert retained is not None
        assert recovered.lookup_scope("seal-child") is None
        assert recovered.read_retained_workspace_file("seal-child", "child.txt") == (b"child output\n", 0o100644)
    finally:
        recovered.deactivate(warn_on_open_scopes=False)


def test_interrupted_seal_recovers_after_retained_registry_publish(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_mg(workspace)
    try:
        _parent, child = _produce_child_workspace_output(mg)
        original_publish_status = lifecycle._publish_scope_registry_status_locked

        def publish_then_crash(*args: object, **kwargs: object) -> None:
            original_publish_status(*args, **kwargs)
            raise RuntimeError("simulated crash after retained registry publish")

        with monkeypatch.context() as patch_context:
            patch_context.setattr(lifecycle, "_publish_scope_registry_status_locked", publish_then_crash)
            with pytest.raises(RuntimeError, match="simulated crash after retained registry publish"):
                mg.seal(child)

        assert read_seal_handoff(mg.store, child) is not None
        assert mg.store.scope_registry_entry(child.name, status="retained") is not None
        assert mg._lifecycle_run is not None
        assert mg._lifecycle_run.phase == "seal_registry"
    finally:
        with contextlib.suppress(Exception):
            mg.deactivate(warn_on_open_scopes=False)

    recovered = _make_mg(workspace, recover_lifecycle="resume")
    try:
        retained = recovered.store.scope_registry_entry("seal-child", status="retained")
        assert retained is not None
        assert recovered.lookup_scope("seal-child") is None
        assert not recovered.store.scope_registry_projection_mismatches()
        assert recovered.read_retained_workspace_file("seal-child", "child.txt") == (b"child output\n", 0o100644)
    finally:
        recovered.deactivate(warn_on_open_scopes=False)
