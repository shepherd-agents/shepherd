"""B4b W3+W4: the dialect's durable trace — both terminal paths, identity measured.

The consumer wires `build_run_trace_revision` → `append_run_trace` after real
runs through the dialect driver, then reads the revision back from the
**selected store bytes** and measures the dual-domain split: the fourth-row
`task.invocation` body digest recomputes byte-exactly and is identical across
two runs of the same task (the cross-run pattern-cache key), while the
vcscore-domain content differs per run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path
from shepherd2.kernel.canonical import canonical_digest
from vcs_core.runtime_api import Store, VcsCore, build_builtin_substrate_context
from vcs_core.runtime_substrate import TaskTraceSubstrateDriver
from vcs_core.substrates import FilesystemSubstrate, MarkerSubstrate

from shepherd_dialect import ShepherdRunDriver, append_run_trace, build_run_trace_revision

DEMO_TASK_ID = f"{__name__}:demo_body"


def demo_body(stack, **args):
    del stack, args
    return "ok"


def failing_body(stack, **args):
    del stack, args
    raise RuntimeError("body failed")


FAILING_TASK_ID = f"{__name__}:failing_body"


@pytest.fixture
def mg(tmp_path: Path, overlay_backend) -> VcsCore:
    root = tmp_path / "ws"
    root.mkdir()
    store = Store(str(root / ".vcscore"))
    ctx = build_builtin_substrate_context(store, workspace=root, config={})
    backend = overlay_backend
    vcscore = VcsCore(
        str(root),
        substrates=[
            MarkerSubstrate(ctx),
            FilesystemSubstrate(ctx, backend=backend),
            ShepherdRunDriver(),
            TaskTraceSubstrateDriver(),
        ],
        store=store,
    )
    vcscore.activate()
    yield vcscore
    vcscore.deactivate()


def _run_and_append(mg: VcsCore, *, task_id: str, args: dict[str, Any], run_ref: str) -> str:
    input_world = mg.world_oid()
    failed = False
    try:
        mg.execute_recorded("runtime", "run", scope=mg.ground, task_id=task_id, args=args, may="Permissive")
    except Exception:
        failed = True
    output_world = mg.world_oid()
    revision = build_run_trace_revision(
        run_ref=run_ref,
        trace_owner_id=f"task:{task_id}:{run_ref}",
        frontier_id=f"frontier:{run_ref}",
        task_id=task_id,
        args=args,
        may_profile="Permissive",
        terminal_status="discarded" if failed else "merged",
        input_world_oid=input_world,
        output_world_oid=None if failed else output_world,
    )
    return append_run_trace(mg, revision)


def _stored_payload(mg: VcsCore, head: str) -> dict[str, Any]:
    return mg._world_storage().store("store_trace").read_revision_payload(head)


def test_fourth_row_digest_recomputes_from_selected_store_bytes(mg: VcsCore) -> None:
    head = _run_and_append(mg, task_id=DEMO_TASK_ID, args={"n": 1}, run_ref="run-a")
    stored = _stored_payload(mg, head)
    invocation = stored["events"][0]
    assert invocation["kind"] == "task.invocation"
    assert invocation["identity_domain"] == "shepherd.kernel.canonical.v2"
    assert stored["identity_domain"] == "vcscore.canonical.v2"  # the hoisted header
    # The acceptance heart: the digest recomputes byte-exactly from STORE bytes.
    assert canonical_digest(invocation["body"]) == invocation["record_digest"]
    # And the world selected it: the head is live on the current world.
    world = mg._world_storage().read_world(mg.world_oid())
    assert world.snapshot.head_for("trace").head == head


def test_cross_run_identity_same_task_two_runs(mg: VcsCore) -> None:
    head_a = _run_and_append(mg, task_id=DEMO_TASK_ID, args={"n": 1}, run_ref="run-a")
    head_b = _run_and_append(mg, task_id=DEMO_TASK_ID, args={"n": 1}, run_ref="run-b")
    stored_a, stored_b = _stored_payload(mg, head_a), _stored_payload(mg, head_b)
    invocation_a, invocation_b = stored_a["events"][0], stored_b["events"][0]
    # The dual-domain split, measured on durable bytes: same fourth-row fact…
    assert invocation_a["record_digest"] == invocation_b["record_digest"]
    # …while the runs' vcscore-domain content differs (different run, different world pointers).
    assert stored_a["run_ref"] != stored_b["run_ref"]
    assert stored_a["events"][1]["head_from"] != stored_b["events"][1]["head_from"]


def test_failure_path_trace_survives_with_no_output_pointer(mg: VcsCore) -> None:
    head = _run_and_append(mg, task_id=FAILING_TASK_ID, args={}, run_ref="run-f")
    stored = _stored_payload(mg, head)
    lifecycle = stored["events"][-1]
    assert lifecycle["transition"] == "failed"
    assert lifecycle["terminal_status"] == "discarded"
    assert stored["events"][1]["head_to"] is None
    # Durably selected despite the discarded run (invariant 3, dialect flavor).
    assert mg._world_storage().read_world(mg.world_oid()).snapshot.head_for("trace").head == head
