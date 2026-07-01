from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import vcs_core._vcscore_lifecycle as lifecycle
from vcs_core import VcsCore
from vcs_core._authority import (
    AuthorityDecision,
    AuthzMatchView,
    GitRepoAuthorityRequest,
    _authority_settlement_pending_path,
    read_pending_authority_settlement,
)
from vcs_core._command_admission import CommandAdmissionError
from vcs_core._errors import InvalidRepositoryStateError, WorkspaceAuthorityRecoveryRequiredError
from vcs_core._permission_plan_evidence import permission_plan_digest
from vcs_core._workspace_authority import (
    WorkspaceAuthorityPending,
    pending_workspace_authority_records,
    write_pending_workspace_authority,
)
from vcs_core.types import ScopeInfo

from ...support.builders import make_marker_filesystem_vcscore
from ...support.overlays import MockOverlayBackend

BINDINGS = {"backend": "backend", "docs": "docs"}
EFFECTIVE_MATCH_DIGEST = "test-carrier-diff-effective-match"
AUTHORITY_SURFACE_PLAN_DIGEST = "test-carrier-diff-surface-plan"
PERMISSION_PLAN_DESCRIPTOR = {
    "schema": "shepherd.permission-plan.v1",
    "fallback": "enforce",
    "assignments": [
        {
            "monitor": "carrier_check_at_commit",
            "timing": "commit",
            "route": "carrier_diff",
            "completeness_basis": "test prepared filesystem carrier diff evidence",
            "tamper_basis": "test coordinator-owned authority merge",
            "confinement": None,
            "evidence": {
                "effective_match_digest": EFFECTIVE_MATCH_DIGEST,
                "authority_surface_plan_digest": AUTHORITY_SURFACE_PLAN_DIGEST,
            },
        }
    ],
}
PERMISSION_PLAN_DIGEST = permission_plan_digest(PERMISSION_PLAN_DESCRIPTOR)


class _FailOnceDiscardBackend(MockOverlayBackend):
    def __init__(self) -> None:
        super().__init__()
        self.failures_remaining = 1

    def discard_layer(self, scope_id: str) -> None:
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise RuntimeError(f"simulated authority discard failure for {scope_id}")
        super().discard_layer(scope_id)


def _authz_view_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "domain": "gitrepo.v0",
        "kind": "gitrepo.file_patch",
        "binding_ref": "backend",
        "action": "git_repo.file_patch",
        "path": "src/app/main.py",
        "mutates": True,
        "reversibility": "reversible",
        "control_plane": False,
        "monitor_basis": "carrier_check_at_commit",
        "route": "carrier_diff",
        "classification_basis": "effect_record",
    }
    kwargs.update(overrides)
    return kwargs


@pytest.mark.parametrize(
    "overrides",
    [
        {"mutates": "yes"},
        {"control_plane": "false"},
        {"classification_basis": "maybe"},
        {"domain": "other.v0"},
        {"path": "", "kind": "gitrepo.file_patch", "mutates": True},
    ],
)
def test_authz_match_view_rejects_invalid_producer_fields(overrides: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        AuthzMatchView(**_authz_view_kwargs(**overrides)) # type: ignore[arg-type]


def _decide(request: GitRepoAuthorityRequest) -> AuthorityDecision:
    view = request.match_view
    if view.binding_ref == "backend" and view.path.startswith("src/app/"):
        return AuthorityDecision(outcome="allowed", reason_code="backend_src_app_allowed")
    if view.binding_ref == "docs" and view.mutates:
        return AuthorityDecision(outcome="denied", reason_code="docs_read_only")
    return AuthorityDecision(outcome="refused", reason_code="outside_fixture_authority")


def _make_authority_mg(
    tmp_path: Path,
    backend: MockOverlayBackend | None = None,
) -> tuple[VcsCore, MockOverlayBackend]:
    effective_backend = backend or MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path / "ws",
        declarative=False,
        backend=effective_backend,
        activate=True,
    )
    return mg, effective_backend


def _child(mg: VcsCore, name: str) -> ScopeInfo:
    return mg.fork(mg.ground, name, hints={"isolated": True})


def _write_workspace_authority_pending(mg: VcsCore, scope: ScopeInfo, operation_id: str) -> None:
    source_commit = mg.store.resolve_to_commit(scope.ref)
    write_pending_workspace_authority(
        mg._repo_path,
        WorkspaceAuthorityPending(
            operation_id=operation_id,
            source_operation_id=f"source_{operation_id}",
            driver_command="scan",
            scope_name=scope.name,
            scope_ref=scope.ref,
            scope_instance_id=scope.instance_id,
            scope_world_id=scope.world_id,
            expected_input_world_oid=mg._current_v2_world_oid(mg._world_storage(), scope.ref),
            scalar_source_commit=str(source_commit.id) if source_commit is not None else None,
        ).with_update(phase="scalar_committed"),
    )


def _authority_effects(history: Any) -> list[dict[str, object]]:
    return [commit.metadata for commit in history.commits if str(commit.metadata.get("type", "")).startswith(("Authority", "RetainedOutput", "Prepared"))]


def _authority_plan_kwargs() -> dict[str, object]:
    return {
        "effective_match_digest": EFFECTIVE_MATCH_DIGEST,
        "authority_surface_plan_digest": AUTHORITY_SURFACE_PLAN_DIGEST,
        "permission_plan_digest": PERMISSION_PLAN_DIGEST,
        "permission_plan_descriptor": PERMISSION_PLAN_DESCRIPTOR,
    }


def _merge_with_authority(mg: VcsCore, *args: Any, **kwargs: Any) -> Any:
    return mg.merge_with_authority(*args, **{**_authority_plan_kwargs(), **kwargs})


def test_authority_allowed_candidate_merges_and_records_evidence(tmp_path: Path) -> None:
    mg, backend = _make_authority_mg(tmp_path)
    try:
        parent_world_before = mg.world_oid(mg.ground)
        child = _child(mg, "allow")
        backend.write_file(child.name, "backend/src/app/main.py", b"ok\n")

        result = _merge_with_authority(mg, child, mg.ground, binding_roots=BINDINGS, decide=_decide)

        assert result.outcome == "allowed"
        assert result.settlement == "merged"
        assert result.permission_plan_digest == PERMISSION_PLAN_DIGEST
        assert result.permission_plan_descriptor == PERMISSION_PLAN_DESCRIPTOR
        assert result.parent_world_before == parent_world_before
        assert result.parent_world_after == mg.world_oid(mg.ground)
        assert mg.store.read_workspace_file(mg.ground.ref, "backend/src/app/main.py") == b"ok\n"
        assert backend.committed == [(child.name, "ground")]
        history = mg.resolve_operation_history(result.authority_operation_id, scope=mg.ground)
        effects = _authority_effects(history)
        assert sorted(effect["type"] for effect in effects) == [
            "AuthorityDecision",
            "PreparedAuthorityMerge",
        ]
        decision = next(effect for effect in effects if effect["type"] == "AuthorityDecision")
        assert decision["outcome"] == "allowed"
        assert decision["permission_plan_digest"] == PERMISSION_PLAN_DIGEST
        assert decision["permission_plan_descriptor"] == PERMISSION_PLAN_DESCRIPTOR
        assert decision["request"]["match_view"]["route"] == "carrier_diff"
        settlement_history = mg.resolve_operation_history(result.settlement_operation_id, scope=mg.ground)
        settlement = next(
            effect for effect in _authority_effects(settlement_history) if effect["type"] == "AuthoritySettlement"
        )
        assert settlement["authority_operation_id"] == result.authority_operation_id
        assert settlement["permission_plan_digest"] == PERMISSION_PLAN_DIGEST
        assert settlement["permission_plan_descriptor"] == PERMISSION_PLAN_DESCRIPTOR
        assert settlement["settlement"] == "merged"
        assert settlement["commit_outcome"] == "merged"
        assert settlement.get("parent_world_before") == result.parent_world_before
        assert settlement.get("parent_world_after") == result.parent_world_after
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_authority_denied_candidate_discards_without_adopted_file(tmp_path: Path) -> None:
    mg, backend = _make_authority_mg(tmp_path)
    try:
        parent_world_before = mg.world_oid(mg.ground)
        child = _child(mg, "deny")
        backend.write_file(child.name, "docs/forbidden.py", b"nope\n")

        result = _merge_with_authority(mg, child, mg.ground, binding_roots=BINDINGS, decide=_decide)

        assert result.outcome == "denied"
        assert result.settlement == "discarded"
        assert result.parent_world_before == parent_world_before
        assert result.parent_world_after == parent_world_before
        assert mg.world_oid(mg.ground) == parent_world_before
        assert mg.store.read_workspace_file(mg.ground.ref, "docs/forbidden.py") is None
        assert backend.committed == []
        assert backend.discarded == [child.name]
        archived = mg.archived_operations(world_id=child.world_id, operation_id=result.authority_operation_id)
        assert len(archived) == 1
        assert archived[0].archived_via == "discarded_world_ref"
        history = mg.resolve_operation_history(result.authority_operation_id, scope=child)
        effects = _authority_effects(history)
        assert any(effect["type"] == "AuthorityDecision" and effect["outcome"] == "denied" for effect in effects)
        assert not any(effect["type"] == "AuthoritySettlement" for effect in effects)
        settlement_history = mg.resolve_operation_history(result.settlement_operation_id, scope=mg.ground)
        settlement = next(
            effect for effect in _authority_effects(settlement_history) if effect["type"] == "AuthoritySettlement"
        )
        assert settlement["authority_operation_id"] == result.authority_operation_id
        assert settlement["settlement"] == "discarded"
        assert settlement["commit_outcome"] == "not_committed_denied"
        assert settlement.get("parent_world_before") == parent_world_before
        assert settlement.get("parent_world_after") == parent_world_before
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_authority_mixed_cohort_discards_all_candidates(tmp_path: Path) -> None:
    mg, backend = _make_authority_mg(tmp_path)
    try:
        child = _child(mg, "mixed")
        backend.write_file(child.name, "backend/src/app/good.py", b"ok\n")
        backend.write_file(child.name, "docs/bad.py", b"nope\n")

        result = _merge_with_authority(mg, child, mg.ground, binding_roots=BINDINGS, decide=_decide)

        assert result.outcome == "denied"
        assert mg.store.read_workspace_file(mg.ground.ref, "backend/src/app/good.py") is None
        assert mg.store.read_workspace_file(mg.ground.ref, "docs/bad.py") is None
        assert backend.committed == []
        assert backend.discarded == [child.name]
        assert [decision.outcome for decision in result.decisions] == ["allowed", "denied"]
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_authority_refuses_unknown_effect_and_discards(tmp_path: Path) -> None:
    mg, backend = _make_authority_mg(tmp_path)
    try:
        child = _child(mg, "refuse")
        backend.write_file(child.name, "secrets/key.txt", b"secret\n")

        result = _merge_with_authority(mg, child, mg.ground, binding_roots=BINDINGS, decide=_decide)

        assert result.outcome == "refused"
        assert mg.store.read_workspace_file(mg.ground.ref, "secrets/key.txt") is None
        assert backend.discarded == [child.name]
        assert result.decisions[0].reason_code == "outside_declared_bindings"
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_authority_refuses_drift_after_decision(tmp_path: Path) -> None:
    mg, backend = _make_authority_mg(tmp_path)
    try:
        child = _child(mg, "drift")
        backend.write_file(child.name, "backend/src/app/authorized.py", b"ok\n")
        injected = False

        def decide_with_straggler(request: GitRepoAuthorityRequest) -> AuthorityDecision:
            nonlocal injected
            decision = _decide(request)
            if not injected:
                backend.write_file(child.name, "backend/secret-exfil.py", b"late\n")
                injected = True
            return decision

        result = _merge_with_authority(mg, child, mg.ground, binding_roots=BINDINGS, decide=decide_with_straggler)

        assert result.outcome == "refused"
        assert result.settlement == "discarded"
        assert mg.store.read_workspace_file(mg.ground.ref, "backend/src/app/authorized.py") is None
        assert mg.store.read_workspace_file(mg.ground.ref, "backend/secret-exfil.py") is None
        assert backend.committed == []
        settlement_history = mg.resolve_operation_history(result.settlement_operation_id, scope=mg.ground)
        settlement = next(
            effect for effect in _authority_effects(settlement_history) if effect["type"] == "AuthoritySettlement"
        )
        assert settlement["reason_code"] == "substrate_no_drift_failed"
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_authority_does_not_record_merged_settlement_before_lifecycle_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mg, backend = _make_authority_mg(tmp_path)
    original_begin = lifecycle._begin_lifecycle_run
    try:
        child = _child(mg, "no-early-settlement")
        backend.write_file(child.name, "backend/src/app/main.py", b"ok\n")

        def fail_begin(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("simulated lifecycle handoff failure")

        monkeypatch.setattr(lifecycle, "_begin_lifecycle_run", fail_begin)

        with pytest.raises(RuntimeError, match="simulated lifecycle"):
            _merge_with_authority(
                mg,
                child,
                mg.ground,
                binding_roots=BINDINGS,
                decide=_decide,
                operation_id="op_no_early_settlement",
            )

        assert mg.store.read_workspace_file(mg.ground.ref, "backend/src/app/main.py") is None
        assert backend.committed == []
        assert child.name in mg._active_scopes
        history = mg.resolve_operation_history("op_no_early_settlement", scope=child)
        effects = _authority_effects(history)
        assert any(effect["type"] == "AuthorityDecision" and effect["outcome"] == "allowed" for effect in effects)
        assert not any(effect["type"] == "AuthoritySettlement" for effect in effects)
        assert not any(commit.metadata.get("type") == "FileCreate" for commit in history.commits)
        pending = read_pending_authority_settlement(
            mg._repo_path,
            "op_no_early_settlement_settlement",
        )
        assert pending.phase == "pending_action"
        assert mg.list_authority_settlement_pending() == ("op_no_early_settlement_settlement",)
        with pytest.raises(ValueError, match="No operation matches"):
            mg.resolve_operation_history("op_no_early_settlement_settlement", scope=mg.ground)
    finally:
        monkeypatch.setattr(lifecycle, "_begin_lifecycle_run", original_begin)
        mg.deactivate(warn_on_open_scopes=False)


def test_authority_settlement_failure_leaves_recoverable_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mg, backend = _make_authority_mg(tmp_path)
    original_record_settlement = lifecycle._record_authority_final_settlement
    fail_next = True
    try:
        parent_world_before = mg.world_oid(mg.ground)
        child = _child(mg, "settlement-recovery")
        backend.write_file(child.name, "backend/src/app/main.py", b"ok\n")
        authority_context = {
            "schema": "shepherd.workspace-control.filesystem-authority-context.v1",
            "shepherd": {
                "run_ref": "run-recovery",
                "task_id": "sample_tasks.fix_bug",
            },
        }

        def fail_first_settlement(*args: Any, **kwargs: Any) -> None:
            nonlocal fail_next
            if fail_next:
                fail_next = False
                raise RuntimeError("simulated settlement write failure")
            original_record_settlement(*args, **kwargs)

        monkeypatch.setattr(lifecycle, "_record_authority_final_settlement", fail_first_settlement)

        with pytest.raises(RuntimeError, match="simulated settlement"):
            _merge_with_authority(
                mg,
                child,
                mg.ground,
                binding_roots=BINDINGS,
                decide=_decide,
                operation_id="op_settlement_recovery",
                authority_context=authority_context,
            )

        assert mg.store.read_workspace_file(mg.ground.ref, "backend/src/app/main.py") == b"ok\n"
        assert backend.committed == [(child.name, "ground")]
        assert child.name not in mg._active_scopes
        pending = read_pending_authority_settlement(mg._repo_path, "op_settlement_recovery_settlement")
        assert pending.phase == "adopted"
        assert pending.commit_outcome == "merged"
        assert pending.authority_context == authority_context
        assert pending.permission_plan_digest == PERMISSION_PLAN_DIGEST
        assert pending.permission_plan_descriptor == PERMISSION_PLAN_DESCRIPTOR
        assert pending.parent_world_before == parent_world_before
        assert pending.parent_world_after == mg.world_oid(mg.ground)
        with pytest.raises(ValueError, match="No operation matches"):
            mg.resolve_operation_history("op_settlement_recovery_settlement", scope=mg.ground)
        with pytest.raises(InvalidRepositoryStateError, match="pending authority settlement"):
            mg.fork(mg.ground, "blocked-by-pending-settlement")

        assert mg.recover_authority_settlements() == ("op_settlement_recovery_settlement",)
        assert mg.list_authority_settlement_pending() == ()
        settlement_history = mg.resolve_operation_history("op_settlement_recovery_settlement", scope=mg.ground)
        settlement = next(
            effect for effect in _authority_effects(settlement_history) if effect["type"] == "AuthoritySettlement"
        )
        assert settlement["commit_outcome"] == "merged"
        assert settlement["authority_context"] == authority_context
        assert settlement["permission_plan_digest"] == PERMISSION_PLAN_DIGEST
        assert settlement["permission_plan_descriptor"] == PERMISSION_PLAN_DESCRIPTOR
        assert settlement.get("parent_world_before") == pending.parent_world_before
        assert settlement.get("parent_world_after") == pending.parent_world_after
    finally:
        monkeypatch.setattr(lifecycle, "_record_authority_final_settlement", original_record_settlement)
        mg.deactivate(warn_on_open_scopes=False)


def test_authority_recovery_owns_workspace_publication_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mg, backend = _make_authority_mg(tmp_path)
    original_select = VcsCore._select_workspace_state_from_store
    fail_next = True
    try:
        parent_world_before = mg.world_oid(mg.ground)
        child = _child(mg, "workspace-publication-recovery")
        backend.write_file(child.name, "backend/src/app/main.py", b"ok after workspace recovery\n")

        def fail_first_workspace_publication(*args: Any, **kwargs: Any) -> None:
            nonlocal fail_next
            if fail_next:
                fail_next = False
                raise RuntimeError("simulated workspace publication failure")
            original_select(*args, **kwargs)

        monkeypatch.setattr(VcsCore, "_select_workspace_state_from_store", fail_first_workspace_publication)

        with pytest.raises(RuntimeError, match="simulated workspace publication failure"):
            _merge_with_authority(
                mg,
                child,
                mg.ground,
                binding_roots=BINDINGS,
                decide=_decide,
                operation_id="op_workspace_publication_recovery",
            )

        assert mg.list_authority_settlement_pending() == ("op_workspace_publication_recovery_settlement",)
        (pending,) = pending_workspace_authority_records(mg._repo_path)
        assert pending.source_operation_id == "op_workspace_publication_recovery"

        with pytest.raises(InvalidRepositoryStateError, match="recover_authority_settlements"):
            mg.recover_lifecycle()
        with pytest.raises(InvalidRepositoryStateError, match="pending authority settlement"):
            mg.recover_workspace_authority()

        assert mg.recover_authority_settlements() == ("op_workspace_publication_recovery_settlement",)
        assert mg.list_authority_settlement_pending() == ()
        assert pending_workspace_authority_records(mg._repo_path) == ()
        assert mg.store.read_workspace_file(mg.ground.ref, "backend/src/app/main.py") == (
            b"ok after workspace recovery\n"
        )
        assert parent_world_before != mg.world_oid(mg.ground)

        settlement_history = mg.resolve_operation_history(
            "op_workspace_publication_recovery_settlement",
            scope=mg.ground,
        )
        settlement = next(
            effect for effect in _authority_effects(settlement_history) if effect["type"] == "AuthoritySettlement"
        )
        assert settlement["commit_outcome"] == "merged"
        assert settlement.get("parent_world_before") == parent_world_before
        assert settlement["parent_world_after"] == mg.world_oid(mg.ground)
        assert settlement["workspace_publication_operation_id"] == pending.operation_id
    finally:
        monkeypatch.setattr(VcsCore, "_select_workspace_state_from_store", original_select)
        mg.deactivate(warn_on_open_scopes=False)


def test_authority_recovery_gates_before_owned_lifecycle_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mg, backend = _make_authority_mg(tmp_path)
    original_select = VcsCore._select_workspace_state_from_store
    fail_next = True
    try:
        child = _child(mg, "recovery-gate-before-mutation")
        backend.write_file(child.name, "backend/src/app/main.py", b"blocked before mutation\n")

        def fail_first_workspace_publication(*args: Any, **kwargs: Any) -> None:
            nonlocal fail_next
            if fail_next:
                fail_next = False
                raise RuntimeError("simulated gated workspace publication failure")
            original_select(*args, **kwargs)

        monkeypatch.setattr(VcsCore, "_select_workspace_state_from_store", fail_first_workspace_publication)

        with pytest.raises(RuntimeError, match="simulated gated workspace publication failure"):
            _merge_with_authority(
                mg,
                child,
                mg.ground,
                binding_roots=BINDINGS,
                decide=_decide,
                operation_id="op_recovery_gate_before_mutation",
            )

        _write_workspace_authority_pending(mg, mg.ground, "wv_unrelated_recovery_blocker")

        assert mg._lifecycle_run is not None
        assert mg._store.ref_exists(child.ref)
        assert mg.list_authority_settlement_pending() == ("op_recovery_gate_before_mutation_settlement",)
        before_workspace_pending = tuple(
            pending.operation_id for pending in pending_workspace_authority_records(mg._repo_path)
        )
        assert len(before_workspace_pending) == 2

        with pytest.raises(WorkspaceAuthorityRecoveryRequiredError, match="wv_unrelated_recovery_blocker"):
            mg.recover_authority_settlements()

        assert mg._lifecycle_run is not None
        assert mg._store.ref_exists(child.ref)
        assert mg.list_authority_settlement_pending() == ("op_recovery_gate_before_mutation_settlement",)
        assert (
            tuple(pending.operation_id for pending in pending_workspace_authority_records(mg._repo_path))
            == before_workspace_pending
        )
    finally:
        monkeypatch.setattr(VcsCore, "_select_workspace_state_from_store", original_select)
        mg.deactivate(warn_on_open_scopes=False)


@pytest.mark.parametrize(
    ("path", "expected_outcome", "expected_commit_outcome", "expected_reason"),
    [
        ("docs/forbidden.py", "denied", "not_committed_denied", "denied_decision"),
        ("secrets/key.txt", "refused", "not_committed_refused", "refused_decision"),
    ],
)
def test_authority_discard_lifecycle_uses_recovery(
    tmp_path: Path,
    path: str,
    expected_outcome: str,
    expected_commit_outcome: str,
    expected_reason: str,
) -> None:
    backend = _FailOnceDiscardBackend()
    mg, _backend = _make_authority_mg(tmp_path, backend=backend)
    try:
        parent_world_before = mg.world_oid(mg.ground)
        child = _child(mg, f"{expected_outcome}-discard-recovery")
        backend.write_file(child.name, path, b"discard me\n")
        operation_id = f"op_{expected_outcome}_discard_recovery"
        settlement_operation_id = f"{operation_id}_settlement"

        with pytest.raises(RuntimeError, match="Authority-safe discard"):
            _merge_with_authority(
                mg,
                child,
                mg.ground,
                binding_roots=BINDINGS,
                decide=_decide,
                operation_id=operation_id,
            )

        assert mg._lifecycle_run is not None
        assert mg._lifecycle_run.operation == "discard"
        assert mg._lifecycle_run.phase == "discard_substrates"
        assert mg._store.ref_exists(child.ref)
        assert mg.list_authority_settlement_pending() == (settlement_operation_id,)
        pending = read_pending_authority_settlement(mg._repo_path, settlement_operation_id)
        assert pending.outcome == expected_outcome
        assert pending.settlement == "discarded"
        assert pending.phase == "pending_action"
        assert pending.workspace_publication_operation_id is None

        with pytest.raises(InvalidRepositoryStateError, match="recover_authority_settlements"):
            mg.recover_lifecycle()

        assert mg.recover_authority_settlements() == (settlement_operation_id,)
        assert mg.list_authority_settlement_pending() == ()
        assert pending_workspace_authority_records(mg._repo_path) == ()
        assert not mg._store.ref_exists(child.ref)
        assert backend.discarded == [child.name]
        assert mg.world_oid(mg.ground) == parent_world_before
        assert mg.store.read_workspace_file(mg.ground.ref, path) is None

        settlement_history = mg.resolve_operation_history(settlement_operation_id, scope=mg.ground)
        settlement = next(
            effect for effect in _authority_effects(settlement_history) if effect["type"] == "AuthoritySettlement"
        )
        assert settlement["outcome"] == expected_outcome
        assert settlement["settlement"] == "discarded"
        assert settlement["commit_outcome"] == expected_commit_outcome
        assert settlement["reason_code"] == expected_reason
        assert settlement.get("workspace_publication_operation_id") is None
        assert settlement.get("parent_world_before") == parent_world_before
        assert settlement.get("parent_world_after") == parent_world_before
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_authority_discard_recovery_gates_before_owned_lifecycle_mutation(tmp_path: Path) -> None:
    backend = _FailOnceDiscardBackend()
    mg, _backend = _make_authority_mg(tmp_path, backend=backend)
    try:
        child = _child(mg, "discard-recovery-gate-before-mutation")
        backend.write_file(child.name, "docs/forbidden.py", b"blocked before discard recovery\n")

        with pytest.raises(RuntimeError, match="Authority-safe discard"):
            _merge_with_authority(
                mg,
                child,
                mg.ground,
                binding_roots=BINDINGS,
                decide=_decide,
                operation_id="op_discard_recovery_gate_before_mutation",
            )

        _write_workspace_authority_pending(mg, mg.ground, "wv_unrelated_discard_recovery_blocker")

        assert mg._lifecycle_run is not None
        assert mg._lifecycle_run.operation == "discard"
        assert mg._store.ref_exists(child.ref)
        assert mg.list_authority_settlement_pending() == (
            "op_discard_recovery_gate_before_mutation_settlement",
        )
        assert backend.discarded == []

        with pytest.raises(
            WorkspaceAuthorityRecoveryRequiredError,
            match="wv_unrelated_discard_recovery_blocker",
        ):
            mg.recover_authority_settlements()

        assert mg._lifecycle_run is not None
        assert mg._lifecycle_run.operation == "discard"
        assert mg._store.ref_exists(child.ref)
        assert mg.list_authority_settlement_pending() == (
            "op_discard_recovery_gate_before_mutation_settlement",
        )
        assert backend.discarded == []
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_authority_discard_owned_lifecycle_survives_restart_for_recovery(tmp_path: Path) -> None:
    backend = _FailOnceDiscardBackend()
    mg, _backend = _make_authority_mg(tmp_path, backend=backend)
    try:
        child = _child(mg, "restart-discard-recovery")
        backend.write_file(child.name, "docs/forbidden.py", b"discard after restart\n")

        with pytest.raises(RuntimeError, match="Authority-safe discard"):
            _merge_with_authority(
                mg,
                child,
                mg.ground,
                binding_roots=BINDINGS,
                decide=_decide,
                operation_id="op_restart_discard_recovery",
            )
    finally:
        mg.deactivate(warn_on_open_scopes=False)

    restarted, _backend = _make_authority_mg(tmp_path, backend=backend)
    restarted.deactivate(warn_on_open_scopes=False)
    try:
        with pytest.raises(InvalidRepositoryStateError, match="generic lifecycle activation"):
            restarted.activate(recover_lifecycle="resume")

        restarted.activate()
        assert restarted.recover_authority_settlements() == ("op_restart_discard_recovery_settlement",)
        assert restarted.list_authority_settlement_pending() == ()
        assert pending_workspace_authority_records(restarted._repo_path) == ()
        assert restarted.store.read_workspace_file(restarted.ground.ref, "docs/forbidden.py") is None
    finally:
        restarted.deactivate(warn_on_open_scopes=False)


def test_authority_owned_lifecycle_survives_restart_for_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mg, backend = _make_authority_mg(tmp_path)
    original_select = VcsCore._select_workspace_state_from_store
    fail_next = True
    try:
        child = _child(mg, "restart-publication-recovery")
        backend.write_file(child.name, "backend/src/app/main.py", b"ok after restart recovery\n")

        def fail_first_workspace_publication(*args: Any, **kwargs: Any) -> None:
            nonlocal fail_next
            if fail_next:
                fail_next = False
                raise RuntimeError("simulated restart workspace publication failure")
            original_select(*args, **kwargs)

        monkeypatch.setattr(VcsCore, "_select_workspace_state_from_store", fail_first_workspace_publication)

        with pytest.raises(RuntimeError, match="simulated restart workspace publication failure"):
            _merge_with_authority(
                mg,
                child,
                mg.ground,
                binding_roots=BINDINGS,
                decide=_decide,
                operation_id="op_restart_publication_recovery",
            )
    finally:
        mg.deactivate(warn_on_open_scopes=False)

    restarted, _backend = _make_authority_mg(tmp_path, backend=backend)
    restarted.deactivate(warn_on_open_scopes=False)
    try:
        with pytest.raises(InvalidRepositoryStateError, match="generic lifecycle activation"):
            restarted.activate(recover_lifecycle="resume")

        restarted.activate()
        assert restarted.recover_authority_settlements() == ("op_restart_publication_recovery_settlement",)
        assert restarted.store.read_workspace_file(restarted.ground.ref, "backend/src/app/main.py") == (
            b"ok after restart recovery\n"
        )
        assert pending_workspace_authority_records(restarted._repo_path) == ()
    finally:
        monkeypatch.setattr(VcsCore, "_select_workspace_state_from_store", original_select)
        restarted.deactivate(warn_on_open_scopes=False)


def test_authority_preflights_settlement_operation_id_before_candidate_action(tmp_path: Path) -> None:
    mg, backend = _make_authority_mg(tmp_path)
    try:
        with mg.runtime_activity(
            scope=mg.ground,
            operation_id="op_collision_settlement",
            operation_label="existing settlement id",
            operation_kind="test.operation-id-collision",
        ):
            pass

        child = _child(mg, "settlement-id-collision")
        backend.write_file(child.name, "backend/src/app/main.py", b"ok\n")

        with pytest.raises(ValueError, match="op_collision_settlement"):
            _merge_with_authority(
                mg,
                child,
                mg.ground,
                binding_roots=BINDINGS,
                decide=_decide,
                operation_id="op_collision",
            )

        assert mg.store.read_workspace_file(mg.ground.ref, "backend/src/app/main.py") is None
        assert backend.committed == []
        assert backend.discarded == []
        assert child.name in mg._active_scopes
        assert mg.list_authority_settlement_pending() == ()
        with pytest.raises(ValueError, match="No operation matches"):
            mg.resolve_operation_history("op_collision", scope=child)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_authority_corrupt_pending_settlement_is_listed_and_recovery_fails_closed(tmp_path: Path) -> None:
    mg, _backend = _make_authority_mg(tmp_path)
    try:
        path = _authority_settlement_pending_path(mg._repo_path, "op_corrupt_settlement")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not-json")

        assert mg.list_authority_settlement_pending() == ("op_corrupt_settlement (present_corrupt)",)
        with pytest.raises(InvalidRepositoryStateError, match="pending-settlement inventory is invalid"):
            mg.recover_authority_settlements()
    finally:
        mg.deactivate(warn_on_open_scopes=False)


@pytest.mark.parametrize("root", ["", ".", "/"])
def test_authority_root_binding_matches_whole_workspace(tmp_path: Path, root: str) -> None:
    mg, backend = _make_authority_mg(tmp_path)
    try:
        child = _child(mg, "root-binding")
        backend.write_file(child.name, "src/app.py", b"ok\n")

        def decide(request: GitRepoAuthorityRequest) -> AuthorityDecision:
            assert request.match_view.binding_ref == "workspace"
            assert request.match_view.path == "src/app.py"
            assert request.match_view.monitor_basis == "carrier_check_at_commit"
            assert "basis" not in request.match_view.as_mapping()
            return AuthorityDecision(outcome="allowed", reason_code="workspace_root_allowed")

        result = _merge_with_authority(
            mg,
            child,
            mg.ground,
            binding_roots={"workspace": root},
            decide=decide,
        )

        assert result.outcome == "allowed"
        assert mg.store.read_workspace_file(mg.ground.ref, "src/app.py") == b"ok\n"
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_authority_refuses_invalid_candidate_path(tmp_path: Path) -> None:
    mg, backend = _make_authority_mg(tmp_path)
    try:
        child = _child(mg, "invalid-path")
        backend.write_file(child.name, "../escape.py", b"nope\n")

        result = _merge_with_authority(
            mg,
            child,
            mg.ground,
            binding_roots={"workspace": ""},
            decide=_decide,
        )

        assert result.outcome == "refused"
        assert result.decisions[0].reason_code == "invalid_path"
        assert mg.store.read_workspace_file(mg.ground.ref, "../escape.py") is None
    finally:
        mg.deactivate(warn_on_open_scopes=False)


@pytest.mark.parametrize(
    "binding_roots",
    [
        {"workspace": "", "docs": "../docs"},
        {"": "backend", "backend": "backend"},
    ],
)
def test_authority_refuses_invalid_binding_roots_without_provider(
    tmp_path: Path,
    binding_roots: dict[str, str],
) -> None:
    mg, backend = _make_authority_mg(tmp_path)
    called = False
    try:
        child = _child(mg, "invalid-binding-roots")
        backend.write_file(child.name, "backend/src/app/main.py", b"ok\n")

        def decide(request: GitRepoAuthorityRequest) -> AuthorityDecision:
            nonlocal called
            called = True
            return _decide(request)

        result = _merge_with_authority(
            mg,
            child,
            mg.ground,
            binding_roots=binding_roots,
            decide=decide,
        )

        assert result.outcome == "refused"
        assert result.decisions[0].reason_code == "invalid_binding_roots"
        assert not called
        assert mg.store.read_workspace_file(mg.ground.ref, "backend/src/app/main.py") is None
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_authority_longest_binding_root_wins_over_workspace_root(tmp_path: Path) -> None:
    mg, backend = _make_authority_mg(tmp_path)
    try:
        child = _child(mg, "longest-binding")
        backend.write_file(child.name, "docs/readme.md", b"nope\n")

        result = _merge_with_authority(
            mg,
            child,
            mg.ground,
            binding_roots={"workspace": "", "docs": "docs"},
            decide=_decide,
        )

        assert result.outcome == "denied"
        assert result.decisions[0].request.match_view.binding_ref == "docs"
        assert result.decisions[0].request.match_view.path == "readme.md"
        assert mg.store.read_workspace_file(mg.ground.ref, "docs/readme.md") is None
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_authority_refuses_ambiguous_equal_binding_roots(tmp_path: Path) -> None:
    mg, backend = _make_authority_mg(tmp_path)
    try:
        child = _child(mg, "ambiguous-binding")
        backend.write_file(child.name, "backend/src/app/main.py", b"ok\n")

        result = _merge_with_authority(
            mg,
            child,
            mg.ground,
            binding_roots={"backend": "backend", "backend_alias": "backend"},
            decide=_decide,
        )

        assert result.outcome == "refused"
        assert result.decisions[0].reason_code == "ambiguous_binding"
        assert mg.store.read_workspace_file(mg.ground.ref, "backend/src/app/main.py") is None
    finally:
        mg.deactivate(warn_on_open_scopes=False)


class _GitControlPlaneAdmissionProvider:
    def validate_command_invocation(
        self,
        command: str,
        scope: ScopeInfo,
        *,
        params: dict[str, Any],
    ) -> None:
        del command, scope
        path = params.get("path")
        if isinstance(path, str) and (path == ".git" or path.startswith(".git/")):
            raise CommandAdmissionError("raw.git control-plane is not ordinary workspace authority")


def test_registered_command_admission_provider_refuses_git_control_plane_before_capture(tmp_path: Path) -> None:
    mg, _backend = _make_authority_mg(tmp_path)
    try:
        mg.register_command_admission_provider(_GitControlPlaneAdmissionProvider())
        child = _child(mg, "git-admission")

        with pytest.raises(CommandAdmissionError, match="control-plane"):
            mg.exec("filesystem", "write", scope=child, path=".git/hooks/evil", content=b"bad\n")

        assert mg.discard(child) == child.name
    finally:
        mg.deactivate(warn_on_open_scopes=False)
