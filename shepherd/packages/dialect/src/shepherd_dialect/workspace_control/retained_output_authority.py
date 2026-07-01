"""Retained-output authority provider for workspace-control."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from shepherd_dialect.permission_plan import CarrierCheckAuthority, PermissionPlan
from shepherd_dialect.permission_plan import install as install_permission_plan
from shepherd_dialect.workspace_control.authority import (
    GitRepoAuthorityDecisionPolicy,
    GitRepoAuthoritySurface,
    GitRepoGrantClamp,
    decide_gitrepo_authority_request,
)
from shepherd_dialect.workspace_control.workspace_authority import (
    validate_run_authority_context,
    vcscore_authority_context_for_run_authority_context,
    workspace_gitrepo_grant_for_profile,
    workspace_retained_output_authority_policy_for_grant,
    workspace_retained_output_authority_policy_for_profile,
    workspace_retained_output_authority_surface_for_grant,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shepherd_runtime.effects import Match
    from vcs_core.runtime_api import AuthorityDecision

    from shepherd_dialect.workspace_control.may import MayProfile
    from shepherd_dialect.workspace_control.schemas import RunAuthorityContext


@dataclass(frozen=True)
class WorkspaceRetainedOutputAuthorityProvider:
    """Evaluate retained-output selection through helper-built ``Match``."""

    profile: MayProfile
    surface: GitRepoAuthoritySurface
    policy: GitRepoAuthorityDecisionPolicy
    authority_context: Mapping[str, object] | None = None

    @property
    def effective_match(self) -> Match:
        return self.surface.effective_match

    @property
    def effective_match_descriptor(self) -> dict[str, object]:
        return self.surface.effective_match_descriptor

    @property
    def effective_match_digest(self) -> str:
        return self.surface.effective_match_digest

    @property
    def authority_surface_plan_digest(self) -> str:
        return self.surface.authority_surface_plan_digest

    @property
    def permission_plan(self) -> PermissionPlan:
        return install_permission_plan(
            CarrierCheckAuthority(
                route="retained_output_selection",
                effective_match_digest=self.effective_match_digest,
                authority_surface_plan_digest=self.authority_surface_plan_digest,
            )
        )

    @property
    def permission_plan_descriptor(self) -> dict[str, object]:
        return self.permission_plan.to_descriptor()

    @property
    def permission_plan_digest(self) -> str:
        return self.permission_plan.digest

    def __call__(self, request: Any) -> AuthorityDecision:
        return decide_gitrepo_authority_request(request=request, surface=self.surface, policy=self.policy)


def retained_output_authority_provider_for_profile(profile: MayProfile) -> WorkspaceRetainedOutputAuthorityProvider:
    """Build the current workspace-control retained-output authority provider."""
    grant = workspace_gitrepo_grant_for_profile(profile, grant_ref=f"workspace-effective:{profile.name}")
    surface = workspace_retained_output_authority_surface_for_grant(grant)
    policy = workspace_retained_output_authority_policy_for_profile(profile)
    return WorkspaceRetainedOutputAuthorityProvider(profile=profile, surface=surface, policy=policy)


def retained_output_authority_provider_for_context(
    context: RunAuthorityContext,
    *,
    shepherd_context: Mapping[str, object] | None = None,
    transaction_kind: str = "retained_output_selection",
) -> WorkspaceRetainedOutputAuthorityProvider:
    """Build a retained-output authority provider from persisted run metadata."""
    validated = validate_run_authority_context(context)
    authority_context = vcscore_authority_context_for_run_authority_context(
        validated,
        transaction_kind=transaction_kind,
        shepherd_context=shepherd_context,
    )
    return WorkspaceRetainedOutputAuthorityProvider(
        profile=validated.profile,
        surface=validated.surface,
        policy=validated.policy,
        authority_context=authority_context,
    )


def retained_output_authority_provider_for_grant(
    profile: MayProfile,
    grant_clamp: GitRepoGrantClamp,
) -> WorkspaceRetainedOutputAuthorityProvider:
    """Build a retained-output authority provider from a descriptor-backed GitRepo grant."""
    surface = workspace_retained_output_authority_surface_for_grant(grant_clamp.effective)
    policy = workspace_retained_output_authority_policy_for_grant(profile, grant_clamp)
    return WorkspaceRetainedOutputAuthorityProvider(profile=profile, surface=surface, policy=policy)
