from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from vcs_core._errors import WorldQuiescenceError
from vcs_core._substrate_runtime import build_builtin_substrate_context
from vcs_core._world_substrate_adapters import TaskTraceSubstrateDriver
from vcs_core.recording import NestedParentAuthorization
from vcs_core.store import Store
from vcs_core.substrates import FilesystemSubstrate, MarkerSubstrate
from vcs_core.vcscore import VcsCore


def _hybrid_payload(frontier: str) -> dict[str, Any]:
    return {
        "trace_runtime": "shepherd.trace.provider-neutral.v1",
        "trace_owner_id": "task:a2:parent",
        "frontier_id": frontier,
        "run_ref": "run_a2_parent",
        "identity_domain": "vcscore.canonical.v2",
        "events": [{"id": "e1", "kind": "run.lifecycle", "transition": "finished"}],
        "causal_edges": [],
        "owner_paths": {"task:a2:parent": ["e1"]},
    }


def _make_trace_vcscore(root: Path) -> VcsCore:
    root.mkdir()
    store = Store(str(root / ".vcscore"))
    ctx = build_builtin_substrate_context(store, workspace=root, config={})
    mg = VcsCore(
        str(root),
        substrates=[MarkerSubstrate(ctx), FilesystemSubstrate(ctx), TaskTraceSubstrateDriver()],
        store=store,
    )
    mg.activate()
    return mg


def test_parent_world_mutation_refuses_while_adopt_child_operation_is_live(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    child = mg.fork(mg.ground, "quiescence-child")
    parent_operation = mg._pipeline.begin_operation(
        handle_id="parent-runtime",
        kind="marker.runtime",
        scope=mg.ground,
        session_id=mg._session_id,
    )
    nested = NestedParentAuthorization(
        parent_scope_ref=mg.ground.ref,
        child_scope_ref=child.ref,
        ancestry_chain=(mg.ground.ref,),
    )
    child_operation = mg._pipeline.begin_operation(
        handle_id="child-runtime",
        kind="marker.runtime",
        scope=child,
        nested_parent=nested,
        world_disposition="adopt",
        session_id=mg._session_id,
    )
    try:
        with pytest.raises(WorldQuiescenceError, match="live child operation child-runtime"):
            mg.exec("marker", "mark", scope=mg.ground, label="parent")
    finally:
        if mg._pipeline.current_operation() is not None:
            mg._pipeline.abort_operation(handle_id=child_operation.handle_id)
        if mg._pipeline.current_operation() is not None:
            mg._pipeline.abort_operation(handle_id=parent_operation.handle_id)
        mg._pipeline.reset()
        mg.discard(child)


def test_parent_trace_append_is_exempt_while_adopt_child_operation_is_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_trace_vcscore(tmp_path / "ws")
    child = None
    try:
        child = mg.fork(mg.ground, "trace-quiescence-child")
        parent_operation = mg._pipeline.begin_operation(
            handle_id="parent-runtime",
            kind="marker.runtime",
            scope=mg.ground,
            session_id=mg._session_id,
        )
        nested = NestedParentAuthorization(
            parent_scope_ref=mg.ground.ref,
            child_scope_ref=child.ref,
            ancestry_chain=(mg.ground.ref,),
        )
        child_operation = mg._pipeline.begin_operation(
            handle_id="child-runtime",
            kind="marker.runtime",
            scope=child,
            nested_parent=nested,
            world_disposition="adopt",
            session_id=mg._session_id,
        )
        try:
            outcome = mg.exec("trace", "append", scope=mg.ground, payload=_hybrid_payload("frontier:parent"))
        finally:
            if mg._pipeline.current_operation() is not None:
                mg._pipeline.abort_operation(handle_id=child_operation.handle_id)
            if mg._pipeline.current_operation() is not None:
                mg._pipeline.abort_operation(handle_id=parent_operation.handle_id)
            mg._pipeline.reset()
        assert outcome.oids
        trace_head = mg._world_storage().read_world(mg.world_oid()).snapshot.head_for("trace")
        assert trace_head.head == outcome.oids[0]
    finally:
        if child is not None:
            mg.discard(child)
        mg.deactivate()


def test_release_child_operation_exempts_parent_from_world_quiescence_guard(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``release`` child is exempt from the world-quiescence guard (D3). Where an
    ``adopt`` child makes a parent world mutation raise ``WorldQuiescenceError`` at
    readiness admission, a ``release`` child lets the request *past* the quiescence
    guard. The boundary refusal that follows is guard #3's separate operation-handle
    concern (the child op is top-of-stack); A4's run-context restructures that so the
    parent records in its own op context — out of A2's scope. This isolates exactly
    the layer A2 owns: the quiescence exemption."""
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    child = mg.fork(mg.ground, "release-quiescence-child")
    parent_operation = mg._pipeline.begin_operation(
        handle_id="parent-runtime",
        kind="marker.runtime",
        scope=mg.ground,
        session_id=mg._session_id,
    )
    nested = NestedParentAuthorization(
        parent_scope_ref=mg.ground.ref,
        child_scope_ref=child.ref,
        ancestry_chain=(mg.ground.ref,),
    )
    child_operation = mg._pipeline.begin_operation(
        handle_id="child-runtime",
        kind="marker.runtime",
        scope=child,
        nested_parent=nested,
        world_disposition="release",
        session_id=mg._session_id,
    )
    try:
        # WorldQuiescenceError subclasses RuntimeError, so the broad catch + the two
        # assertions below pin this precisely to the boundary refusal, not quiescence.
        with pytest.raises(RuntimeError) as exc_info:
            mg.exec("marker", "mark", scope=mg.ground, label="parent")
        # Past the quiescence guard: an adopt child raises WorldQuiescenceError here.
        assert not isinstance(exc_info.value, WorldQuiescenceError)
        # Refused instead by the boundary operation-handle guard (#3).
        assert "belongs to" in str(exc_info.value)
    finally:
        if mg._pipeline.current_operation() is not None:
            mg._pipeline.abort_operation(handle_id=child_operation.handle_id)
        if mg._pipeline.current_operation() is not None:
            mg._pipeline.abort_operation(handle_id=parent_operation.handle_id)
        mg._pipeline.reset()
        mg.discard(child)


def test_parent_trace_append_is_scope_ref_neutral_while_adopt_child_operation_is_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exempt parent trace append lands on the trace substrate but does NOT
    advance the parent's merge-relevant scope ref (Q1c: trace appends are
    scope-ref-neutral — they buffer on the op ref). Proven by the parent staying
    mergeable-from the child across the append."""
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    mg = _make_trace_vcscore(tmp_path / "ws")
    child = None
    try:
        child = mg.fork(mg.ground, "trace-neutral-child")
        before = mg.store.resolve_to_commit(mg.ground.ref)
        assert before is not None
        # The child is mergeable from the parent before the append (control).
        mg.store.assert_mergeable(child, mg.ground.ref)

        parent_operation = mg._pipeline.begin_operation(
            handle_id="parent-runtime",
            kind="marker.runtime",
            scope=mg.ground,
            session_id=mg._session_id,
        )
        nested = NestedParentAuthorization(
            parent_scope_ref=mg.ground.ref,
            child_scope_ref=child.ref,
            ancestry_chain=(mg.ground.ref,),
        )
        child_operation = mg._pipeline.begin_operation(
            handle_id="child-runtime",
            kind="marker.runtime",
            scope=child,
            nested_parent=nested,
            world_disposition="adopt",
            session_id=mg._session_id,
        )
        try:
            outcome = mg.exec("trace", "append", scope=mg.ground, payload=_hybrid_payload("frontier:neutral"))
            assert outcome.oids
            after = mg.store.resolve_to_commit(mg.ground.ref)
            assert after is not None
            # Scope-ref-neutral: the parent ref that assert_mergeable gates on did
            # not move, so the child remains mergeable despite the live append.
            assert str(after.id) == str(before.id)
            mg.store.assert_mergeable(child, mg.ground.ref)
        finally:
            if mg._pipeline.current_operation() is not None:
                mg._pipeline.abort_operation(handle_id=child_operation.handle_id)
            if mg._pipeline.current_operation() is not None:
                mg._pipeline.abort_operation(handle_id=parent_operation.handle_id)
            mg._pipeline.reset()
    finally:
        if child is not None:
            mg.discard(child)
        mg.deactivate()


def test_world_quiescence_error_message_is_byte_exact(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the full actionable ``WorldQuiescenceError`` prescription a supervisor sees
    — the whole message, not just a substring (house byte-exact full-string == style).

    Note: the quiescence guard keys on the *persisted nested edge*, not the env flag.
    The flag gates whether a nested op can be *opened* via the high-level path (see
    ``test_nested_operations_run_path.py``); a hand-built nested op enforces quiescence
    regardless, which is why this pins the message at the recording layer directly."""
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    child = mg.fork(mg.ground, "byte-exact-child")
    parent_operation = mg._pipeline.begin_operation(
        handle_id="parent-runtime",
        kind="marker.runtime",
        scope=mg.ground,
        session_id=mg._session_id,
    )
    nested = NestedParentAuthorization(
        parent_scope_ref=mg.ground.ref,
        child_scope_ref=child.ref,
        ancestry_chain=(mg.ground.ref,),
    )
    child_operation = mg._pipeline.begin_operation(
        handle_id="child-runtime",
        kind="marker.runtime",
        scope=child,
        nested_parent=nested,
        world_disposition="adopt",
        session_id=mg._session_id,
    )
    try:
        with pytest.raises(WorldQuiescenceError) as exc_info:
            mg.exec("marker", "mark", scope=mg.ground, label="parent")
        assert str(exc_info.value) == (
            "Cannot execute marker.mark: live child operation child-runtime on "
            f"{child.ref} has world disposition 'adopt' and blocks parent mutation "
            f"on {mg.ground.ref}. Finish or archive the child operation before "
            "mutating its parent, or discard the child scope and fork fresh."
        )
    finally:
        if mg._pipeline.current_operation() is not None:
            mg._pipeline.abort_operation(handle_id=child_operation.handle_id)
        if mg._pipeline.current_operation() is not None:
            mg._pipeline.abort_operation(handle_id=parent_operation.handle_id)
        mg._pipeline.reset()
        mg.discard(child)
