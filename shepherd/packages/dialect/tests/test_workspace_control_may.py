"""Workspace-control ``may=`` algebra coverage."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Annotated

import pytest
from shepherd_runtime.nucleus import GitRepo
from vcs_core._authority import AuthzMatchView

from shepherd_dialect.workspace_control.authority import (
    GitRepoAuthorityDecisionPolicy,
    GitRepoGrant,
    GitRepoGrantClamp,
    GitRepoGrantClause,
    GitRepoGrantDescriptor,
    GitRepoPath,
    May,
    ReadOnly,
    ReadWrite,
    build_gitrepo_field_authority_surface,
    clamp_gitrepo_grants,
    decide_gitrepo_authority_request,
    gitrepo_authority_surface_for_grant,
    gitrepo_grant_descriptor_from_may_annotation,
)
from shepherd_dialect.workspace_control.may import (
    DEFAULT_WORKSPACE_MAY_PROFILE,
    MayProfileWideningError,
    UnsupportedMayProfileError,
    canonical_may_profile_name,
    normalize_may_profile,
    repo_authority_for_may,
    resolve_workspace_authority_decision,
    supported_may_profile_names,
)
from shepherd_dialect.workspace_control.retained_output_authority import (
    retained_output_authority_provider_for_context,
    retained_output_authority_provider_for_profile,
)
from shepherd_dialect.workspace_control.schemas import RunAuthorityContext
from shepherd_dialect.workspace_control.workspace_authority import run_authority_context_for_decision


def test_supported_may_profile_names_are_ordered_by_authority() -> None:
    assert supported_may_profile_names() == ("ReadOnly", "ReadWrite", "Permissive")
    assert DEFAULT_WORKSPACE_MAY_PROFILE == "ReadWrite"


def test_public_may_is_annotated_alias() -> None:
    assert May is Annotated


def test_repo_authority_lowering_is_canonical() -> None:
    assert repo_authority_for_may("ReadOnly") == "readonly"
    assert repo_authority_for_may("ReadWrite") == "readwrite"
    assert repo_authority_for_may("Permissive") == "readwrite"


def test_workspace_authority_decision_uses_task_default_when_requested_is_omitted() -> None:
    decision = resolve_workspace_authority_decision(task_default=DEFAULT_WORKSPACE_MAY_PROFILE, requested=None)

    assert decision.task_default.name == "ReadWrite"
    assert decision.requested is None
    assert decision.effective.name == "ReadWrite"
    assert decision.may_profile_name == "ReadWrite"
    assert decision.repo_authority == "readwrite"
    assert decision.workspace_selection_can_mutate is True


def test_workspace_authority_decision_can_narrow_task_default() -> None:
    decision = resolve_workspace_authority_decision(task_default="ReadWrite", requested="ReadOnly")

    assert decision.task_default.name == "ReadWrite"
    assert decision.requested is not None
    assert decision.requested.name == "ReadOnly"
    assert decision.effective.name == "ReadOnly"
    assert decision.may_profile_name == "ReadOnly"
    assert decision.repo_authority == "readonly"
    assert decision.workspace_selection_can_mutate is False


def test_run_authority_context_round_trips_and_rehydrates_retained_output_provider() -> None:
    decision = resolve_workspace_authority_decision(task_default="ReadWrite", requested="ReadOnly")

    context = run_authority_context_for_decision(decision)
    restored = RunAuthorityContext.from_json(context.to_json())
    clamp = GitRepoGrantClamp.from_descriptor(restored.grant_clamp)
    provider = retained_output_authority_provider_for_context(restored)
    allowed = provider(_retained_output_request(mutates=False, kind="gitrepo.file_read", path="README.md"))
    denied = provider(_retained_output_request(mutates=True, kind="gitrepo.file_patch"))

    assert restored == context
    assert restored.task_default_may == "ReadWrite"
    assert restored.requested_may == "ReadOnly"
    assert restored.effective_may == "ReadOnly"
    assert restored.repo_authority == "readonly"
    assert restored.workspace_selection_can_mutate is False
    assert clamp.effective.digest == restored.effective_grant_digest
    assert provider.effective_match_digest == restored.effective_match_digest
    assert provider.authority_surface_plan_digest == restored.authority_surface_plan_digest
    assert allowed.outcome == "allowed"
    assert denied.outcome == "denied"


def test_authority_surface_plan_digest_is_not_monitor_permission_plan_digest(tmp_path) -> None:
    from shepherd_dialect.confinement import resolve_may
    from shepherd_dialect.permission_plan import install as install_permission_plan

    decision = resolve_workspace_authority_decision(task_default="ReadWrite", requested="ReadOnly")
    context = run_authority_context_for_decision(decision)
    payload = context.to_json()
    monitor_plan = install_permission_plan(resolve_may("ReadOnly"), tmp_path)

    assert context.authority_surface_plan_digest
    assert monitor_plan.digest
    assert context.authority_surface_plan_digest != monitor_plan.digest
    assert "authority_surface_plan_digest" in payload
    assert "permission_plan_digest" not in payload


def test_retained_output_provider_builds_carrier_permission_plan() -> None:
    decision = resolve_workspace_authority_decision(task_default="ReadWrite", requested="ReadOnly")
    context = run_authority_context_for_decision(decision)
    provider = retained_output_authority_provider_for_context(context)
    descriptor = provider.permission_plan_descriptor
    (assignment,) = descriptor["assignments"]

    assert provider.permission_plan_digest
    assert provider.permission_plan_digest != provider.authority_surface_plan_digest
    assert descriptor["schema"] == "shepherd.permission-plan.v1"
    assert assignment["monitor"] == "carrier_check_at_commit"
    assert assignment["route"] == "retained_output_selection"
    assert assignment["evidence"] == {
        "authority_surface_plan_digest": provider.authority_surface_plan_digest,
        "effective_match_digest": provider.effective_match_digest,
    }
    assert "permission_plan_digest" not in context.to_json()


def test_gitrepo_grant_clamp_from_descriptor_rejects_self_consistent_widened_effective_grant() -> None:
    parent = GitRepoGrantDescriptor(
        grant_ref="readonly-parent",
        clauses=(GitRepoGrantClause(binding_ref="workspace", mutates=False),),
    )
    requested = GitRepoGrantDescriptor(
        grant_ref="requested-write",
        clauses=(GitRepoGrantClause(binding_ref="workspace"),),
    )
    clamp = clamp_gitrepo_grants(parent_ceiling=parent, requested=requested, grant_ref="effective")
    widened_effective = GitRepoGrantDescriptor(
        grant_ref="effective",
        clauses=(GitRepoGrantClause(binding_ref="workspace"),),
    )
    forged = _forge_clamp_effective(clamp, widened_effective)

    with pytest.raises(ValueError, match="lawful parent/requested intersection"):
        GitRepoGrantClamp.from_descriptor(forged)


def test_retained_output_provider_rejects_tampered_run_authority_context_before_deciding() -> None:
    decision = resolve_workspace_authority_decision(task_default="ReadWrite", requested="ReadOnly")
    context = run_authority_context_for_decision(decision)
    clamp = GitRepoGrantClamp.from_descriptor(context.grant_clamp)
    widened_effective = GitRepoGrantDescriptor(
        grant_ref=clamp.effective.grant_ref,
        clauses=(GitRepoGrantClause(binding_ref="workspace"),),
    )
    payload = context.to_json()
    payload["grant_clamp"] = _forge_clamp_effective(clamp, widened_effective)
    tampered = RunAuthorityContext.from_json(payload)

    with pytest.raises(ValueError, match="lawful parent/requested intersection"):
        retained_output_authority_provider_for_context(tampered)


def test_retained_output_provider_rejects_tampered_classifier_policy_before_deciding() -> None:
    decision = resolve_workspace_authority_decision(task_default="ReadWrite", requested="ReadOnly")
    context = run_authority_context_for_decision(decision)
    payload = context.to_json()
    policy = dict(context.classifier_policy)
    policy["allow_reason_code"] = "forged_allow_reason"
    payload["classifier_policy"] = policy
    tampered = RunAuthorityContext.from_json(payload)

    with pytest.raises(ValueError, match="classifier policy disagrees"):
        retained_output_authority_provider_for_context(tampered)


def test_run_may_override_cannot_widen_task_default() -> None:
    with pytest.raises(MayProfileWideningError, match="may='ReadWrite' exceeds task may_default='ReadOnly'"):
        resolve_workspace_authority_decision(task_default="ReadOnly", requested="ReadWrite")


def test_unsupported_may_profile_fails_closed() -> None:
    with pytest.raises(UnsupportedMayProfileError, match="may='WriteOnly'"):
        canonical_may_profile_name("WriteOnly")

    with pytest.raises(UnsupportedMayProfileError, match="may='Standard'"):
        resolve_workspace_authority_decision(task_default="ReadWrite", requested="Standard")


def test_public_may_gitrepo_grant_annotation_lowers_to_descriptor() -> None:
    descriptor = gitrepo_grant_descriptor_from_may_annotation(
        May[GitRepo, GitRepoPath("src/app")],
        grant_ref="signature:repo",
    )

    assert descriptor is not None
    assert descriptor.to_descriptor() == {
        "schema": "shepherd.workspace-control.gitrepo-grant.v1",
        "grant_ref": "signature:repo",
        "clauses": [
            {
                "binding_ref": "workspace",
                "path_prefix": "src/app",
                "mutates": True,
            }
        ],
    }


def test_public_may_gitrepo_readonly_narrows_workspace_authority_decision() -> None:
    descriptor = gitrepo_grant_descriptor_from_may_annotation(
        May[GitRepo, ReadOnly],
        grant_ref="signature:repo",
    )
    decision = resolve_workspace_authority_decision(
        task_default="ReadWrite",
        requested=None,
        gitrepo_grant=descriptor,
    )

    assert decision.repo_authority == "readonly"
    assert decision.workspace_selection_can_mutate is False
    assert decision.gitrepo_grant_clamp is not None
    assert decision.gitrepo_grant_clamp.effective.clauses == (
        GitRepoGrantClause(binding_ref="workspace", mutates=False),
    )


def test_public_may_gitrepo_readwrite_cannot_widen_task_default_readonly() -> None:
    descriptor = gitrepo_grant_descriptor_from_may_annotation(
        May[GitRepo, ReadWrite],
        grant_ref="signature:repo",
    )
    decision = resolve_workspace_authority_decision(
        task_default="ReadOnly",
        requested=None,
        gitrepo_grant=descriptor,
    )

    assert decision.effective.name == "ReadOnly"
    assert decision.repo_authority == "readonly"
    assert decision.workspace_selection_can_mutate is False
    assert decision.gitrepo_grant_clamp is not None
    assert decision.gitrepo_grant_clamp.effective.clauses == (
        GitRepoGrantClause(binding_ref="workspace", mutates=False),
    )


def test_public_may_gitrepo_readwrite_cannot_widen_call_site_readonly() -> None:
    descriptor = gitrepo_grant_descriptor_from_may_annotation(
        May[GitRepo, ReadWrite],
        grant_ref="signature:repo",
    )
    decision = resolve_workspace_authority_decision(
        task_default="ReadWrite",
        requested="ReadOnly",
        gitrepo_grant=descriptor,
    )

    assert decision.effective.name == "ReadOnly"
    assert decision.repo_authority == "readonly"
    assert decision.workspace_selection_can_mutate is False
    assert decision.gitrepo_grant_clamp is not None
    assert decision.gitrepo_grant_clamp.effective.clauses == (
        GitRepoGrantClause(binding_ref="workspace", mutates=False),
    )


def test_public_may_gitrepo_grant_rejects_wrong_handle_type() -> None:
    with pytest.raises(TypeError, match="GitRepo May grant metadata"):
        gitrepo_grant_descriptor_from_may_annotation(
            May[str, ReadWrite],
            grant_ref="signature:repo",
        )


def test_public_may_gitrepo_grant_rejects_unknown_metadata() -> None:
    with pytest.raises(TypeError, match="unsupported GitRepo May grant"):
        gitrepo_grant_descriptor_from_may_annotation(
            May[GitRepo, object()],
            grant_ref="signature:repo",
        )


def test_public_may_gitrepo_grant_rejects_multiple_metadata() -> None:
    with pytest.raises(ValueError, match="exactly one GitRepo grant"):
        gitrepo_grant_descriptor_from_may_annotation(
            May[GitRepo, ReadOnly, ReadWrite],
            grant_ref="signature:repo",
        )


def test_public_gitrepo_grant_rejects_unsupported_values() -> None:
    with pytest.raises((TypeError, ValueError)):
        GitRepoGrant(binding_ref="workspace", path_prefix="../escape")


def test_readonly_retained_output_authority_uses_match_for_non_mutating_selection() -> None:
    provider = retained_output_authority_provider_for_profile(normalize_may_profile("ReadOnly"))
    request = _retained_output_request(mutates=False, kind="gitrepo.retained_output_select")

    assert provider.effective_match.matches(request.match_view.as_mapping())
    assert provider.effective_match_digest
    assert provider.authority_surface_plan_digest
    decision = provider(request)

    assert decision.outcome == "allowed"
    assert decision.reason_code == "may_ReadOnly_retained_output_selection_match"


def test_readonly_retained_output_authority_denies_mutating_selection() -> None:
    provider = retained_output_authority_provider_for_profile(normalize_may_profile("ReadOnly"))
    request = _retained_output_request(mutates=True, kind="gitrepo.file_patch")

    assert not provider.effective_match.matches(request.match_view.as_mapping())
    decision = provider(request)

    assert decision.outcome == "denied"
    assert decision.reason_code == "may_ReadOnly_retained_output_selection_mutates_workspace"


def test_readwrite_retained_output_authority_allows_mutating_selection() -> None:
    provider = retained_output_authority_provider_for_profile(normalize_may_profile("ReadWrite"))
    request = _retained_output_request(mutates=True, kind="gitrepo.file_patch")

    assert provider.effective_match.matches(request.match_view.as_mapping())
    decision = provider(request)

    assert decision.outcome == "allowed"
    assert decision.reason_code == "may_ReadWrite_retained_output_selection_match"


def test_retained_output_authority_refuses_unsupported_binding() -> None:
    provider = retained_output_authority_provider_for_profile(normalize_may_profile("ReadWrite"))
    request = _retained_output_request(mutates=True, binding_ref="docs")

    decision = provider(request)

    assert decision.outcome == "refused"
    assert decision.reason_code == "unsupported_retained_output_binding"


def test_retained_output_authority_refuses_match_evaluation_errors() -> None:
    provider = retained_output_authority_provider_for_profile(normalize_may_profile("ReadOnly"))
    request = SimpleNamespace(
        reason_code=None,
        match_view=SimpleNamespace(
            as_mapping=lambda: {
                "domain": "gitrepo.v0",
                "route": "retained_output_selection",
                "binding_ref": "workspace",
                "classification_basis": "exact_tree_diff",
            }
        ),
    )

    decision = provider(request)

    assert decision.outcome == "refused"
    assert decision.reason_code == "retained_output_match_view_invalid:ValueError"


def test_readwrite_retained_output_authority_allows_changed_paths_fallback() -> None:
    provider = retained_output_authority_provider_for_profile(normalize_may_profile("ReadWrite"))
    request = _retained_output_request(mutates=True, classification_basis="changed_paths_fallback")

    decision = provider(request)

    assert decision.outcome == "allowed"
    assert decision.reason_code == "may_ReadWrite_retained_output_selection_match"
    assert decision.monitor_basis == "carrier_check_at_commit"
    assert decision.completeness == "advisory"


def test_readwrite_retained_output_authority_refuses_unclassifiable_evidence() -> None:
    provider = retained_output_authority_provider_for_profile(normalize_may_profile("ReadWrite"))
    request = _retained_output_request(mutates=True, reason_code="unclassifiable_retained_output")

    decision = provider(request)

    assert decision.outcome == "refused"
    assert decision.reason_code == "unclassifiable_retained_output"


def test_retained_output_authority_refuses_preclassified_request_errors() -> None:
    provider = retained_output_authority_provider_for_profile(normalize_may_profile("ReadWrite"))
    request = _retained_output_request(mutates=True, reason_code="raw_git_control_plane")

    decision = provider(request)

    assert decision.outcome == "refused"
    assert decision.reason_code == "raw_git_control_plane"


@pytest.mark.parametrize(
    ("overrides", "missing_fields"),
    [
        ({"mutates": "yes"}, ()),
        ({"path": 42}, ()),
        ({"control_plane": "false"}, ()),
        ({"classification_basis": "maybe"}, ()),
        ({"monitor_basis": ""}, ()),
        ({}, ("kind",)),
    ],
)
def test_readwrite_retained_output_authority_refuses_malformed_view_before_matching(
    overrides: dict[str, object],
    missing_fields: tuple[str,...],
) -> None:
    provider = retained_output_authority_provider_for_profile(normalize_may_profile("ReadWrite"))
    mapping = _retained_output_request(mutates=True).match_view.as_mapping()
    mapping.update(overrides)
    for field in missing_fields:
        del mapping[field]

    decision = provider(_request_from_mapping(mapping))

    assert decision.outcome == "refused"
    assert decision.reason_code.startswith("retained_output_match_view_invalid:")
    assert decision.completeness == "incomplete"


def test_readwrite_retained_output_authority_refuses_control_plane_view_by_default() -> None:
    provider = retained_output_authority_provider_for_profile(normalize_may_profile("ReadWrite"))
    mapping = _retained_output_request(mutates=True).match_view.as_mapping()
    mapping["control_plane"] = True

    decision = provider(_request_from_mapping(mapping))

    assert decision.outcome == "refused"
    assert decision.reason_code == "unsupported_retained_output_control_plane"
    assert decision.completeness == "incomplete"


def test_internal_gitrepo_grant_clamp_builds_effective_match_surface() -> None:
    parent = GitRepoGrantDescriptor(
        grant_ref="parent-ceiling",
        clauses=(
            GitRepoGrantClause(binding_ref="docs", mutates=False),
            GitRepoGrantClause(binding_ref="backend", path_prefix="src/"),
        ),
    )
    requested = GitRepoGrantDescriptor(
        grant_ref="parameter-grant",
        clauses=(
            GitRepoGrantClause(binding_ref="docs", mutates=False),
            GitRepoGrantClause(binding_ref="backend", path_prefix="src/app/"),
        ),
    )

    clamp = clamp_gitrepo_grants(parent_ceiling=parent, requested=requested, grant_ref="effective-grant")
    surface = gitrepo_authority_surface_for_grant(clamp.effective, label="fixture.effective", route="carrier_diff")

    assert clamp.effective.clauses == (
        GitRepoGrantClause(binding_ref="docs", mutates=False),
        GitRepoGrantClause(binding_ref="backend", path_prefix="src/app/"),
    )
    assert clamp.descriptor["effective_clause_count"] == 2
    assert clamp.digest
    assert surface.effective_match_digest
    assert surface.authority_surface_plan_digest
    assert surface.effective_match_descriptor["grant_ref"] == "effective-grant"
    assert surface.effective_match_descriptor["grant_digest"] == clamp.effective.digest
    assert surface.effective_match.matches(_gitrepo_view(binding_ref="docs", path="README.md", mutates=False))
    assert not surface.effective_match.matches(_gitrepo_view(binding_ref="docs", path="README.md", mutates=True))
    assert surface.effective_match.matches(_gitrepo_view(binding_ref="backend", path="src/app", mutates=True))
    assert surface.effective_match.matches(_gitrepo_view(binding_ref="backend", path="src/app/main.py", mutates=True))
    assert not surface.effective_match.matches(
        _gitrepo_view(binding_ref="backend", path="src/application.py", mutates=True)
    )
    assert not surface.effective_match.matches(_gitrepo_view(binding_ref="backend", path="src/lib.py", mutates=True))


def test_internal_gitrepo_grant_clamp_drops_disjoint_clauses() -> None:
    parent = GitRepoGrantDescriptor(
        grant_ref="parent-ceiling",
        clauses=(GitRepoGrantClause(binding_ref="backend", path_prefix="src/app/"),),
    )
    requested = GitRepoGrantDescriptor(
        grant_ref="parameter-grant",
        clauses=(GitRepoGrantClause(binding_ref="backend", path_prefix="tests/"),),
    )

    clamp = clamp_gitrepo_grants(parent_ceiling=parent, requested=requested, grant_ref="effective-empty")
    surface = gitrepo_authority_surface_for_grant(clamp.effective, label="fixture.empty", route="carrier_diff")

    assert clamp.effective.clauses == ()
    assert not surface.effective_match.matches(
        _gitrepo_view(binding_ref="backend", path="src/app/main.py", mutates=True)
    )

    sibling = GitRepoGrantDescriptor(
        grant_ref="sibling-grant",
        clauses=(GitRepoGrantClause(binding_ref="backend", path_prefix="src/application"),),
    )
    sibling_clamp = clamp_gitrepo_grants(parent_ceiling=parent, requested=sibling, grant_ref="effective-sibling")

    assert sibling_clamp.effective.clauses == ()


def test_internal_gitrepo_grant_path_prefix_normalizes_root_and_trailing_slash() -> None:
    explicit_root = GitRepoGrantClause(binding_ref="backend", path_prefix="/")
    dotted_root = GitRepoGrantClause(binding_ref="backend", path_prefix=".")
    normalized_prefix = GitRepoGrantClause(binding_ref="backend", path_prefix="src/app/")

    assert explicit_root.path_prefix is None
    assert dotted_root.path_prefix is None
    assert normalized_prefix.path_prefix == "src/app"
    assert normalized_prefix.to_descriptor()["path_prefix"] == "src/app"

    surface = gitrepo_authority_surface_for_grant(
        GitRepoGrantDescriptor(grant_ref="root", clauses=(explicit_root,)),
        label="fixture.root",
        route="carrier_diff",
    )

    assert surface.effective_match.matches(_gitrepo_view(binding_ref="backend", path="README.md", mutates=True))
    assert surface.effective_match.matches(
        _gitrepo_view(binding_ref="backend", path="src/application.py", mutates=True)
    )


@pytest.mark.parametrize("path_prefix", ["", "../src", "src/../app", "/src", "src\0app"])
def test_internal_gitrepo_grant_path_prefix_rejects_invalid_values(path_prefix: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        GitRepoGrantClause(binding_ref="backend", path_prefix=path_prefix)


@pytest.mark.parametrize(
    "constraint",
    [
        {"field": "unknown", "op": "eq", "value": "x"},
        {"field": "mutates", "op": "startswith", "value": "false"},
        {"field": "mutates", "op": "eq", "value": "false"},
        {"field": "path", "op": "startswith", "value": "src/app"},
        {"field": "path", "op": "startswith", "value": True},
        {"field": "classification_basis", "op": "eq", "value": "maybe"},
    ],
)
def test_internal_gitrepo_field_authority_surface_rejects_unsupported_constraints(
    constraint: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        build_gitrepo_field_authority_surface(label="invalid", clauses=((constraint,),))


def test_path_sensitive_gitrepo_authority_allows_exact_tree_diff() -> None:
    surface = gitrepo_authority_surface_for_grant(
        GitRepoGrantDescriptor(
            grant_ref="workspace-src-app",
            clauses=(GitRepoGrantClause(binding_ref="workspace", path_prefix="src/app"),),
        ),
        label="fixture.path-sensitive",
        route="retained_output_selection",
    )
    request = _retained_output_request(mutates=True, path="src/app/main.py")

    decision = decide_gitrepo_authority_request(
        request=request,
        surface=surface,
        policy=_path_sensitive_policy(),
    )

    assert surface.path_sensitive is True
    assert decision.outcome == "allowed"
    assert decision.reason_code == "test_path_sensitive_match"
    assert decision.matched_grant_ref == "workspace-src-app"
    assert decision.monitor_basis == "carrier_check_at_commit"
    assert decision.completeness == "complete"


def test_path_sensitive_gitrepo_authority_refuses_changed_paths_fallback() -> None:
    surface = gitrepo_authority_surface_for_grant(
        GitRepoGrantDescriptor(
            grant_ref="workspace-src-app",
            clauses=(GitRepoGrantClause(binding_ref="workspace", path_prefix="src/app"),),
        ),
        label="fixture.path-sensitive",
        route="retained_output_selection",
    )
    request = _retained_output_request(
        mutates=True,
        path="src/app/main.py",
        classification_basis="changed_paths_fallback",
    )

    decision = decide_gitrepo_authority_request(
        request=request,
        surface=surface,
        policy=_path_sensitive_policy(),
    )

    assert decision.outcome == "refused"
    assert decision.reason_code == "changed_paths_fallback_incomplete_for_path_authority"
    assert decision.monitor_basis == "carrier_check_at_commit"
    assert decision.completeness == "incomplete"


def test_path_sensitive_gitrepo_authority_refuses_unclassifiable_without_match_evaluation() -> None:
    surface = gitrepo_authority_surface_for_grant(
        GitRepoGrantDescriptor(
            grant_ref="workspace-src-app",
            clauses=(GitRepoGrantClause(binding_ref="workspace", path_prefix="src/app"),),
        ),
        label="fixture.path-sensitive",
        route="retained_output_selection",
    )
    request = _retained_output_request(
        mutates=True,
        path="src/app/main.py",
        reason_code="unclassifiable_retained_output",
    )

    decision = decide_gitrepo_authority_request(
        request=request,
        surface=surface,
        policy=_path_sensitive_policy(),
    )

    assert decision.outcome == "refused"
    assert decision.reason_code == "unclassifiable_retained_output"
    assert decision.completeness == "incomplete"


def _retained_output_request(
    *,
    mutates: bool,
    kind: str = "gitrepo.file_patch",
    binding_ref: str = "workspace",
    classification_basis: str = "exact_tree_diff",
    reason_code: str | None = None,
    path: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        request_id="request-1",
        reason_code=reason_code,
        match_view=AuthzMatchView(
            domain="gitrepo.v0",
            kind=kind,
            binding_ref=binding_ref,
            action=kind.replace("gitrepo.", "git_repo."),
            path=path if path is not None else ("candidate.txt" if mutates else ""),
            mutates=mutates,
            reversibility="reversible",
            control_plane=False,
            monitor_basis="carrier_check_at_commit",
            route="retained_output_selection",
            classification_basis=classification_basis,
        ),
    )


def _request_from_mapping(mapping: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        request_id="request-from-mapping",
        reason_code=None,
        match_view=SimpleNamespace(as_mapping=lambda: mapping),
    )


def _gitrepo_view(*, binding_ref: str, path: str, mutates: bool) -> dict[str, object]:
    return AuthzMatchView(
        domain="gitrepo.v0",
        kind="gitrepo.file_patch" if mutates else "gitrepo.file_read",
        binding_ref=binding_ref,
        action="git_repo.file_patch" if mutates else "git_repo.file_read",
        path=path,
        mutates=mutates,
        reversibility="reversible",
        control_plane=False,
        monitor_basis="carrier_check_at_commit",
        route="carrier_diff",
    ).as_mapping()


def _path_sensitive_policy() -> GitRepoAuthorityDecisionPolicy:
    return GitRepoAuthorityDecisionPolicy(
        routes=("retained_output_selection",),
        binding_refs=("workspace",),
        allowed_classification_bases=("exact_tree_diff",),
        allow_changed_paths_fallback=True,
        allow_changed_paths_fallback_for_path_sensitive=False,
        reason_code_subject="retained_output",
        allow_reason_code="test_path_sensitive_match",
        outside_match_reason_code="test_path_sensitive_outside_match",
        invalid_view_reason_code_prefix="test_path_sensitive_match_view_invalid",
        match_evaluation_failed_reason_code_prefix="test_path_sensitive_match_evaluation_failed",
        unclassifiable_reason_code="unclassifiable_retained_output",
        default_monitor_basis="carrier_check_at_commit",
    )


def _forge_clamp_effective(
    clamp: GitRepoGrantClamp,
    effective: GitRepoGrantDescriptor,
) -> dict[str, object]:
    descriptor = clamp.to_descriptor()
    descriptor["effective"] = effective.to_descriptor()
    descriptor["effective_digest"] = effective.digest
    descriptor["effective_clause_count"] = len(effective.clauses)
    descriptor.pop("digest", None)
    descriptor["digest"] = _descriptor_digest(descriptor)
    return descriptor


def _descriptor_digest(descriptor: dict[str, object]) -> str:
    encoded = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
