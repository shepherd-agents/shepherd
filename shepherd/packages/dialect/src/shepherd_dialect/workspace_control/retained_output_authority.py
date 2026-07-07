"""Retained-output authority provider for workspace-control."""

from __future__ import annotations

from dataclasses import dataclass, replace
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
    """Evaluate retained-output settlement (selection or application) through helper-built ``Match``.

    ``plan_route`` labels the settlement lane on the PermissionPlan and must equal the vcs-core
    kind→route derivation for the settling verb (T1 D7); the authority *surface* (and its
    digests) stays exactly as recorded at run time — the granted surface is verb-independent,
    only the route names the lane.
    """

    profile: MayProfile
    surface: GitRepoAuthoritySurface
    policy: GitRepoAuthorityDecisionPolicy
    authority_context: Mapping[str, object] | None = None
    plan_route: str = "retained_output_selection"

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
                route=self.plan_route,
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
    """Build a retained-output authority provider from persisted run metadata.

    For ``retained_output_application`` (the ``apply`` verb, T1 D7) the classifier policy and
    PermissionPlan carry the application route while the recorded surface/digests are reused
    verbatim — the run's granted surface is verb-independent.
    """
    validated = validate_run_authority_context(context)
    authority_context = vcscore_authority_context_for_run_authority_context(
        validated,
        transaction_kind=transaction_kind,
        shepherd_context=shepherd_context,
    )
    policy = validated.policy
    surface = validated.surface
    if transaction_kind == "retained_output_application":
        # The recorded GRANT is the verb-independent authority (digest-verified against the run
        # context by validate_run_authority_context); the Match surface is its per-lane lowering.
        # Re-lower the same verified grant for the application route — the selection surface's
        # clauses are route-pinned to retained_output_selection, so application-routed views
        # would fall outside it and every apply would be refused outside_effective_match.
        surface = workspace_retained_output_authority_surface_for_grant(
            validated.effective_grant,
            route=transaction_kind,
        )
        policy = replace(policy, routes=("retained_output_application",))
    return WorkspaceRetainedOutputAuthorityProvider(
        profile=validated.profile,
        surface=surface,
        policy=policy,
        authority_context=authority_context,
        plan_route=transaction_kind,
    )


def retained_output_authority_provider_for_grant(
    profile: MayProfile,
    grant_clamp: GitRepoGrantClamp,
) -> WorkspaceRetainedOutputAuthorityProvider:
    """Build a retained-output authority provider from a descriptor-backed GitRepo grant."""
    surface = workspace_retained_output_authority_surface_for_grant(grant_clamp.effective)
    policy = workspace_retained_output_authority_policy_for_grant(profile, grant_clamp)
    return WorkspaceRetainedOutputAuthorityProvider(profile=profile, surface=surface, policy=policy)
