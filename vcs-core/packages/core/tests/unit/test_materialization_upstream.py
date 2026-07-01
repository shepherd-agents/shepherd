"""Tests for upstream-aware planner preflight behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
from vcs_core._upstream import PreflightResult
from vcs_core.materialization import MaterializationPreflightError, MaterializationUnit, plan_materialization
from vcs_core.store import Store
from vcs_core.vcscore import VcsCore


def _record_pending_replay(store: Store, *, materializer_key: str) -> None:
    if store.is_empty:
        store.create_root_commit()
    task = store.fork(store.GROUND_REF, "task-upstream")
    store._emit_effect(
        task,
        "CustomReplay",
        {"materializer_key": materializer_key},
        substrate="custom",
    )
    store.merge(task, store.GROUND_REF)


class _FakeUpstreamMaterializer:
    materializer_key = "custom.sqlite"

    def __init__(
        self,
        *,
        units: tuple[MaterializationUnit, ...],
        preflight: dict[str, PreflightResult] | None = None,
    ) -> None:
        self._units = units
        self._preflight = preflight or {}
        self.apply_calls = 0

    def collect_units(self, *, pending_commits, diff, status):  # type: ignore[no-untyped-def]
        del pending_commits, diff, status
        return self._units

    def preflight_units(self, units, *, mode="pure"):  # type: ignore[no-untyped-def]
        assert mode in {"pure", "recording"}
        assert tuple(units) == self._units
        return self._preflight

    def apply_units(self, units):  # type: ignore[no-untyped-def]
        del units
        self.apply_calls += 1


class _FakeNonPreflightMaterializer:
    materializer_key = "custom.sqlite"

    def __init__(self, *, units: tuple[MaterializationUnit, ...]) -> None:
        self._units = units

    def collect_units(self, *, pending_commits, diff, status):  # type: ignore[no-untyped-def]
        del pending_commits, diff, status
        return self._units

    def apply_units(self, units):  # type: ignore[no-untyped-def]
        del units


def test_plan_materialization_rejects_upstream_unit_without_basis_token(workspace: Path) -> None:
    store = Store(str(workspace / ".vcscore"))
    _record_pending_replay(store, materializer_key="custom.sqlite")
    materializer = _FakeUpstreamMaterializer(
        units=(
            MaterializationUnit(
                unit_id="sqlite:main",
                materializer_key="custom.sqlite",
                substrate="sqlite",
                target_id="sqlite:main",
                reversibility="auto",
                commit_index=0,
                upstream_aware=True,
                frontier="frontier-1",
            ),
        )
    )

    with pytest.raises(RuntimeError, match="missing a basis_token"):
        plan_materialization(store, materializers=(materializer,))


def test_plan_materialization_rejects_upstream_unit_with_ambiguous_basis(workspace: Path) -> None:
    store = Store(str(workspace / ".vcscore"))
    _record_pending_replay(store, materializer_key="custom.sqlite")
    materializer = _FakeUpstreamMaterializer(
        units=(
            MaterializationUnit(
                unit_id="sqlite:main-a",
                materializer_key="custom.sqlite",
                substrate="sqlite",
                target_id="sqlite:main",
                reversibility="auto",
                commit_index=0,
                upstream_aware=True,
                basis_token="basis-a",
                frontier="frontier-1",
            ),
            MaterializationUnit(
                unit_id="sqlite:main-b",
                materializer_key="custom.sqlite",
                substrate="sqlite",
                target_id="sqlite:main",
                reversibility="auto",
                commit_index=1,
                upstream_aware=True,
                basis_token="basis-b",
                frontier="frontier-1",
            ),
        )
    )

    with pytest.raises(RuntimeError, match="exactly one basis_token"):
        plan_materialization(store, materializers=(materializer,))


def test_apply_materialization_is_not_called_when_preflight_fails(workspace: Path) -> None:
    class _Substrate:
        name = "sqlite"
        commands = {}
        effects = {}

        def __init__(self, materializer: _FakeUpstreamMaterializer) -> None:
            self._materializer = materializer

        def bind_pipeline(self, pipeline, *, scope_queries=None) -> None:  # type: ignore[no-untyped-def]
            del pipeline, scope_queries

        def activate(self) -> None:
            pass

        def deactivate(self) -> None:
            pass

        def authority(self):  # type: ignore[no-untyped-def]
            return None

        def python_patches(self) -> tuple[object, ...]:
            return ()

        def materializers(self):  # type: ignore[no-untyped-def]
            return (self._materializer,)

    store = Store(str(workspace / ".vcscore"))
    _record_pending_replay(store, materializer_key="custom.sqlite")
    materializer = _FakeUpstreamMaterializer(
        units=(
            MaterializationUnit(
                unit_id="sqlite:main",
                materializer_key="custom.sqlite",
                substrate="sqlite",
                target_id="sqlite:main",
                reversibility="auto",
                commit_index=0,
                upstream_aware=True,
                basis_token="basis-1",
                frontier="frontier-1",
            ),
        ),
        preflight={
            "sqlite:main": PreflightResult(
                status="stale",
                reason="upstream token advanced",
                observed_token="basis-2",
            )
        },
    )
    mg = VcsCore(str(workspace), substrates=[_Substrate(materializer)], store=store)  # type: ignore[list-item]
    mg.activate()
    try:
        with pytest.raises(MaterializationPreflightError, match="preflight failed"):
            mg.push()
        assert materializer.apply_calls == 0
    finally:
        mg.deactivate()


def test_plan_materialization_rejects_missing_upstream_preflight_verdict(workspace: Path) -> None:
    store = Store(str(workspace / ".vcscore"))
    _record_pending_replay(store, materializer_key="custom.sqlite")
    materializer = _FakeUpstreamMaterializer(
        units=(
            MaterializationUnit(
                unit_id="sqlite:main",
                materializer_key="custom.sqlite",
                substrate="sqlite",
                target_id="sqlite:main",
                reversibility="auto",
                commit_index=0,
                upstream_aware=True,
                basis_token="basis-1",
                frontier="frontier-1",
            ),
        ),
        preflight={},
    )

    with pytest.raises(RuntimeError, match="must return a verdict"):
        plan_materialization(store, materializers=(materializer,))


def test_plan_materialization_rejects_upstream_unit_without_preflight_provider(workspace: Path) -> None:
    store = Store(str(workspace / ".vcscore"))
    _record_pending_replay(store, materializer_key="custom.sqlite")
    materializer = _FakeNonPreflightMaterializer(
        units=(
            MaterializationUnit(
                unit_id="sqlite:main",
                materializer_key="custom.sqlite",
                substrate="sqlite",
                target_id="sqlite:main",
                reversibility="auto",
                commit_index=0,
                upstream_aware=True,
                basis_token="basis-1",
                frontier="frontier-1",
            ),
        )
    )

    with pytest.raises(RuntimeError, match="preflight-capable materializer"):
        plan_materialization(store, materializers=(materializer,))


def test_plan_materialization_skip_preflight_allows_verify_replanning(workspace: Path) -> None:
    store = Store(str(workspace / ".vcscore"))
    _record_pending_replay(store, materializer_key="custom.sqlite")
    materializer = _FakeNonPreflightMaterializer(
        units=(
            MaterializationUnit(
                unit_id="sqlite:main",
                materializer_key="custom.sqlite",
                substrate="sqlite",
                target_id="sqlite:main",
                reversibility="auto",
                commit_index=0,
                upstream_aware=True,
                basis_token="basis-1",
                frontier="frontier-1",
            ),
        )
    )

    planned = plan_materialization(store, materializers=(materializer,), skip_preflight=True)
    assert [unit.unit_id for unit in planned.units] == ["sqlite:main"]


def test_filesystem_materializer_preflight_is_noop(workspace: Path) -> None:
    store = Store(str(workspace / ".vcscore"))
    mg = VcsCore(str(workspace), store=store)
    mg.activate()
    try:
        task = mg.fork(mg.ground, "task-filesystem-noop")
        store._emit_effect(
            task,
            "FileCreate",
            {"path": "preflight.txt"},
            substrate="filesystem",
            workspace_changes=(("preflight.txt", b"payload"),),
        )
        mg.merge(task, mg.ground)

        planned = plan_materialization(store)
        assert [unit.unit_id for unit in planned.units] == ["filesystem:workspace"]
    finally:
        mg.deactivate()
