"""The literal §8.1 ``update_readme`` acceptance fixture (v1-integration.md).

The named deterministic fixture, run through the production dialect's `run`
on the real clonefile carrier: the **success** variant (file modified;
effects captured into merged history; durable trace `merged`), the
**failure** variant (mid-body raise; wrap discards; ground pristine; durable
trace `discarded` with output pointer None), and the **supervised** variants
(`drafts_only_supervisor`, the §7.3 check-at-commit cell: approve under
``drafts/``, deny elsewhere with ``SupervisorDenied`` — both decisions
recorded into the durable trace as ``supervisor.decision`` events).

These convert the §8.2 boxes annotated *green-in-substance* (evidence was
the equivalently-shaped skeleton/dialect fixtures) to literally green on the
named fixture. The §8.2 *inspection* boxes (`run.trace` sugar; the trace
``read`` round-trip) stay pending — slice-3 plumbing with its own seam.

macOS pairing (clonefile carrier, in-process provider — the fixture is
deterministic and jail-independent; the jailed pairing has its own B3c
suites). The Linux pairing rides the container gate via the fuse carrier.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from vcs_core.runtime_api import Store, VcsCore, build_builtin_substrate_context
from vcs_core.runtime_substrate import TaskTraceSubstrateDriver
from vcs_core.substrates import DeclarativeFilesystemSubstrate, MarkerSubstrate

from shepherd_dialect import (
    ShepherdRunDriver,
    SupervisorDenied,
    append_run_trace,
    build_run_trace_revision,
    drafts_only_supervisor,
    supervisor_frame,
)
from shepherd_dialect.trace_events import (
    committed_operation_events_from_log,
    log_entry_operation_id,
    proposed_operation_events,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="clonefile carrier pairing; the Linux pairing rides the container gate"
)


def update_readme(stack: Any, *, working_path: str, target: str, marker: str) -> dict:
    """Append a deterministic marker line to a target file under the workspace."""
    del stack
    path = Path(working_path) / target
    existing = path.read_text() if path.exists() else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(existing + f"\n{marker}\n")
    return {"target": target, "marker": marker}


def update_readme_then_fail(stack: Any, *, working_path: str, target: str, marker: str) -> dict:
    """The failure variant: a partial write, then a mid-body raise."""
    update_readme(stack, working_path=working_path, target=target, marker=marker)
    raise RuntimeError("mid-body failure after a partial write")


FIXTURE_ID = f"{__name__}:update_readme"
FAILING_FIXTURE_ID = f"{__name__}:update_readme_then_fail"


@pytest.fixture
def mg(tmp_path: Path) -> VcsCore:
    root = tmp_path / "ws"
    root.mkdir()
    store = Store(str(root / ".vcscore"))
    ctx = build_builtin_substrate_context(store, workspace=root, config={"backend": "clonefile"})
    vcscore = VcsCore(
        str(root),
        substrates=[
            MarkerSubstrate(ctx),
            DeclarativeFilesystemSubstrate(ctx),
            ShepherdRunDriver(),
            TaskTraceSubstrateDriver(),
        ],
        store=store,
    )
    vcscore.activate()
    yield vcscore
    vcscore.deactivate()


def _run_and_append_trace(
    mg: VcsCore,
    *,
    task_id: str,
    args: dict[str, Any],
    run_ref: str,
    supervisors: tuple = (),
) -> tuple[str, BaseException | None]:
    """Run the fixture and append its durable trace on BOTH terminal paths,
    recording supervisor decisions (approvals from the payload; a denial from
    the raised ``SupervisorDenied``) as ``supervisor.decision`` events."""
    input_world = mg.world_oid()
    prior_log_oids = {entry.oid for entry in _merged_effects(mg)}
    error: BaseException | None = None
    run_operation_id: str | None = None
    supervision_events: list[dict[str, Any]] = []
    proposed_records: list[dict[str, Any]] = []
    failure_events: list[dict[str, Any]] = []
    extra = {"supervisor_handlers": [supervisor_frame(s) for s in supervisors]} if supervisors else {}
    try:
        outcome = mg.execute_recorded(
            "runtime", "run", scope=mg.ground, task_id=task_id, args=args, may="Permissive", **extra
        )
    except Exception as exc:  # every failure path must still trace
        error = exc
        denial = exc if isinstance(exc, SupervisorDenied) else None
        if denial is not None:
            proposed_records.append(
                {
                    "binding": "workspace",
                    "op": type(denial.effect).__name__,
                    "path": getattr(denial.effect, "path", None),
                    "decision": "denied",
                }
            )
            supervision_events.append(
                {
                    "kind": "supervisor.decision",
                    "decision": "denied",
                    "op": type(denial.effect).__name__,
                    "path": getattr(denial.effect, "path", None),
                    "reason": denial.reason,
                }
            )
        else:
            failure_events.extend(
                [
                    {
                        "kind": "task.body.entered",
                        "phase": "body",
                        "evidence_level": "inferred_from_body_exception",
                    },
                    {
                        "kind": "task.body.raised",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                    {
                        "kind": "task.body.partial_work",
                        "materialized": False,
                        "reason": "discarded workspace effects are not durably attributed in this trace cut",
                    },
                ]
            )
    else:
        core = outcome.value.transitions[0].payload["portable_core"]
        run_operation_id = core.get("operation_id") if isinstance(core.get("operation_id"), str) else None
        proposed_records.extend(core.get("supervision", ()))
        supervision_events.extend(
            {"kind": "supervisor.decision", "decision": entry["decision"], "op": entry["op"], "path": entry["path"]}
            for entry in core.get("supervision", ())
        )
        for record in proposed_records:
            if isinstance(record, dict) and run_operation_id is not None:
                record.setdefault("operation_id", run_operation_id)
    output_world = mg.world_oid()
    owned_entries = _new_merged_effects(mg, prior_log_oids)
    if run_operation_id is None:
        run_operation_id = _single_operation_id(owned_entries)
    taxonomy_events = [
        *proposed_operation_events(proposed_records),
        *(committed_operation_events_from_log(owned_entries) if error is None else ()),
    ]
    revision = build_run_trace_revision(
        run_ref=run_ref,
        trace_owner_id=f"task:{task_id}:{run_ref}",
        frontier_id=f"frontier:{run_ref}",
        task_id=task_id,
        args=args,
        may_profile="Permissive",
        terminal_status="discarded" if error is not None else "merged",
        input_world_oid=input_world,
        output_world_oid=None if error is not None else output_world,
        operation_id=run_operation_id,
        extra_events=[*supervision_events, *failure_events, *taxonomy_events],
    )
    return append_run_trace(mg, revision), error


def _stored_payload(mg: VcsCore, head: str) -> dict[str, Any]:
    return mg._world_storage().store("store_trace").read_revision_payload(head)


def _merged_effects(mg: VcsCore) -> list[Any]:
    return list(mg.log(max_count=40))


def _new_merged_effects(mg: VcsCore, prior_oids: set[str]) -> list[Any]:
    return [entry for entry in _merged_effects(mg) if entry.oid not in prior_oids]


def _single_operation_id(entries: list[Any]) -> str | None:
    operation_ids = {operation_id for entry in entries if (operation_id := log_entry_operation_id(entry)) is not None}
    if len(operation_ids) == 1:
        return next(iter(operation_ids))
    return None


def _events_of_kind(stored: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [event for event in stored["events"] if event.get("kind") == kind]


def _file_effect_events(stored: dict[str, Any]) -> list[dict[str, Any]]:
    return [event for event in stored["events"] if event.get("kind") in {"FileCreate", "FilePatch"}]


def test_success_variant_modifies_file_and_traces_merged(mg: VcsCore) -> None:
    """§8.2 success path: file modified, effects in merged history, trace `merged`."""
    head, error = _run_and_append_trace(
        mg, task_id=FIXTURE_ID, args={"target": "README.md", "marker": "hello"}, run_ref="run-ok"
    )
    assert error is None
    assert any(
        e.metadata.get("type") in {"FileCreate", "FilePatch"} and e.metadata.get("path") == "README.md"
        for e in _merged_effects(mg)
    ), "the fixture's write must be captured into merged history"
    stored = _stored_payload(mg, head)
    lifecycle = stored["events"][-1]
    assert lifecycle["terminal_status"] == "merged"
    assert stored["events"][1]["head_to"] is not None
    kinds = [e["kind"] for e in stored["events"]]
    assert "SubstrateOperationCommitted" in kinds
    assert any(kind in {"FileCreate", "FilePatch"} for kind in kinds)
    (committed,) = _events_of_kind(stored, "SubstrateOperationCommitted")
    assert committed["binding"] == "workspace"
    assert committed["effect"]["kind"] in {"FileCreate", "FilePatch"}
    assert committed["effect"]["path"] == "README.md"
    assert committed["operation_ref"]
    assert committed["operation_id"]
    (file_event,) = _file_effect_events(stored)
    assert file_event["binding"] == "workspace"
    assert file_event["path"] == "README.md"
    assert file_event["phase"] == "committed"
    assert file_event["operation_id"] == committed["operation_id"]


def test_success_trace_excludes_prior_committed_effects(mg: VcsCore) -> None:
    """W1.D ownership: committed effect events belong to this run, not recent history."""
    prior_head, prior_error = _run_and_append_trace(
        mg, task_id=FIXTURE_ID, args={"target": "PRIOR.md", "marker": "old"}, run_ref="run-prior"
    )
    assert prior_error is None
    assert prior_head

    head, error = _run_and_append_trace(
        mg, task_id=FIXTURE_ID, args={"target": "README.md", "marker": "hello"}, run_ref="run-owned"
    )
    assert error is None
    stored = _stored_payload(mg, head)
    paths = [event.get("path") for event in stored["events"] if event.get("kind") in {"FileCreate", "FilePatch"}]
    assert paths == ["README.md"]


def test_failure_variant_discards_and_trace_outlives(mg: VcsCore, tmp_path: Path) -> None:
    """§8.2 failure path: workspace untouched in ground; trace `discarded`, output None."""
    head, error = _run_and_append_trace(
        mg, task_id=FAILING_FIXTURE_ID, args={"target": "README.md", "marker": "boom"}, run_ref="run-fail"
    )
    assert isinstance(error, RuntimeError)
    assert not (tmp_path / "ws" / "README.md").exists(), "ground must stay pristine"
    assert not any(
        e.metadata.get("type") in {"FileCreate", "FilePatch"} and e.metadata.get("path") == "README.md"
        for e in _merged_effects(mg)
    ), "the discarded run's write must not reach merged history"
    stored = _stored_payload(mg, head)
    lifecycle = stored["events"][-1]
    assert lifecycle["terminal_status"] == "discarded"
    assert stored["events"][1]["head_to"] is None
    kinds = [e["kind"] for e in stored["events"]]
    assert "task.body.entered" in kinds
    assert "task.body.raised" in kinds
    assert "task.body.partial_work" in kinds
    assert "SubstrateOperationCommitted" not in kinds
    raised = next(e for e in stored["events"] if e["kind"] == "task.body.raised")
    assert raised["error_type"] == "RuntimeError"
    partial = next(e for e in stored["events"] if e["kind"] == "task.body.partial_work")
    assert partial["materialized"] is False


def test_supervised_approve_under_drafts(mg: VcsCore) -> None:
    """§8.2 supervised-approve: a drafts/ write approves; the trace records it."""
    head, error = _run_and_append_trace(
        mg,
        task_id=FIXTURE_ID,
        args={"target": "drafts/notes.md", "marker": "ok"},
        run_ref="run-approve",
        supervisors=(drafts_only_supervisor,),
    )
    assert error is None
    assert any(
        e.metadata.get("path") == "drafts/notes.md" for e in _merged_effects(mg)
    ), "the approved write must merge"
    decisions = [e for e in _stored_payload(mg, head)["events"] if e["kind"] == "supervisor.decision"]
    assert decisions
    assert decisions[0]["decision"] == "approved"
    assert decisions[0]["path"] == "drafts/notes.md"
    stored = _stored_payload(mg, head)
    kinds = [e["kind"] for e in stored["events"]]
    assert "SubstrateOperationProposed" in kinds
    assert "SubstrateOperationCommitted" in kinds
    assert any(kind in {"FileCreate", "FilePatch"} for kind in kinds)
    (proposed,) = _events_of_kind(stored, "SubstrateOperationProposed")
    assert proposed["binding"] == "workspace"
    assert proposed["effect"] == {"kind": "FileCreate", "path": "drafts/notes.md"}
    assert proposed["decision"] == "approved"
    assert proposed["operation_id"]
    proposed_effect = next(event for event in _file_effect_events(stored) if event["phase"] == "proposed")
    assert proposed_effect["binding"] == "workspace"
    assert proposed_effect["path"] == "drafts/notes.md"
    assert proposed_effect["decision"] == "approved"
    assert proposed_effect["operation_id"] == proposed["operation_id"]
    committed = _events_of_kind(stored, "SubstrateOperationCommitted")[0]
    assert committed["effect"]["path"] == "drafts/notes.md"


def test_supervised_deny_outside_drafts(mg: VcsCore, tmp_path: Path) -> None:
    """§8.2 supervised-deny: a non-drafts write denies with SupervisorDenied;
    the wrap discards (ground pristine); the trace records the denial."""
    head, error = _run_and_append_trace(
        mg,
        task_id=FIXTURE_ID,
        args={"target": "src/main.py", "marker": "nope"},
        run_ref="run-deny",
        supervisors=(drafts_only_supervisor,),
    )
    assert isinstance(error, SupervisorDenied)
    assert "src/main.py" in error.reason
    assert not (tmp_path / "ws" / "src" / "main.py").exists()
    assert not any(
        e.metadata.get("path") == "src/main.py" for e in _merged_effects(mg)
    ), "the denied write must never reach merged history"
    stored = _stored_payload(mg, head)
    decisions = [e for e in stored["events"] if e["kind"] == "supervisor.decision"]
    assert decisions
    assert decisions[0]["decision"] == "denied"
    assert decisions[0]["path"] == "src/main.py"
    kinds = [e["kind"] for e in stored["events"]]
    assert "SubstrateOperationProposed" in kinds
    assert "SubstrateOperationCommitted" not in kinds
    (proposed,) = _events_of_kind(stored, "SubstrateOperationProposed")
    assert proposed["binding"] == "workspace"
    assert proposed["effect"] == {"kind": "FileCreate", "path": "src/main.py"}
    assert proposed["decision"] == "denied"
    assert "operation_id" not in proposed
    proposed_effect = next(event for event in _file_effect_events(stored) if event["phase"] == "proposed")
    assert proposed_effect["binding"] == "workspace"
    assert proposed_effect["path"] == "src/main.py"
    assert proposed_effect["decision"] == "denied"
    assert stored["events"][-1]["terminal_status"] == "discarded"
