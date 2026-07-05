"""P-030 Lane C LC-3c / S1 — whole-run ceiling expansion (adversarial).

The soundness-critical trap: a whole-run ceiling (`may="ReadOnly"` → one
`binding_ref="workspace"` clause) intersected against per-binding requested clauses
(`docs`/`backend`) via `clamp_gitrepo_grants` (exact `binding_ref` equality) yields zero
effective clauses → `Match.nothing()` → the carrier lane denies everything (fail-closed but
non-functional). LC-3c expands the ceiling to one clause per requested binding, each inheriting
the profile constraint.

These tests try to *refute* two claims, not confirm a happy path:
  S1a — a `may="ReadOnly"` run clamps **every** requested binding to read-only (not just one);
  S1b — the ceiling **constrains** every binding, never defaults a binding open (`mutates=None`)
        under a read-only profile;
and the regression guard S1c — the single-binding path is byte-identical (one `workspace` clause).
"""

from __future__ import annotations

import pytest

from shepherd_dialect.workspace_control.authority import (
    GitRepoGrantClause,
    GitRepoGrantDescriptor,
)
from shepherd_dialect.workspace_control.may import (
    HeterogeneousBindingAuthorityError,
    resolve_workspace_authority_decision,
)


def _requested(*bindings: tuple[str, bool | None]) -> GitRepoGrantDescriptor:
    """Build a requested per-binding grant. `mutates=None` is ReadWrite; `False` is ReadOnly."""
    return GitRepoGrantDescriptor(
        grant_ref="signature:test",
        clauses=tuple(GitRepoGrantClause(binding_ref=name, mutates=mutates) for name, mutates in bindings),
    )


def _effective_mutates(decision) -> dict[str, bool | None]:
    return {clause.binding_ref: clause.mutates for clause in decision.gitrepo_grant_clamp.effective.clauses}


def test_s1a_readonly_run_clamps_every_binding_readonly() -> None:
    # Two ReadWrite-requested bindings under a whole-run ReadOnly ceiling.
    requested = _requested(("docs", None), ("backend", None))
    decision = resolve_workspace_authority_decision(task_default="ReadOnly", requested=None, gitrepo_grant=requested)
    effective = _effective_mutates(decision)
    # BOTH bindings must clamp to read-only — not just one, and not zero (the deny-all bug).
    assert effective == {"docs": False, "backend": False}, effective


def test_s1b_permissive_ceiling_preserves_per_binding_grants() -> None:
    # docs ReadOnly, backend ReadWrite under a Permissive ceiling — the flagship shape.
    requested = _requested(("docs", False), ("backend", None))
    decision = resolve_workspace_authority_decision(task_default="Permissive", requested=None, gitrepo_grant=requested)
    effective = _effective_mutates(decision)
    # The Permissive ceiling does not widen: docs stays read-only, backend stays writable.
    assert effective["docs"] is False, effective
    assert effective["backend"] is None, effective


def test_s1b_readonly_never_defaults_a_binding_open() -> None:
    # A ReadWrite request under ReadOnly must never leave a binding `mutates=None` (open).
    requested = _requested(("docs", None), ("backend", None))
    decision = resolve_workspace_authority_decision(task_default="ReadOnly", requested=None, gitrepo_grant=requested)
    assert all(mutates is False for mutates in _effective_mutates(decision).values())


def test_s1c_single_binding_ceiling_is_unchanged() -> None:
    # Regression: the v0.1 single-binding path (one `workspace` clause) is byte-identical.
    requested = _requested(("workspace", None))
    ro = resolve_workspace_authority_decision(task_default="ReadOnly", requested=None, gitrepo_grant=requested)
    assert _effective_mutates(ro) == {"workspace": False}
    assert ro.repo_authority == "readonly"
    rw = resolve_workspace_authority_decision(task_default="Permissive", requested=None, gitrepo_grant=requested)
    assert _effective_mutates(rw) == {"workspace": None}
    assert rw.repo_authority == "readwrite"


# --- LC-3c / S2: the non-collapsing per-binding decision view -------------------------------


def test_s2_per_binding_view_does_not_collapse_to_a_scalar() -> None:
    # docs ReadOnly, backend ReadWrite under Permissive — the flagship. The scalar `repo_authority`
    # would collapse to "readwrite" the moment ANY binding is writable (`allows_mutation` =
    # any-not-False), making the ReadOnly `docs` binding WRITABLE too — a silent authority
    # *amplification*. The S2 tripwire makes that read RAISE instead of returning the amplifying
    # scalar; the multi-binding path reads the per-binding view.
    requested = _requested(("docs", False), ("backend", None))
    decision = resolve_workspace_authority_decision(task_default="Permissive", requested=None, gitrepo_grant=requested)
    by_binding = decision.repo_authority_by_binding()
    assert by_binding == {"docs": "readonly", "backend": "readwrite"}, by_binding
    with pytest.raises(HeterogeneousBindingAuthorityError):
        _ = decision.repo_authority  # the amplifying scalar read fails loudly (S2 tripwire)


def test_s2_tripwire_covers_both_scalars_and_their_transitive_readers() -> None:
    # Both run-wide scalars fail loudly on a heterogeneous decision — which also covers every
    # transitive reader (`_runtime_may_for_workspace_authority`, placement decisions) without
    # each needing its own guard: a forgotten LC-3e call-site conversion becomes a loud seam
    # failure, not a silent authority bug.
    requested = _requested(("docs", False), ("backend", None))
    decision = resolve_workspace_authority_decision(task_default="Permissive", requested=None, gitrepo_grant=requested)
    with pytest.raises(HeterogeneousBindingAuthorityError):
        _ = decision.repo_authority
    with pytest.raises(HeterogeneousBindingAuthorityError):
        _ = decision.workspace_selection_can_mutate


def test_s2_tripwire_stays_silent_when_bindings_agree() -> None:
    # Homogeneous multi-binding decisions keep the scalar surface intact: all-ReadOnly and
    # all-ReadWrite runs read the same scalar as before (no false positives from the tripwire).
    all_ro = resolve_workspace_authority_decision(
        task_default="ReadOnly", requested=None, gitrepo_grant=_requested(("docs", None), ("backend", None))
    )
    assert all_ro.repo_authority == "readonly"
    assert all_ro.workspace_selection_can_mutate is False
    all_rw = resolve_workspace_authority_decision(
        task_default="Permissive", requested=None, gitrepo_grant=_requested(("docs", None), ("backend", None))
    )
    assert all_rw.repo_authority == "readwrite"
    assert all_rw.workspace_selection_can_mutate is True


def test_s2_readonly_run_is_readonly_per_binding() -> None:
    requested = _requested(("docs", None), ("backend", None))
    decision = resolve_workspace_authority_decision(task_default="ReadOnly", requested=None, gitrepo_grant=requested)
    assert decision.repo_authority_by_binding() == {"docs": "readonly", "backend": "readonly"}


def test_s2_single_binding_view_matches_the_scalar() -> None:
    requested = _requested(("workspace", None))
    decision = resolve_workspace_authority_decision(task_default="Permissive", requested=None, gitrepo_grant=requested)
    assert decision.repo_authority_by_binding() == {"workspace": "readwrite"}
    assert decision.repo_authority == "readwrite"
