"""B4b W2: the selectable arm of the dispatch bridge.

A bound selectable SPI driver with an installed store dispatches through
``mg.exec`` to a **selected, published** world revision: real ``base_heads``
from the binding's current head, carried heads spelled as unchanged
selections, one journaled CAS-protected publication. The PD3a refusal stays
for uninstalled stores; an interrupted append recovers via the standard
operation journal (S1 check 4, graduated — execplan review fix 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from vcs_core._fork_hints import ForkHints
from vcs_core._substrate_driver import (
    BaseSubstrateDriver,
    CapabilitySet,
    CommandRequest,
    CommandSpec,
    DriverContext,
    DriverIngressResult,
    DriverSchema,
    ParamSpec,
)
from vcs_core._substrate_runtime import build_builtin_substrate_context
from vcs_core._world_substrate_adapters import TaskTraceSubstrateDriver
from vcs_core.store import Store
from vcs_core.substrates import FilesystemSubstrate, MarkerSubstrate
from vcs_core.vcscore import VcsCore

from ...support.overlays import MockOverlayBackend


def _hybrid_payload(frontier: str) -> dict[str, Any]:
    return {
        "trace_runtime": "shepherd.trace.provider-neutral.v1",
        "trace_owner_id": "task:w2:run1",
        "frontier_id": frontier,
        "run_ref": "run_w2",
        "identity_domain": "vcscore.canonical.v2",
        "events": [{"id": "e1", "kind": "run.lifecycle", "transition": "finished"}],
        "causal_edges": [],
        "owner_paths": {"task:w2:run1": ["e1"]},
    }


def _make_env(root: Path) -> tuple[VcsCore, MockOverlayBackend]:
    root.mkdir(exist_ok=True)
    store = Store(str(root / ".vcscore"))
    ctx = build_builtin_substrate_context(store, workspace=root, config={})
    backend = MockOverlayBackend()
    mg = VcsCore(
        str(root),
        substrates=[MarkerSubstrate(ctx), FilesystemSubstrate(ctx, backend=backend), TaskTraceSubstrateDriver()],
        store=store,
    )
    mg.activate()
    return mg, backend


def _world_publishing_run(mg: VcsCore, backend: MockOverlayBackend, name: str) -> None:
    scope = mg.fork(mg.ground, name, hints=ForkHints(isolated=True))
    backend.write_file(scope.name, f"{name}.txt", b"payload\n")
    mg.merge(scope, mg.ground)


@pytest.fixture
def env(tmp_path: Path) -> tuple[VcsCore, MockOverlayBackend]:
    mg, backend = _make_env(tmp_path / "ws")
    yield mg, backend
    mg.deactivate()


def test_first_append_on_a_fresh_repo_publishes_a_root_world(env) -> None:
    mg, _ = env
    assert mg.world_oid() is None
    outcome = mg.exec("trace", "append", scope=mg.ground, payload=_hybrid_payload("frontier:root"))
    w = mg.world_oid()
    assert w is not None
    head = mg._world_storage().read_world(w).snapshot.head_for("trace")
    assert head.head == outcome.oids[0]


def test_append_after_runs_advances_world_and_carries_workspace(env) -> None:
    mg, backend = env
    _world_publishing_run(mg, backend, "run-one")
    w0 = mg.world_oid()
    manager = mg._world_storage()
    workspace_before = manager.read_world(w0).snapshot.head_for("workspace")

    mg.exec("trace", "append", scope=mg.ground, payload=_hybrid_payload("frontier:1"))
    w1 = mg.world_oid()
    assert w1 != w0
    snapshot = manager.read_world(w1).snapshot
    assert snapshot.head_for("trace") is not None
    assert snapshot.head_for("workspace").head == workspace_before.head


def test_payload_reads_back_field_complete(env) -> None:
    mg, backend = env
    _world_publishing_run(mg, backend, "run-one")
    payload = _hybrid_payload("frontier:complete")
    outcome = mg.exec("trace", "append", scope=mg.ground, payload=payload)
    stored = mg._world_storage().store("store_trace").read_revision_payload(outcome.oids[0])
    for field in ("trace_runtime", "trace_owner_id", "frontier_id", "run_ref",
                  "identity_domain", "events", "causal_edges", "owner_paths"):
        assert stored[field] == payload[field], field


def test_public_selected_binding_revision_read_resolves_current_head(env) -> None:
    mg, backend = env
    assert mg.read_selected_binding_revision("trace") is None
    assert mg.read_selected_binding_revision("shepherd.tasks") is None

    _world_publishing_run(mg, backend, "run-one")
    assert mg.read_selected_binding_revision("trace") is None

    first = _hybrid_payload("frontier:first")
    second = _hybrid_payload("frontier:second")
    mg.exec("trace", "append", scope=mg.ground, payload=first)
    assert mg.read_selected_binding_revision("trace")["frontier_id"] == "frontier:first"

    mg.exec("trace", "append", scope=mg.ground, payload=second)
    selected = mg.read_selected_binding_revision("trace")
    assert selected is not None
    assert selected["frontier_id"] == "frontier:second"
    assert selected == mg.read_trace_revision()


def test_public_binding_revision_read_can_dereference_old_selected_heads(env) -> None:
    mg, backend = env
    _world_publishing_run(mg, backend, "run-one")

    first = mg.exec("trace", "append", scope=mg.ground, payload=_hybrid_payload("frontier:first"))
    second = mg.exec("trace", "append", scope=mg.ground, payload=_hybrid_payload("frontier:second"))

    selected = mg.read_selected_binding_revision_with_head("trace")
    assert selected is not None
    assert selected.binding == "trace"
    assert selected.store_id == "store_trace"
    assert selected.head == second.oids[0]
    assert selected.payload["frontier_id"] == "frontier:second"

    old_payload = mg.read_binding_revision(
        "trace",
        first.oids[0],
        store_id="store_trace",
        resource_id="shepherd-trace:main",
    )
    assert old_payload["frontier_id"] == "frontier:first"


def test_public_selected_binding_revision_requires_binding_name(env) -> None:
    mg, _ = env
    with pytest.raises(ValueError, match="binding_name is required"):
        mg.read_selected_binding_revision("")


def test_second_append_chains_parent_heads(env) -> None:
    mg, backend = env
    _world_publishing_run(mg, backend, "run-one")
    first = mg.exec("trace", "append", scope=mg.ground, payload=_hybrid_payload("frontier:1"))
    second = mg.exec("trace", "append", scope=mg.ground, payload=_hybrid_payload("frontier:2"))
    metadata = mg._world_storage().store("store_trace").read_revision_metadata(second.oids[0])
    assert metadata.parent_heads == (first.oids[0],)


def test_trace_head_survives_a_subsequent_run_merge(env) -> None:
    mg, backend = env
    _world_publishing_run(mg, backend, "run-one")
    outcome = mg.exec("trace", "append", scope=mg.ground, payload=_hybrid_payload("frontier:1"))
    _world_publishing_run(mg, backend, "run-two")
    surviving = mg._world_storage().read_world(mg.world_oid()).snapshot.head_for("trace")
    assert surviving is not None
    assert surviving.head == outcome.oids[0]


@dataclass(frozen=True)
class _UninstalledSelectableDriver(BaseSubstrateDriver):
    store_id: str = "store_nowhere"
    binding: str = "nowhere"
    role: str = "test.Nowhere"
    driver_id: str = "test.nowhere"
    driver_version: str = "v0.1"

    @property
    def capabilities(self) -> CapabilitySet:
        return CapabilitySet(accepts=frozenset({CommandRequest}), selectable=True)

    def describe(self) -> DriverSchema:
        return DriverSchema(
            driver_id=self.driver_id,
            driver_version=self.driver_version,
            capabilities=self.capabilities,
            commands={"append": CommandSpec(description="x", params={"payload": ParamSpec(type="object")})},
        )

    def prepare(self, context: DriverContext, request: Any) -> DriverIngressResult:
        raise AssertionError("an uninstalled selectable driver must refuse before dispatch")


def test_uninstalled_selectable_driver_still_refuses(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    store = Store(str(root / ".vcscore"))
    ctx = build_builtin_substrate_context(store, workspace=root, config={})
    mg = VcsCore(str(root), substrates=[MarkerSubstrate(ctx), FilesystemSubstrate(ctx), _UninstalledSelectableDriver()], store=store)
    mg.activate()
    try:
        with pytest.raises(ValueError, match="store 'store_nowhere' is not in the world installation"):
            mg.exec("nowhere", "append", scope=mg.ground, payload={})
    finally:
        mg.deactivate()


def test_interrupted_append_recovers_standard_never_half_published(tmp_path: Path) -> None:
    """S1 check 4, graduated: the acceptance's recovery line (review fix 3)."""
    root = tmp_path / "ws"
    mg, backend = _make_env(root)
    _world_publishing_run(mg, backend, "run-one")
    pre = mg.world_oid()
    # Interrupt: a world-operation journal opened for an append that never finishes.
    mg._world_storage().open_operation_journal(
        operation_id="op-w2-interrupted",
        operation_kind="trace.append",
        target_ref=mg.ground.ref,
        input_world_oid=pre,
    )
    mg.deactivate()

    mg2, _ = _make_env(root)
    try:
        # Standard recovery: the session activates; nothing half-published —
        # the ground world is exactly what it was before the interruption.
        assert mg2.world_oid() == pre
        assert "trace" not in mg2._world_storage().read_world(pre).snapshot.by_binding()
        # Fail-closed: the orphaned world-operation journal readiness-blocks
        # further appends until the operator salvages it…
        with pytest.raises(Exception, match="op-w2-interrupted"):
            mg2.exec("trace", "append", scope=mg2.ground, payload=_hybrid_payload("frontier:blocked"))
        # The world-operation journal family's salvage sequence (not the
        # recording pipeline's archive_orphaned_operations): fail, then archive.
        mg2._world_storage().fail_operation_journal("op-w2-interrupted", error="interrupted by test")
        mg2._world_storage().archive_operation_journal("op-w2-interrupted")
        # …after which the route works again and the append lands cleanly.
        mg2.exec("trace", "append", scope=mg2.ground, payload=_hybrid_payload("frontier:post"))
        assert mg2.world_oid() != pre
    finally:
        mg2.deactivate()
