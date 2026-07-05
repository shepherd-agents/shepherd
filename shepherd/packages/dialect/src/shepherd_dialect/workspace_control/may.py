"""Slice-local ``may=`` lowering for workspace-control authority.

This module is intentionally narrower than the dialect-wide ``may=`` story.
It supports the current workspace-control filesystem/retained-output vertical
slice while older confinement and nucleus paths keep their existing lowering
rules until the full authority model converges.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

MayProfileName = Literal["ReadOnly", "ReadWrite", "Permissive"]
WorkspaceRepoAuthority = Literal["readonly", "readwrite"]

DEFAULT_WORKSPACE_MAY_PROFILE: MayProfileName = "ReadWrite"


class MayProfileError(ValueError):
    """Raised when a may profile cannot be used safely."""


class UnsupportedMayProfileError(MayProfileError):
    """Raised when a profile has no lowering in the workspace-control slice."""


class MayProfileWideningError(MayProfileError):
    """Raised when a caller tries to widen a task's declared authority."""


class HeterogeneousBindingAuthorityError(MayProfileError):
    """Raised when a run-wide scalar authority is read off a decision whose bindings differ.

    The S2 tripwire (Lane C LC-3): collapsing ``{docs: readonly, backend: readwrite}`` to one
    scalar either amplifies the most-restricted binding (the profile fallback would make the
    ReadOnly ``docs`` read as ``"readwrite"``) or downgrades the granted one (the any-clause
    clamp check). Any consumer that still reads a scalar on the multi-binding path fails loudly
    here instead of silently mis-enforcing; multi-binding consumers read
    :meth:`WorkspaceAuthorityDecision.repo_authority_by_binding`.
    """


@dataclass(frozen=True)
class MayProfile:
    """Normalized workspace-control may profile.

    The rank is the current filesystem/workspace ordering. ``ReadOnly``
    is a strict subset of ``ReadWrite``; ``Permissive`` remains the legacy broad
    top profile but lowers to the same workspace GitRepo authority as
    ``ReadWrite`` in this slice.
    """

    name: MayProfileName
    rank: int
    workspace_repo_authority: WorkspaceRepoAuthority
    workspace_selection_can_mutate: bool


@dataclass(frozen=True)
class WorkspaceAuthorityDecision:
    """Resolved authority facts for one workspace-control run.

    This is the first internal decision boundary for the walking
    skeleton. It records the effective profile once, then exposes the concrete
    authority facts consumed by handle acquisition and retained-output
    selection.
    """

    task_default: MayProfile
    requested: MayProfile | None
    effective: MayProfile
    gitrepo_grant_clamp: Any | None = None

    @property
    def may_profile_name(self) -> MayProfileName:
        """Return the profile name persisted on the run record."""
        return self.effective.name

    @property
    def repo_authority(self) -> WorkspaceRepoAuthority:
        """Return the temporary skeleton GitRepo authority (single-binding scalar)."""
        self._refuse_heterogeneous_scalar_read("repo_authority")
        if self.gitrepo_grant_clamp is not None and not _gitrepo_grant_clamp_allows_mutation(self.gitrepo_grant_clamp):
            return "readonly"
        return self.effective.workspace_repo_authority

    @property
    def workspace_selection_can_mutate(self) -> bool:
        """Return whether retained-output selection may mutate workspace (single-binding scalar)."""
        self._refuse_heterogeneous_scalar_read("workspace_selection_can_mutate")
        if self.gitrepo_grant_clamp is not None and not _gitrepo_grant_clamp_allows_mutation(self.gitrepo_grant_clamp):
            return False
        return self.effective.workspace_selection_can_mutate

    def _refuse_heterogeneous_scalar_read(self, scalar: str) -> None:
        """The S2 tripwire: a run-wide scalar has no sound value when bindings differ.

        Homogeneous decisions (single-binding, pure ``may=``, or all bindings agreeing) keep the
        scalar surface byte-identical; a heterogeneous per-binding decision makes any scalar read
        an amplification or loss, so it raises — turning a forgotten LC-3e call-site conversion
        into a loud seam failure instead of a silent authority bug.
        """
        per_binding = self.repo_authority_by_binding()
        if len(set(per_binding.values())) > 1:
            raise HeterogeneousBindingAuthorityError(
                f"WorkspaceAuthorityDecision.{scalar} was read on a per-binding decision whose "
                f"bindings differ ({per_binding}); a run-wide scalar would amplify or lose a "
                "binding's authority. Multi-binding consumers must read repo_authority_by_binding()."
            )

    def repo_authority_by_binding(self) -> dict[str, WorkspaceRepoAuthority]:
        """Return per-binding GitRepo authority — the LC-3c/S2 non-collapsing view.

        The scalar :attr:`repo_authority` collapses to ``"readonly"`` when *any* clause forbids
        mutation, which would silently downgrade a ``backend: ReadWrite`` binding in a
        ``docs: ReadOnly / backend: ReadWrite`` run. This map preserves each binding's authority:
        a clause is read-only iff its own ``mutates`` is ``False``; otherwise it inherits the
        effective profile authority. Empty when no per-binding grant is present (the pure ``may=``
        path). The multi-binding run path (LC-3d/LC-3e) reads this, never the scalar, so no
        downstream site collapses per-binding authority to one run-wide value.
        """
        if self.gitrepo_grant_clamp is None:
            return {}
        effective = getattr(self.gitrepo_grant_clamp, "effective", None)
        clauses = getattr(effective, "clauses", ())
        return {
            clause.binding_ref: ("readonly" if clause.mutates is False else self.effective.workspace_repo_authority)
            for clause in clauses
        }


_MAY_PROFILES: dict[str, MayProfile] = {
    "ReadOnly": MayProfile(
        name="ReadOnly",
        rank=10,
        workspace_repo_authority="readonly",
        workspace_selection_can_mutate=False,
    ),
    "ReadWrite": MayProfile(
        name="ReadWrite",
        rank=20,
        workspace_repo_authority="readwrite",
        workspace_selection_can_mutate=True,
    ),
    "Permissive": MayProfile(
        name="Permissive",
        rank=30,
        workspace_repo_authority="readwrite",
        workspace_selection_can_mutate=True,
    ),
}


def supported_may_profile_names() -> tuple[MayProfileName, ...]:
    """Return the supported names in authority order."""
    return ("ReadOnly", "ReadWrite", "Permissive")


def normalize_may_profile(value: str) -> MayProfile:
    """Return a canonical profile for ``value`` or fail closed."""
    if not isinstance(value, str) or not value:
        raise UnsupportedMayProfileError(f"may={value!r} is not a supported workspace-control profile")
    profile = _MAY_PROFILES.get(value)
    if profile is None:
        raise UnsupportedMayProfileError(f"may={value!r} has no workspace authority lowering")
    return profile


def canonical_may_profile_name(value: str) -> MayProfileName:
    """Return the canonical spelling for a supported profile."""
    return normalize_may_profile(value).name


def may_profile_allows(requested: MayProfile, ceiling: MayProfile) -> bool:
    """Return whether ``requested`` is no broader than ``ceiling``."""
    return requested.rank <= ceiling.rank


def resolve_run_may_profile(*, task_default: str, requested: str | None) -> MayProfile:
    """Resolve a run's effective profile without allowing call-site widening."""
    return resolve_workspace_authority_decision(task_default=task_default, requested=requested).effective


def resolve_workspace_authority_decision(
    *,
    task_default: str,
    requested: str | None,
    gitrepo_grant: Any | None = None,
) -> WorkspaceAuthorityDecision:
    """Resolve the workspace-control authority facts for a run."""
    ceiling = normalize_may_profile(task_default)
    requested_profile = normalize_may_profile(requested) if requested is not None else None
    effective = ceiling if requested_profile is None else requested_profile
    if not may_profile_allows(effective, ceiling):
        raise MayProfileWideningError(f"may={effective.name!r} exceeds task may_default={ceiling.name!r}")
    return WorkspaceAuthorityDecision(
        task_default=ceiling,
        requested=requested_profile,
        effective=effective,
        gitrepo_grant_clamp=_clamp_gitrepo_grant_to_profile(effective, gitrepo_grant),
    )


def repo_authority_for_may(value: str) -> WorkspaceRepoAuthority:
    """Lower a supported profile to the temporary GitRepo authority."""
    return normalize_may_profile(value).workspace_repo_authority


def _clamp_gitrepo_grant_to_profile(profile: MayProfile, gitrepo_grant: Any | None) -> Any | None:
    if gitrepo_grant is None:
        return None
    from shepherd_dialect.workspace_control.authority import (
        GitRepoGrantClause,
        GitRepoGrantDescriptor,
        clamp_gitrepo_grants,
    )

    # LC-3c / S1 — expand the whole-run ceiling to one clause per *requested* binding, each
    # inheriting the profile's mutation constraint. `clamp_gitrepo_grants` cross-products
    # parent x requested clauses and intersects only on exact `binding_ref` equality, so a single
    # `binding_ref="workspace"` ceiling clamped against per-binding requested clauses
    # (`docs`/`backend`) would intersect to nothing → `Match.nothing()` → deny-everything
    # (fail-closed but non-functional). Deriving the ceiling from the requested binding_refs gives
    # every requested binding a ceiling clause that inherits the constraint:
    #   * `may="ReadOnly"` (can_mutate False) ⇒ every binding's ceiling clause is `mutates=False`,
    #     so *every* binding clamps to read-only (the S1 invariant), and
    #   * a `Permissive`/`ReadWrite` ceiling is `mutates=None` (unconstrained) ⇒ the per-binding
    #     requested clause stands.
    # The constraint is *inherited*, never defaulted-open: a binding is never left `mutates=None`
    # under a read-only profile. A single-binding requested grant (one `"workspace"` clause)
    # reproduces exactly the prior single-clause ceiling — byte-identical for the v0.1 path.
    profile_mutates = False if not profile.workspace_selection_can_mutate else None
    requested_binding_refs = tuple(dict.fromkeys(clause.binding_ref for clause in gitrepo_grant.clauses))
    if not requested_binding_refs:
        requested_binding_refs = ("workspace",)
    profile_grant = GitRepoGrantDescriptor(
        grant_ref=f"workspace-effective-profile:{profile.name}",
        clauses=tuple(
            GitRepoGrantClause(binding_ref=binding_ref, mutates=profile_mutates)
            for binding_ref in requested_binding_refs
        ),
    )
    return clamp_gitrepo_grants(
        parent_ceiling=profile_grant,
        requested=gitrepo_grant,
        grant_ref=f"workspace-effective:{profile.name}:{gitrepo_grant.digest}",
    )


def _gitrepo_grant_clamp_allows_mutation(grant_clamp: Any) -> bool:
    effective = getattr(grant_clamp, "effective", None)
    clauses = getattr(effective, "clauses", ())
    return any(getattr(clause, "mutates", None) is not False for clause in clauses)
