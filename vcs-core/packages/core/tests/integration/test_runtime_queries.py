from __future__ import annotations

from pathlib import Path

import pytest
from vcs_core._claims import ClaimConflictError
from vcs_core._substrate_runtime import build_builtin_substrate_context
from vcs_core.store import Store
from vcs_core.substrates import MarkerSubstrate
from vcs_core.vcscore import VcsCore


def _vcscore(workspace: Path) -> VcsCore:
    store = Store(str(workspace / ".vcscore"))
    marker = MarkerSubstrate(build_builtin_substrate_context(store))
    mg = VcsCore(str(workspace), substrates=[marker], store=store)
    mg.activate()
    return mg


def test_runtime_binding_reports_nearest_carrier_for_nonisolated_child(workspace: Path) -> None:
    mg = _vcscore(workspace)
    try:
        parent = mg.fork(mg.ground, "task-parent", hints={"isolated": True})
        child = mg.fork(parent, "tool-child", hints={"isolated": False})

        mg._register_carrier("sqlite", "sqlite:main", parent)

        nearest = mg._runtime.nearest_carrier_scope("sqlite", "sqlite:main", child)

        assert nearest == parent
        assert mg._runtime.can_create_carrier("sqlite", "sqlite:main", child) is False
    finally:
        mg.deactivate()


def test_runtime_binding_allows_new_carrier_for_isolated_branch(workspace: Path) -> None:
    mg = _vcscore(workspace)
    try:
        parent = mg.fork(mg.ground, "task-parent", hints={"isolated": True})
        child = mg.fork(parent, "task-child", hints={"isolated": True})

        mg._register_carrier("sqlite", "sqlite:main", parent)

        nearest = mg._runtime.nearest_carrier_scope("sqlite", "sqlite:main", child)

        assert nearest == parent
        assert mg._runtime.can_create_carrier("sqlite", "sqlite:main", child) is True
    finally:
        mg.deactivate()


def test_claim_registry_defaults_real_target_to_nonsuppressing_policy(workspace: Path) -> None:
    mg = _vcscore(workspace)
    try:
        db_path = workspace / "data.db"
        claim = mg._register_claim(
            substrate="sqlite",
            target_id="sqlite:main",
            path=db_path,
            policy="exclusive",
        )

        looked_up = mg._runtime.lookup_claim(db_path)

        assert looked_up == claim
        assert looked_up is not None
        assert looked_up.policy == "exclusive"
    finally:
        mg.deactivate()


def test_claim_registry_marks_shadow_paths_as_authoritative_internal_state(workspace: Path) -> None:
    mg = _vcscore(workspace)
    try:
        shadow_path = workspace / ".vcscore" / "runtime" / "sqlite-main.db"
        claim = mg._register_claim(
            substrate="sqlite",
            target_id="sqlite:main",
            path=shadow_path,
            policy="authoritative_suppress_fs",
        )

        looked_up = mg._runtime.lookup_claim(shadow_path)

        assert looked_up == claim
        assert looked_up is not None
        assert looked_up.policy == "authoritative_suppress_fs"
    finally:
        mg.deactivate()


def test_vcscore_claim_registry_rejects_conflicting_exact_path_claims(workspace: Path) -> None:
    mg = _vcscore(workspace)
    try:
        path = workspace / "data.db"
        mg._register_claim(
            substrate="sqlite",
            target_id="sqlite:main",
            path=path,
            policy="exclusive",
        )

        with pytest.raises(ClaimConflictError, match="already claimed by sqlite:sqlite:main"):
            mg._register_claim(
                substrate="filesystem",
                target_id="workspace",
                path=path,
                policy="observe",
            )
    finally:
        mg.deactivate()
