from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from vcs_core import FilesystemSubstrate, MarkerSubstrate, Store, VcsCore, build_builtin_substrate_context
from vcs_core.runtime_api import AuthorityDecision, AuthzMatchView, GitRepoAuthorityRequest
from vcs_core.runtime_substrate import TaskTraceSubstrateDriver
from vcs_core.types import AuthorityExecutionOutcome

from shepherd_dialect import handle
from shepherd_dialect.run_driver import ShepherdRunDriver
from shepherd_dialect.workspace_control import (
    ShepherdRunLedgerDriver,
    ShepherdTaskArtifactDriver,
    ShepherdTaskLedgerDriver,
    ShepherdWorkspace,
)
from shepherd_dialect.workspace_control._filesystem_authority import (
    filesystem_authority_execution_options_for_clamp,
    filesystem_authority_merge_provider_for_clamp,
    merge_workspace_scope_with_filesystem_authority,
)
from shepherd_dialect.workspace_control.authority import (
    GitRepoGrantClamp,
    GitRepoGrantClause,
    GitRepoGrantDescriptor,
    clamp_gitrepo_grants,
)
from shepherd_dialect.workspace_control.feature_flags import _seal_and_select_enabled

BINDING_ROOTS = {"backend": "backend", "docs": "docs"}


pytestmark = pytest.mark.workspace_scenario


def _make_workspace(root: Path) -> ShepherdWorkspace:
    root.mkdir(parents=True, exist_ok=True)
    store = Store(str(root / ".vcscore"))
    context = build_builtin_substrate_context(store=store, workspace=root, config={"backend": "clonefile"})
    mg = VcsCore(
        str(root),
        substrates=[
            MarkerSubstrate(context),
            FilesystemSubstrate(context),
            TaskTraceSubstrateDriver(),
            ShepherdTaskLedgerDriver(),
            ShepherdTaskArtifactDriver(),
            ShepherdRunLedgerDriver(),
            ShepherdRunDriver(),
        ],
        store=store,
    )
    with _seal_and_select_enabled():
        mg.activate()
    return ShepherdWorkspace(mg, trace_store_path=root / ".vcscore" / "shepherd" / "trace.sqlite", workspace_path=root)


def _authority_clamp() -> GitRepoGrantClamp:
    parent_ceiling = GitRepoGrantDescriptor(
        grant_ref="parent-ceiling",
        clauses=(
            GitRepoGrantClause("docs", mutates=False),
            GitRepoGrantClause("backend", path_prefix="src", mutates=True),
        ),
    )
    requested = GitRepoGrantDescriptor(
        grant_ref="requested-run-grant",
        clauses=(
            GitRepoGrantClause("docs", mutates=False),
            GitRepoGrantClause("backend", path_prefix="src/app", mutates=True),
        ),
    )
    return clamp_gitrepo_grants(
        parent_ceiling=parent_ceiling,
        requested=requested,
        grant_ref="effective-run-grant",
    )


def _shepherd_context(run_ref: str) -> dict[str, object]:
    return {
        "run_ref": run_ref,
        "task_id": "sample_tasks.fix_bug",
        "task_version": "v1",
        "may_profile": "ReadWrite",
        "launch_surface": "python",
    }


def _authority_effects(history: Any) -> list[dict[str, object]]:
    return [commit.metadata for commit in history.commits if str(commit.metadata.get("type", "")).startswith(("Authority", "RetainedOutput", "Prepared"))]


def test_workspace_filesystem_authority_execution_options_drive_runtime_run(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path / "ws")
    try:
        options = filesystem_authority_execution_options_for_clamp(
            grant_clamp=_authority_clamp(),
            binding_roots=BINDING_ROOTS,
            shepherd_context=_shepherd_context("run-dialect-options"),
        )

        def task_body(_stack: object, *, working_path: str) -> dict[str, object]:
            path = Path(working_path) / "backend" / "src" / "app" / "main.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"ok\n")
            return {"wrote": True}

        recorded = workspace.mg.execute_recorded(
            "runtime",
            "run",
            scope=workspace.mg.ground,
            task_body=task_body,
            execution_options=options,
        )

        assert isinstance(recorded.value, AuthorityExecutionOutcome)
        assert recorded.value.authority_result.outcome == "allowed"
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "backend/src/app/main.py") == b"ok\n"
        history = workspace.mg.resolve_operation_history(
            recorded.value.authority_result.authority_operation_id,
            scope=workspace.mg.ground,
        )
        decision = next(effect for effect in _authority_effects(history) if effect["type"] == "AuthorityDecision")
        assert decision["authority_context"]["shepherd"] == _shepherd_context("run-dialect-options")
    finally:
        workspace.close()


def test_workspace_filesystem_authority_execution_options_discard_mixed_runtime_run(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path / "ws")
    try:
        options = filesystem_authority_execution_options_for_clamp(
            grant_clamp=_authority_clamp(),
            binding_roots=BINDING_ROOTS,
            shepherd_context=_shepherd_context("run-dialect-options-mixed"),
        )

        def task_body(_stack: object, *, working_path: str) -> dict[str, object]:
            allowed_path = Path(working_path) / "backend" / "src" / "app" / "main.py"
            denied_path = Path(working_path) / "docs" / "bad.py"
            allowed_path.parent.mkdir(parents=True, exist_ok=True)
            denied_path.parent.mkdir(parents=True, exist_ok=True)
            allowed_path.write_bytes(b"ok\n")
            denied_path.write_bytes(b"nope\n")
            return {"wrote": True}

        recorded = workspace.mg.execute_recorded(
            "runtime",
            "run",
            scope=workspace.mg.ground,
            task_body=task_body,
            execution_options=options,
        )

        assert isinstance(recorded.value, AuthorityExecutionOutcome)
        result = recorded.value.authority_result
        assert result.outcome == "denied"
        assert [decision.outcome for decision in result.decisions] == ["allowed", "denied"]
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "backend/src/app/main.py") is None
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "docs/bad.py") is None
    finally:
        workspace.close()


def test_workspace_filesystem_authority_merge_records_shepherd_context_and_adopts_allowed(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path / "ws")
    try:
        child = workspace.mg.fork(workspace.mg.ground, "dialect-allow", hints={"isolated": True})
        workspace.mg.exec("filesystem", "write", scope=child, path="backend/src/app/main.py", content=b"ok\n")

        result = merge_workspace_scope_with_filesystem_authority(
            workspace.mg,
            child,
            workspace.mg.ground,
            grant_clamp=_authority_clamp(),
            binding_roots=BINDING_ROOTS,
            shepherd_context=_shepherd_context("run-dialect-allow"),
            operation_id="op_dialect_allow",
        )

        assert result.outcome == "allowed"
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "backend/src/app/main.py") == b"ok\n"
        history = workspace.mg.resolve_operation_history(result.authority_operation_id, scope=workspace.mg.ground)
        effects = _authority_effects(history)
        prepared = next(effect for effect in effects if effect["type"] == "PreparedAuthorityMerge")
        decision = next(effect for effect in effects if effect["type"] == "AuthorityDecision")
        for effect in (prepared, decision):
            context = effect["authority_context"]
            assert isinstance(context, dict)
            assert context["schema"] == "shepherd.workspace-control.filesystem-authority-context.v1"
            assert context["source"] == "shepherd.workspace_control"
            assert context["transaction_kind"] == "filesystem_merge"
            assert context["shepherd"] == _shepherd_context("run-dialect-allow")
            assert context["effective_match_digest"] == result.decisions[0].effective_match_digest
            assert context["authority_surface_plan_digest"] == result.decisions[0].authority_surface_plan_digest
        assert decision["matched_grant_ref"] == "effective-run-grant"

        settlement_history = workspace.mg.resolve_operation_history(
            result.settlement_operation_id,
            scope=workspace.mg.ground,
        )
        settlement = next(
            effect for effect in _authority_effects(settlement_history) if effect["type"] == "AuthoritySettlement"
        )
        assert settlement["authority_context"] == decision["authority_context"]
        assert settlement["commit_outcome"] == "merged"
    finally:
        workspace.close()


def test_workspace_filesystem_authority_merge_denies_mixed_cohort_without_public_may_syntax(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path / "ws")
    try:
        child = workspace.mg.fork(workspace.mg.ground, "dialect-mixed", hints={"isolated": True})
        workspace.mg.exec("filesystem", "write", scope=child, path="backend/src/app/good.py", content=b"ok\n")
        workspace.mg.exec("filesystem", "write", scope=child, path="docs/bad.py", content=b"nope\n")

        result = merge_workspace_scope_with_filesystem_authority(
            workspace.mg,
            child,
            workspace.mg.ground,
            grant_clamp=_authority_clamp(),
            binding_roots=BINDING_ROOTS,
            shepherd_context={
                **_shepherd_context("run-dialect-mixed"),
                "handler_env_ref": "public-handler-context-is-not-authority",
            },
            operation_id="op_dialect_mixed",
        )

        assert result.outcome == "denied"
        assert [decision.outcome for decision in result.decisions] == ["allowed", "denied"]
        assert result.decisions[1].reason_code == "filesystem_merge_mutates_outside_effective_match"
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "backend/src/app/good.py") is None
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "docs/bad.py") is None
        history = workspace.mg.resolve_operation_history(result.authority_operation_id, scope=child)
        denied = next(
            effect
            for effect in _authority_effects(history)
            if effect["type"] == "AuthorityDecision" and effect["outcome"] == "denied"
        )
        assert denied["authority_context"]["shepherd"]["handler_env_ref"] == "public-handler-context-is-not-authority"
    finally:
        workspace.close()


def test_workspace_filesystem_authority_context_is_metadata_not_policy() -> None:
    provider = filesystem_authority_merge_provider_for_clamp(
        grant_clamp=_authority_clamp(),
        binding_roots=BINDING_ROOTS,
        shepherd_context={"run_ref": "run-shadow", "authority_override": "allow_all"},
    )
    request = GitRepoAuthorityRequest(
        request_id="request-docs-write",
        candidate_effect_ref="filesystem:0",
        candidate_index=0,
        effect_type="FileCreate",
        substrate="filesystem",
        scope_ref="scope:child",
        parent_scope_ref="scope:parent",
        candidate_digest="candidate-digest",
        match_view=AuthzMatchView(
            domain="gitrepo.v0",
            kind="gitrepo.file_create",
            binding_ref="docs",
            action="git_repo.file_create",
            path="bad.py",
            mutates=True,
            reversibility="reversible",
            control_plane=False,
            monitor_basis="carrier_check_at_commit",
            route="carrier_diff",
            classification_basis="effect_record",
        ),
    )

    decision = provider.decide(request)

    assert decision.outcome == "denied"
    assert decision.reason_code == "filesystem_merge_mutates_outside_effective_match"


def test_public_handle_does_not_shadow_filesystem_authority_monitor(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path / "ws")
    calls: list[object] = []
    try:
        child = workspace.mg.fork(workspace.mg.ground, "dialect-public-handler", hints={"isolated": True})
        workspace.mg.exec("filesystem", "write", scope=child, path="docs/bad.py", content=b"nope\n")

        with handle(
            "gitrepo.file_create",
            lambda request: (
                calls.append(request)
                or AuthorityDecision(outcome="allowed", reason_code="public_handler_is_not_authority")
            ),
        ):
            result = merge_workspace_scope_with_filesystem_authority(
                workspace.mg,
                child,
                workspace.mg.ground,
                grant_clamp=_authority_clamp(),
                binding_roots=BINDING_ROOTS,
                shepherd_context=_shepherd_context("run-public-handler-shadow"),
                operation_id="op_dialect_public_handler_shadow",
            )

        assert calls == []
        assert result.outcome == "denied"
        assert result.decisions[0].reason_code == "filesystem_merge_mutates_outside_effective_match"
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "docs/bad.py") is None
    finally:
        workspace.close()
