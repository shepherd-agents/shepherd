"""Dialect-side filesystem authority merge adapter."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from shepherd_dialect.permission_plan import CarrierCheckAuthority, PermissionPlan
from shepherd_dialect.permission_plan import install as install_permission_plan
from shepherd_dialect.workspace_control.authority import (
    GitRepoAuthorityDecisionPolicy,
    GitRepoAuthoritySurface,
    GitRepoGrantClamp,
    decide_gitrepo_authority_request,
    gitrepo_authority_surface_for_grant,
)

if TYPE_CHECKING:
    from vcs_core.runtime_api import AuthorityDecision, AuthorityMergeResult, CommandExecutionOptions
    from vcs_core.types import ScopeInfo


_AUTHORITY_CONTEXT_SCHEMA = "shepherd.workspace-control.filesystem-authority-context.v1"


@dataclass(frozen=True)
class WorkspaceFilesystemAuthorityMergeProvider:
    """Prepared dialect-side authority provider for one filesystem merge."""

    surface: GitRepoAuthoritySurface
    policy: GitRepoAuthorityDecisionPolicy
    binding_roots: dict[str, str]
    authority_context: dict[str, object]

    @property
    def effective_match_digest(self) -> str:
        return cast("str", self.surface.effective_match_digest)

    @property
    def authority_surface_plan_digest(self) -> str:
        return cast("str", self.surface.authority_surface_plan_digest)

    @property
    def permission_plan(self) -> PermissionPlan:
        return install_permission_plan(
            CarrierCheckAuthority(
                route="carrier_diff",
                effective_match_digest=self.effective_match_digest,
                authority_surface_plan_digest=self.authority_surface_plan_digest,
                completeness_basis=(
                    "prepared filesystem carrier effects classified before merge, with prepared-cohort digest "
                    "reverified immediately before commit"
                ),
                tamper_basis="the check runs in the coordinator/vcs-core authority merge path, outside the task process",
            )
        )

    @property
    def permission_plan_descriptor(self) -> dict[str, object]:
        return self.permission_plan.to_descriptor()

    @property
    def permission_plan_digest(self) -> str:
        return self.permission_plan.digest

    def decide(self, request: object) -> AuthorityDecision:
        """Evaluate one classified vcs-core request through the dialect Match surface."""
        return cast(
            "AuthorityDecision",
            decide_gitrepo_authority_request(request=request, surface=self.surface, policy=self.policy),
        )

    def execution_options(self) -> CommandExecutionOptions:
        """Build framework execution controls for authority terminalization."""
        from vcs_core.runtime_api import AuthorityMergeControl, CommandExecutionOptions

        return CommandExecutionOptions(
            success_disposition="authority_merge",
            authority_merge=AuthorityMergeControl(
                binding_roots=self.binding_roots,
                decide=self.decide,
                effective_match_digest=self.effective_match_digest,
                authority_surface_plan_digest=self.authority_surface_plan_digest,
                permission_plan_digest=self.permission_plan_digest,
                permission_plan_descriptor=self.permission_plan_descriptor,
                authority_context=self.authority_context,
            ),
        )

    def merge(
        self,
        mg: Any,
        scope: ScopeInfo,
        parent: ScopeInfo,
        *,
        operation_id: str | None = None,
    ) -> AuthorityMergeResult:
        """Run vcs-core's authority-enabled merge through this dialect provider."""
        return cast(
            "AuthorityMergeResult",
            mg.merge_with_authority(
                scope,
                parent,
                binding_roots=self.binding_roots,
                decide=self.decide,
                operation_id=operation_id,
                effective_match_digest=self.effective_match_digest,
                authority_surface_plan_digest=self.authority_surface_plan_digest,
                permission_plan_digest=self.permission_plan_digest,
                permission_plan_descriptor=self.permission_plan_descriptor,
                authority_context=self.authority_context,
            ),
        )


def filesystem_authority_merge_provider_for_clamp(
    *,
    grant_clamp: GitRepoGrantClamp,
    binding_roots: Mapping[str, str],
    shepherd_context: Mapping[str, object] | None = None,
    label: str = "workspace-control.filesystem-merge",
) -> WorkspaceFilesystemAuthorityMergeProvider:
    """Build the current internal filesystem authority merge provider."""
    surface = gitrepo_authority_surface_for_grant(
        grant_clamp.effective,
        label=label,
        route="carrier_diff",
    )
    binding_refs = tuple(sorted({clause.binding_ref for clause in grant_clamp.effective.clauses}))
    policy = GitRepoAuthorityDecisionPolicy(
        routes=("carrier_diff",),
        binding_refs=binding_refs or None,
        denied_when_mutates_outside_match=True,
        reason_code_subject="filesystem_merge",
        allow_reason_code="filesystem_merge_effective_match",
        outside_match_reason_code="filesystem_merge_outside_effective_match",
        mutating_outside_match_reason_code="filesystem_merge_mutates_outside_effective_match",
        invalid_view_reason_code_prefix="filesystem_merge_match_view_invalid",
        match_evaluation_failed_reason_code_prefix="filesystem_merge_match_evaluation_failed",
        default_monitor_basis="carrier_check_at_commit",
    )
    authority_context = _filesystem_authority_context(
        grant_clamp=grant_clamp,
        surface=surface,
        shepherd_context=shepherd_context,
    )
    return WorkspaceFilesystemAuthorityMergeProvider(
        surface=surface,
        policy=policy,
        binding_roots=dict(binding_roots),
        authority_context=authority_context,
    )


def filesystem_authority_execution_options_for_clamp(
    *,
    grant_clamp: GitRepoGrantClamp,
    binding_roots: Mapping[str, str],
    shepherd_context: Mapping[str, object] | None = None,
) -> CommandExecutionOptions:
    """Build execution controls for a workspace-control filesystem authority run."""
    provider = filesystem_authority_merge_provider_for_clamp(
        grant_clamp=grant_clamp,
        binding_roots=binding_roots,
        shepherd_context=shepherd_context,
    )
    return provider.execution_options()


def merge_workspace_scope_with_filesystem_authority(
    mg: Any,
    scope: ScopeInfo,
    parent: ScopeInfo,
    *,
    grant_clamp: GitRepoGrantClamp,
    binding_roots: Mapping[str, str],
    shepherd_context: Mapping[str, object] | None = None,
    operation_id: str | None = None,
) -> AuthorityMergeResult:
    """Merge a child scope through dialect-owned GitRepo authority."""
    provider = filesystem_authority_merge_provider_for_clamp(
        grant_clamp=grant_clamp,
        binding_roots=binding_roots,
        shepherd_context=shepherd_context,
    )
    return provider.merge(mg, scope, parent, operation_id=operation_id)


def _filesystem_authority_context(
    *,
    grant_clamp: GitRepoGrantClamp,
    surface: GitRepoAuthoritySurface,
    shepherd_context: Mapping[str, object] | None,
) -> dict[str, object]:
    context: dict[str, object] = {
        "schema": _AUTHORITY_CONTEXT_SCHEMA,
        "source": "shepherd.workspace_control",
        "transaction_kind": "filesystem_merge",
        "grant": {
            "grant_ref": grant_clamp.effective.grant_ref,
            "grant_digest": grant_clamp.effective.digest,
        },
        "clamp": {
            "digest": grant_clamp.digest,
            "parent_ceiling": _descriptor_mapping(grant_clamp.descriptor.get("parent_ceiling"), "parent_ceiling"),
            "requested": _descriptor_mapping(grant_clamp.descriptor.get("requested"), "requested"),
        },
        "effective_match_digest": surface.effective_match_digest,
        "authority_surface_plan_digest": surface.authority_surface_plan_digest,
    }
    if shepherd_context is not None:
        context["shepherd"] = dict(shepherd_context)
    return context


def _descriptor_mapping(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"GitRepo grant clamp descriptor field {field_name!r} must be a mapping")
    return dict(value)
