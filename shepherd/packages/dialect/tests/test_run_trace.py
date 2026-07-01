"""B4b slice 3: `RunTrace` + the read route + the verbs (triage D1's re-pin locus).

Each verb's test names the legacy question it preserves (D1: preserve the
question, retire the mechanism): `filter` ← the stream *views* question
(legacy `test_views`), `summary` ← `test_debug_summary`, `compare` ←
`test_comparison`/`compare_streams`, now keyed on real cross-run identity.
The Match auto-lift is the selector parameter: kind strings, effect types,
and predicates today; `Match`/ `Pattern.event` compile into the same
slot tomorrow.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from vcs_core.runtime_api import VcsCore, Store, build_builtin_substrate_context
from vcs_core.runtime_substrate import FileCreate, TaskTraceSubstrateDriver
from vcs_core.substrates import DeclarativeFilesystemSubstrate, MarkerSubstrate

from shepherd_dialect import (
    CHILD_LAUNCH_REFUSED,
    CHILD_RUN_COMPLETED,
    CHILD_VALUE_COMPLETED,
    ShepherdRunDriver,
    RunTrace,
    append_run_trace,
    build_run_trace_revision,
    read_run_trace,
)

if TYPE_CHECKING:
    from pathlib import Path


def revision(run_ref: str, *, args: dict | None = None, status: str = "merged", extra=()) -> dict:
    return build_run_trace_revision(
        run_ref=run_ref,
        trace_owner_id=f"task:t:{run_ref}",
        frontier_id=f"frontier:{run_ref}",
        task_id="pkg.mod:t",
        args=args if args is not None else {"x": 1},
        may_profile="Permissive",
        terminal_status=status,
        input_world_oid="a" * 40,
        output_world_oid=None if status == "discarded" else "b" * 40,
        extra_events=extra,
    )


DENIAL = {"kind": "supervisor.decision", "decision": "denied", "op": "FileCreate", "path": "src/x.py"}
EFFECT = {"kind": "FileCreate", "path": "README.md"}


def child_completed_event(
    child_run_ref: str,
    *,
    lifecycle: str = "finished",
    terminal_status: str = "merged",
    disposition: str = "release",
    scope_status: str = "discarded",
) -> dict:
    return {
        "id": f"child-run-completed:{child_run_ref}",
        "kind": CHILD_RUN_COMPLETED,
        "parent_run_ref": "run-parent",
        "child_run_ref": child_run_ref,
        "child_task_id": "pkg.mod:child",
        "child_operation_id": f"op-{child_run_ref}",
        "child_logical_scope_ref": f"refs/vcscore/scopes/task-{child_run_ref}",
        "child_execution_scope_ref": f"refs/vcscore/scopes/exec-{child_run_ref}",
        "child_trace_head": f"trace-head-{child_run_ref}",
        "child_lifecycle": lifecycle,
        "child_world_disposition": disposition,
        "child_scope_terminal_status": scope_status,
        "terminal_status": terminal_status,
        "may_profile": "ReadOnly",
        "caused_by": f"task-call:{child_run_ref}",
    }


# --- filter: the stream-views question, re-pinned ---


def test_filter_by_kind_string():
    trace = RunTrace(revision("r1", extra=[DENIAL]))
    assert [e["kind"] for e in trace.filter("supervisor.decision")] == ["supervisor.decision"]


def test_filter_by_effect_type_lifts_to_kind():
    trace = RunTrace(revision("r1", extra=[EFFECT]))
    (event,) = trace.filter(FileCreate)
    assert event["path"] == "README.md"


def test_filter_by_predicate_is_the_match_compilation_slot():
    trace = RunTrace(revision("r1", extra=[DENIAL, EFFECT]))
    denied = trace.filter(lambda e: e.get("decision") == "denied")
    assert [e["path"] for e in denied] == ["src/x.py"]


def test_filter_rejects_non_selector():
    with pytest.raises(TypeError, match="selector"):
        RunTrace(revision("r1")).filter(42)


# --- summary: the debug-summary question, re-pinned ---


def test_summary_counts_terminal_pointers_and_decisions():
    trace = RunTrace(revision("r1", extra=[DENIAL]))
    summary = trace.summary()
    assert summary["terminal_status"] == "merged"
    assert summary["kinds"]["task.invocation"] == 1
    assert summary["head_to"] == "b" * 40
    assert summary["supervision"][0]["decision"] == "denied"
    assert summary["child_runs"] == ()
    assert summary["invocation_digest"].startswith("sha256:")


def test_nested_child_completed_event_schema_is_filterable_and_summarized():
    child_head = "trace-head-run-child"
    child_trace = RunTrace(
        revision(
            "run-child",
            status="merged",
            extra=[
                {
                    "kind": "child.work",
                    "detail": "synthetic child terminal trace",
                }
            ],
        )
    )
    event = child_completed_event("run-child")
    event["child_trace_head"] = child_head
    parent_trace = RunTrace(revision("run-parent", extra=[event]))

    (observed,) = parent_trace.filter(CHILD_RUN_COMPLETED)
    assert observed == event
    summary = parent_trace.summary()
    assert summary["kinds"][CHILD_RUN_COMPLETED] == 1
    assert summary["child_runs"] == (
        {
            "child_run_ref": "run-child",
            "child_lifecycle": "finished",
            "child_world_disposition": "release",
            "child_scope_terminal_status": "discarded",
            "child_trace_head": child_head,
            "child_operation_id": "op-run-child",
            "child_logical_scope_ref": "refs/vcscore/scopes/task-run-child",
            "child_execution_scope_ref": "refs/vcscore/scopes/exec-run-child",
            "terminal_status": "merged",
        },
    )
    assert child_trace.summary()["terminal_status"] == "merged"


def test_nested_child_summary_preserves_failed_child_fields_verbatim():
    event = child_completed_event("run-child-failed", lifecycle="failed", terminal_status="discarded")
    summary = RunTrace(revision("run-parent", extra=[event])).summary()

    assert summary["child_runs"][0]["child_run_ref"] == "run-child-failed"
    assert summary["child_runs"][0]["child_lifecycle"] == "failed"
    assert summary["child_runs"][0]["child_world_disposition"] == "release"
    assert summary["child_runs"][0]["child_scope_terminal_status"] == "discarded"
    assert summary["child_runs"][0]["terminal_status"] == "discarded"


def test_child_launch_refused_is_not_summarized_as_completed_child_run():
    refused = {
        "id": "child-launch-refused:call-1",
        "kind": CHILD_LAUNCH_REFUSED,
        "parent_run_ref": "run-parent",
        "child_task_id": "pkg.mod:child",
        "reason": "precondition refused before child runtime launch",
        "caused_by": "task-call:call-1",
    }
    trace = RunTrace(revision("run-parent", extra=[refused]))

    assert trace.filter(CHILD_LAUNCH_REFUSED) == (refused,)
    assert trace.summary()["kinds"][CHILD_LAUNCH_REFUSED] == 1
    assert trace.summary()["child_runs"] == ()


def test_value_child_event_schema_is_separate_from_durable_child_runs():
    event = {
        "id": "child-value-completed:run-child",
        "kind": CHILD_VALUE_COMPLETED,
        "parent_run_ref": "run-parent",
        "child_run_ref": "run-child",
        "child_task_id": "pkg.mod:child",
        "child_trace_token": "memory-trace:run-child",
        "child_lifecycle": "finished",
        "terminal_status": "merged",
        "may_profile": "ReadOnly",
        "caused_by": "task-call:run-child",
        "evidence_level": "same_process_value",
        "trace_materialized": False,
        "ledger_visible": False,
        "operation_identity_kind": "logical_placeholder",
    }
    trace = RunTrace(revision("run-parent", extra=[event]))

    assert trace.filter(CHILD_VALUE_COMPLETED) == (event,)
    assert trace.summary()["child_runs"] == ()
    assert trace.summary()["value_children"] == (
        {
            "child_run_ref": "run-child",
            "child_lifecycle": "finished",
            "child_trace_token": "memory-trace:run-child",
            "evidence_level": "same_process_value",
            "trace_materialized": False,
            "ledger_visible": False,
            "operation_identity_kind": "logical_placeholder",
            "terminal_status": "merged",
        },
    )


# --- compare: the compare_streams question, keyed on the fourth row ---


def test_compare_same_invocation_across_terminal_outcomes():
    ok = RunTrace(revision("r1"))
    failed = RunTrace(revision("r2", status="discarded", extra=[DENIAL]))
    diff = ok.compare(failed)
    assert diff["same_invocation"] is True # same task+args+may ⇒ same cross-run fact
    assert diff["terminal_status"] == ("merged", "discarded")
    assert diff["kind_count_delta"] == {"supervisor.decision": 1}


def test_compare_different_args_is_a_different_fact():
    diff = RunTrace(revision("r1")).compare(RunTrace(revision("r2", args={"x": 2})))
    assert diff["same_invocation"] is False


# --- the durable read route (W1) ---


@pytest.fixture
def mg(tmp_path: Path):
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


@pytest.mark.skipif(sys.platform != "darwin", reason="clonefile carrier pairing (sibling-suite convention)")
def test_nested_child_completed_event_points_to_durable_child_trace(mg) -> None:
    child_head = append_run_trace(
        mg,
        revision(
            "run-child",
            status="merged",
            extra=[{"kind": "child.work", "detail": "stored synthetic child terminal trace"}],
        ),
    )
    event = child_completed_event("run-child")
    event["child_trace_head"] = child_head
    parent_head = append_run_trace(mg, revision("run-parent", extra=[event]))

    parent_trace = read_run_trace(mg, parent_head)
    child_trace = read_run_trace(mg, child_head)

    assert parent_trace is not None
    assert child_trace is not None
    assert parent_trace.summary()["child_runs"][0]["child_trace_head"] == child_head
    assert parent_trace.filter(CHILD_RUN_COMPLETED)[0]["child_trace_head"] == child_head
    assert child_trace.run_ref == "run-child"
    assert child_trace.summary()["terminal_status"] == "merged"


@pytest.mark.skipif(sys.platform != "darwin", reason="clonefile carrier pairing (sibling-suite convention)")
def test_read_run_trace_rides_the_public_route(mg, tmp_path: Path, monkeypatch) -> None:
    assert read_run_trace(mg) is None # no world yet — the None path is honest
    head = append_run_trace(mg, revision("run-1", extra=[DENIAL]))
    by_head = read_run_trace(mg, head)
    assert by_head.run_ref == "run-1"
    by_selected = read_run_trace(mg) # head=None: the binding's selected head
    assert by_selected.payload == by_head.payload
    assert by_head.summary()["supervision"][0]["path"] == "src/x.py"

    # The lower-level helper reads the same revision at the process boundary.
    from click.testing import CliRunner

    from shepherd_dialect.cli import main

    monkeypatch.chdir(tmp_path / "ws")
    result = CliRunner().invoke(main, ["run", "trace-revision", head, "--json"])
    assert result.exit_code == 0, result.output
    assert '"run_ref": "run-1"' in result.output


def test_cli_refuses_outside_a_repo(tmp_path: Path, monkeypatch) -> None:
    from click.testing import CliRunner

    from shepherd_dialect.cli import main

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["run", "trace", "deadbeef"])
    assert result.exit_code == 1
    assert "not a Shepherd workspace" in result.output


# --- the driver-plugin config (W3) ---


def test_run_driver_is_plugin_discoverable() -> None:
    from vcs_core.discovery import discover_plugin_registrations

    registrations = discover_plugin_registrations(strict=False)
    assert "shepherd.run_driver" in registrations
    registration = registrations["shepherd.run_driver"]
    assert registration.implementation_kind == "driver"
    assert registration.module_name == "shepherd_dialect.run_driver"
    assert registration.class_name == "ShepherdRunDriver"


def test_run_driver_plugin_registration_is_import_light() -> None:
    probe = (
        "import sys\n"
        "from vcs_core.discovery import discover_plugin_registrations\n"
        "registrations = discover_plugin_registrations(strict=False)\n"
        "registration = registrations['shepherd.run_driver']\n"
        "assert registration.module_name == 'shepherd_dialect.run_driver'\n"
        "assert registration.class_name == 'ShepherdRunDriver'\n"
        "loaded = sorted(m for m in sys.modules if m == 'shepherd_dialect.run_driver')\n"
        "assert not loaded, loaded\n"
        "print('registration boundary OK')\n"
    )
    proc = subprocess.run([sys.executable, "-P", "-c", probe], capture_output=True, text=True, check=True)
    assert "registration boundary OK" in proc.stdout
