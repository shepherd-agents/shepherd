"""CLI sugar for the Shepherd dialect workspace-control surface."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from typing import TYPE_CHECKING, Any, cast

import click

from shepherd_dialect.trace import RunTrace
from shepherd_dialect.workspace_control import ShepherdWorkspace

if TYPE_CHECKING:
    from shepherd_dialect.workspace_control.workspace import WorkspaceRunPlacement


@click.group()
def main() -> None:
    """Shepherd dialect sugar over the vcs-core canonical surface."""


@main.group()
def run() -> None:
    """Inspect runs and settle retained outputs; start is a fenced compatibility entry point."""


@main.group()
def task() -> None:
    """Manage and inspect task-library entries."""


@run.command("trace-revision")
@click.argument("rev")
@click.option("--events", "show_events", is_flag=True, help="Print the full event list too.")
@click.option("--json", "json_output", is_flag=True, help="Emit the raw JSON payload.")
def run_trace_revision(rev: str, show_events: bool, json_output: bool) -> None:
    """Print the run-trace summary for one durable trace revision."""
    workspace = _open_workspace(activate=False)
    mg = workspace.mg
    try:
        try:
            payload = mg.read_trace_revision(rev)
        except (KeyError, ValueError) as exc:
            raise click.ClickException(f"cannot read trace revision {rev!r}: {exc}") from exc
    finally:
        _close_workspace(workspace)
    if payload is None:
        raise click.ClickException(f"no trace revision at {rev!r}.")
    trace = RunTrace(payload)
    _emit_trace(trace, show_events=show_events, json_output=json_output)
    if show_events:
        return


@run.command("list")
@click.option("--status", help="Only include runs with this status.")
@click.option("--task-id", help="Only include runs for this task id.")
@click.option("--max-count", type=int, help="Return at most this many latest runs.")
@click.option("--json", "json_output", is_flag=True, help="Emit the raw JSON payload.")
def run_list(status: str | None, task_id: str | None, max_count: int | None, json_output: bool) -> None:
    """List run summaries from the selected run ledger."""
    workspace = _open_workspace(activate=False)
    try:
        rows = _query(lambda: workspace.runs.list(status=status, task_id=task_id, max_count=max_count))
        _emit(rows, json_output=json_output, human=_emit_run_list)
    finally:
        _close_workspace(workspace)


@run.command("show")
@click.argument("run_ref", required=False)
@click.option("--latest", is_flag=True, help="Show the latest run.")
@click.option("--json", "json_output", is_flag=True, help="Emit the raw JSON payload.")
def run_show(run_ref: str | None, latest: bool, json_output: bool) -> None:
    """Show one run record from the selected run ledger."""
    selector = _run_selector(run_ref, latest=latest)
    workspace = _open_workspace(activate=False)
    try:
        record = _query(lambda: workspace.runs.show(selector))
        if record is None:
            raise click.ClickException(f"no run matches {selector!r}")
        _emit(record, json_output=json_output, human=_emit_run_show)
    finally:
        _close_workspace(workspace)


@run.command("vcscore")
@click.argument("run_ref", required=False)
@click.option("--latest", is_flag=True, help="Show citations for the latest run.")
@click.option("--json", "json_output", is_flag=True, help="Emit the raw JSON payload.")
def run_vcscore(run_ref: str | None, latest: bool, json_output: bool) -> None:
    """Show the vcs-core citations carried by one run record."""
    selector = _run_selector(run_ref, latest=latest)
    workspace = _open_workspace(activate=False)
    try:
        projection = _query(lambda: workspace.runs.vcscore(selector))
        if projection is None:
            raise click.ClickException(f"no run matches {selector!r}")
        _emit(projection, json_output=json_output, human=_emit_mapping_summary)
    finally:
        _close_workspace(workspace)


@run.command("start")
@click.argument("task_ref")
@click.option("--args", "args_json", default="{}", show_default=True, help="JSON object of task keyword args.")
@click.option("--may", help="Override the run may profile recorded in the run ledger.")
@click.option(
    "--placement",
    type=click.Choice(["auto", "advisory", "jail"]),
    default="auto",
    show_default=True,
    help="Execution placement for the retained workspace run.",
)
@click.option("--reason", help="Resolution reason recorded in the run link map.")
def run_start(task_ref: str, args_json: str, may: str | None, placement: str, reason: str | None) -> None:
    """Run the fenced compatibility start path."""
    workspace = _open_workspace(activate=True)
    args = _json_object(args_json, label="--args")
    try:
        _emit_json(
            _query(
                lambda: workspace.runs.start(
                    task_ref,
                    args=args,
                    may=may,
                    placement=cast("WorkspaceRunPlacement", placement),
                    launch_surface="cli",
                    reason=reason,
                )
            )
        )
    finally:
        _close_workspace(workspace)


@run.command("trace")
@click.argument("run_ref", required=False)
@click.option("--latest", is_flag=True, help="Show the latest run trace.")
@click.option("--events", "show_events", is_flag=True, help="Print the full event list too.")
@click.option("--json", "json_output", is_flag=True, help="Emit the raw JSON payload.")
def run_trace_ref(run_ref: str | None, latest: bool, show_events: bool, json_output: bool) -> None:
    """Print the materialized trace associated with one run ref."""
    selector = _run_selector(run_ref, latest=latest)
    workspace = _open_workspace(activate=False)
    try:
        trace = _query(lambda: workspace.runs.trace(selector, events=show_events))
        if trace is None:
            raise click.ClickException(f"run {selector!r} has no materialized trace")
        if isinstance(trace, RunTrace):
            _emit_trace(trace, show_events=show_events, json_output=json_output)
            return
        _emit(trace, json_output=json_output, human=_emit_mapping_summary)
    finally:
        _close_workspace(workspace)


@run.command("output-citations")
@click.argument("run_ref", required=False)
@click.option("--binding", help="Only include citations for this binding.")
@click.option("--latest", is_flag=True, help="Only include citations for the latest run.")
@click.option("--json", "json_output", is_flag=True, help="Emit the raw JSON payload.")
def run_output_citations(run_ref: str | None, binding: str | None, latest: bool, json_output: bool) -> None:
    """List raw run-ledger output citations without retained-custody state."""
    selector = _optional_run_selector(run_ref, latest=latest)
    workspace = _open_workspace(activate=False)
    try:
        citations = _query(lambda: workspace.runs.output_citations(run_ref=selector, binding=binding))
        _emit(citations, json_output=json_output, human=_emit_mapping_summary)
    finally:
        _close_workspace(workspace)


@run.command("outputs")
@click.argument("run_ref", required=False)
@click.option("--trace-store", "trace_store_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--binding", help="Only include outputs for this binding.")
@click.option("--state", help="Only include outputs with this retained-output state.")
@click.option("--latest", is_flag=True, help="Only include outputs for the latest run.")
@click.option("--json", "json_output", is_flag=True, help="Emit the raw JSON payload.")
def run_outputs(
    run_ref: str | None,
    trace_store_path: str | None,
    binding: str | None,
    state: str | None,
    latest: bool,
    json_output: bool,
) -> None:
    """List product run outputs after trace-descriptor and custody validation."""
    selector = _optional_run_selector(run_ref, latest=latest)
    workspace = _open_workspace(activate=False)
    trace_store = None if trace_store_path is None else _open_trace_store(trace_store_path)
    try:
        outputs = _query(
            lambda: workspace.runs.outputs(run_ref=selector, binding=binding, state=state, trace_store=trace_store)
        )
    finally:
        if trace_store is not None:
            close = getattr(trace_store, "close", None)
            if callable(close):
                close()
        _close_workspace(workspace)
    _emit(outputs, json_output=json_output, human=_emit_outputs)


@run.command("changeset")
@click.argument("run_ref", required=False)
@click.option("--trace-store", "trace_store_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--output-name", default="workspace", show_default=True, help="Run output name to inspect.")
@click.option("--binding", help="Only include outputs for this binding.")
@click.option("--state", help="Only include outputs with this retained-output state.")
@click.option("--latest", is_flag=True, help="Inspect the latest run.")
@click.option("--read", "read_path", metavar="PATH", help="Print one changed file's content instead of the summary.")
@click.option("--json", "json_output", is_flag=True, help="Emit the raw JSON payload.")
def run_changeset(
    run_ref: str | None,
    trace_store_path: str | None,
    output_name: str,
    binding: str | None,
    state: str | None,
    latest: bool,
    read_path: str | None,
    json_output: bool,
) -> None:
    """Inspect the read-only changeset view for one retained run output."""
    if read_path is not None and json_output:
        raise click.UsageError("--read and --json are mutually exclusive")
    selector = _run_selector(run_ref, latest=latest)
    workspace = _open_workspace(activate=False)
    trace_store = None if trace_store_path is None else _open_trace_store(trace_store_path)
    try:
        changeset = _query(
            lambda: workspace.runs.changeset(
                selector,
                output_name=output_name,
                binding=binding,
                state=state,
                trace_store=trace_store,
            )
        )
        if read_path is not None:
            value = changeset.read_file(read_path)
            if value is None:
                raise click.ClickException(f"changeset has no file at {read_path!r}")
            content, _mode = value
            click.echo(content.decode("utf-8", errors="replace"), nl=False)
            return
        _emit(changeset.inspect(), json_output=json_output, human=_emit_changeset)
    finally:
        if trace_store is not None:
            close = getattr(trace_store, "close", None)
            if callable(close):
                close()
        _close_workspace(workspace)


@run.command("select")
@click.argument("run_ref")
@click.option("--trace-store", "trace_store_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--output-name", default="workspace", show_default=True, help="Run output name to settle.")
@click.option("--binding", help="Only include outputs for this binding.")
def run_select(run_ref: str, trace_store_path: str | None, output_name: str, binding: str | None) -> None:
    """Select one retained run output into its live parent world."""
    _settle_run_output(
        "select",
        run_ref=run_ref,
        trace_store_path=trace_store_path,
        output_name=output_name,
        binding=binding,
    )


@run.command("release")
@click.argument("run_ref")
@click.option("--trace-store", "trace_store_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--output-name", default="workspace", show_default=True, help="Run output name to settle.")
@click.option("--binding", help="Only include outputs for this binding.")
def run_release(run_ref: str, trace_store_path: str | None, output_name: str, binding: str | None) -> None:
    """Release one retained run output without selecting it."""
    _settle_run_output(
        "release",
        run_ref=run_ref,
        trace_store_path=trace_store_path,
        output_name=output_name,
        binding=binding,
    )


@run.command("discard")
@click.argument("run_ref")
@click.option("--trace-store", "trace_store_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--output-name", default="workspace", show_default=True, help="Run output name to settle.")
@click.option("--binding", help="Only include outputs for this binding.")
def run_discard(run_ref: str, trace_store_path: str | None, output_name: str, binding: str | None) -> None:
    """Discard one retained run output as explicit non-application."""
    _settle_run_output(
        "discard",
        run_ref=run_ref,
        trace_store_path=trace_store_path,
        output_name=output_name,
        binding=binding,
    )


@run.command("publish-retained-workspace-output")
@click.argument("run_ref")
def run_publish_retained_workspace_output(run_ref: str) -> None:
    """Publish or repair the retained workspace-output citation for one run."""
    workspace = _open_workspace(activate=True)
    try:
        _emit_json(_query(lambda: workspace.runs.publish_retained_workspace_output(run_ref)))
    finally:
        _close_workspace(workspace)


@run.command("repair")
@click.option("--json", "json_output", is_flag=True, help="Emit the result as JSON.")
def run_repair(json_output: bool) -> None:
    """Reclaim orphaned operation refs left by an interrupted run.

    A run interrupted by Ctrl-C, a kill, or a crash can leave an orphaned operation
    ref that blocks the next run. Starting another run reclaims it automatically; this
    command does it explicitly. Only orphaned *operations* (a dead run's bookkeeping)
    are reclaimed — orphaned scopes (work-in-progress) are left for review.
    """
    from vcs_core import VcsCoreError

    workspace = _open_workspace(activate=True)
    try:
        try:
            reclaimed = list(workspace.mg.archive_orphaned_operations())
        except VcsCoreError as exc:
            raise click.ClickException(f"could not reclaim orphaned operations: {exc}") from exc
        if json_output:
            _emit_json({"reclaimed": reclaimed})
        elif reclaimed:
            click.echo(f"Reclaimed {len(reclaimed)} interrupted run(s): {', '.join(reclaimed)}")
        else:
            click.echo("Nothing to repair — no orphaned operations.")
    finally:
        _close_workspace(workspace)


@task.command("list")
@click.option("--status", help="Only include task versions with this status.")
@click.option("--prefix", help="Only include task ids with this prefix.")
@click.option("--json", "json_output", is_flag=True, help="Emit the raw JSON payload.")
def task_list(status: str | None, prefix: str | None, json_output: bool) -> None:
    """List task summaries from the selected task ledger."""
    workspace = _open_workspace(activate=False)
    try:
        rows = _query(lambda: workspace.tasks.list(status=status, prefix=prefix))
        _emit(rows, json_output=json_output, human=_emit_task_list)
    finally:
        _close_workspace(workspace)


@task.command("show")
@click.argument("task_ref")
@click.option("--json", "json_output", is_flag=True, help="Emit the raw JSON payload.")
def task_show(task_ref: str, json_output: bool) -> None:
    """Show one task definition and its signature/permission surface."""
    workspace = _open_workspace(activate=False)
    try:
        description = _query(lambda: workspace.tasks.describe(task_ref))
        if description is None:
            raise click.ClickException(f"no task matches {task_ref!r}")
        _emit(description, json_output=json_output, human=_emit_task_show)
    finally:
        _close_workspace(workspace)


@task.command("register")
@click.argument("source")
@click.option("--task-id", help="Task id to record; defaults to the import path with ':' replaced by '.'.")
@click.option("--may-default", help="Default may profile recorded for future runs.")
@click.option("--metadata", "metadata_json", default="{}", show_default=True, help="JSON object metadata.")
def task_register(source: str, task_id: str | None, may_default: str | None, metadata_json: str) -> None:
    """Register a task import path as an active task version."""
    workspace = _open_workspace(activate=True)
    metadata = _json_object(metadata_json, label="--metadata")
    try:
        _emit_json(workspace.tasks.register(source, task_id=task_id, may_default=may_default, metadata=metadata))
    finally:
        _close_workspace(workspace)


@task.command("resolve")
@click.argument("task_ref")
@click.option("--reason", default="cli", show_default=True, help="Resolution reason to record.")
@click.option("--parent-run", help="Reserved for managed runtime invocations; raw use fails closed.")
@click.option("--json", "json_output", is_flag=True, help="Emit the raw JSON payload.")
def task_resolve(task_ref: str, reason: str, parent_run: str | None, json_output: bool) -> None:
    """Resolve a task ref to an exact artifact lock."""
    if parent_run is not None:
        raise click.ClickException(
            "--parent-run requires managed invocation authority; raw CLI parent attachment is disabled"
        )
    workspace = _open_workspace(activate=parent_run is not None)
    try:
        resolved = workspace.runs.resolve_task(
            task_ref,
            reason=reason,
            parent_run_ref=parent_run,
            launch_surface="cli",
        )
        _emit(resolved, json_output=json_output, human=_emit_mapping_summary)
    finally:
        _close_workspace(workspace)


def _open_workspace(*, activate: bool = False) -> ShepherdWorkspace:
    workspace = os.path.abspath(".")
    repo_path = os.path.join(workspace, ".vcscore")  # noqa: PTH118
    if not os.path.exists(repo_path):  # noqa: PTH110
        raise click.ClickException("not a Shepherd workspace. Run `sp init` first.")
    return ShepherdWorkspace.discover(workspace, activate=activate)


def _close_workspace(workspace: Any) -> None:
    close = getattr(workspace, "close", None)
    if callable(close):
        close()


def _open_trace_store(path: str) -> Any:
    from shepherd2.trace_store import SQLiteTraceStore

    return SQLiteTraceStore(path)


def _run_selector(run_ref: str | None, *, latest: bool) -> str:
    if latest:
        if run_ref is not None:
            raise click.UsageError("pass either RUN_REF or --latest, not both")
        return "@latest"
    if run_ref is None:
        raise click.UsageError("missing RUN_REF; pass a run ref or --latest")
    return run_ref


def _optional_run_selector(run_ref: str | None, *, latest: bool) -> str | None:
    if latest:
        if run_ref is not None:
            raise click.UsageError("pass either RUN_REF or --latest, not both")
        return "@latest"
    return run_ref


def _settle_run_output(
    action: str,
    *,
    run_ref: str,
    trace_store_path: str | None,
    output_name: str,
    binding: str | None,
) -> None:
    workspace = _open_workspace(activate=True)
    trace_store = None if trace_store_path is None else _open_trace_store(trace_store_path)
    try:
        output = _query(
            lambda: workspace.runs.output_for_settlement(
                run_ref,
                output_name=output_name,
                binding=binding,
                trace_store=trace_store,
            )
        )
        method = getattr(workspace, action)
        _emit_json(_query(lambda: method(output)))
    finally:
        if trace_store is not None:
            close = getattr(trace_store, "close", None)
            if callable(close):
                close()
        _close_workspace(workspace)


def _query(callback: Any) -> Any:
    from vcs_core import OrphanedOperationsError

    try:
        return callback()
    except OrphanedOperationsError as exc:
        from shepherd_dialect.workspace_control.workspace import ORPHANED_OPERATIONS_REMEDY

        raise click.ClickException(ORPHANED_OPERATIONS_REMEDY) from exc
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


def _json_object(value: str, *, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"{label} must be a JSON object: {exc}") from exc
    if not isinstance(parsed, dict):
        raise click.ClickException(f"{label} must be a JSON object")
    return {str(key): item for key, item in parsed.items()}


def _emit_json(value: Any) -> None:
    click.echo(json.dumps(_jsonable(value), indent=2, sort_keys=True, default=str))


def _emit(value: Any, *, json_output: bool, human: Any) -> None:
    if json_output:
        _emit_json(value)
        return
    human(value)


def _emit_run_list(value: Any) -> None:
    rows = list(_jsonable(value))
    if not rows:
        click.echo("No runs.")
        return
    click.echo(f"{'RUN':12s} {'STATUS':10s} {'TASK':36s} {'STARTED'}")
    for row in rows:
        task = f"{row.get('task_id')}@{row.get('task_version')}"
        click.echo(
            f"{_short(row.get('run_ref')):12s} "
            f"{str(row.get('status', ''))[:10]:10s} "
            f"{task[:36]:36s} "
            f"{row.get('started_at') or '-'}"
        )


def _emit_run_show(value: Any) -> None:
    record = _jsonable(value)
    terminal = record.get("terminalization") or {}
    execution = record.get("execution_evidence") or {}
    outputs = record.get("outputs") or {}
    click.echo(f"Run {record.get('run_ref')}")
    click.echo(f"  status:       {record.get('status')}")
    click.echo(f"  task:         {record.get('task_id')}@{record.get('task_version')}")
    click.echo(f"  provider:     {record.get('provider')}")
    click.echo(f"  may:          {record.get('may_profile')}")
    click.echo(f"  enforcement:  {record.get('enforcement')} ({execution.get('enforcement_basis')})")
    flags = execution.get("effective_feature_flags")
    if flags:
        rendered = ", ".join(f"{name}={'on' if state else 'off'}" for name, state in sorted(flags.items()))
        click.echo(f"  flags:        {rendered}")
    click.echo(f"  terminal:     {terminal.get('body_status')} / {terminal.get('world_disposition')}")
    click.echo(f"  publication:  {terminal.get('output_publication_status')}")
    if outputs:
        click.echo("  outputs:")
        for name, output in outputs.items():
            click.echo(f"    {name}: {_short(output.get('output_id'))} {output.get('materialization_kind')}")
    trace = record.get("operation_refs", {}).get("trace_head")
    if trace:
        click.echo(f"  trace:        {trace}")


def _emit_outputs(value: Any) -> None:
    rows = list(_jsonable(value))
    if not rows:
        click.echo("No outputs.")
        return
    click.echo(f"{'OUTPUT':12s} {'STATE':12s} {'RUN':12s} PATHS")
    for row in rows:
        identity = row.get("identity") or {}
        owner = row.get("owner") or {}
        paths = ", ".join(row.get("changed_paths") or ())
        click.echo(
            f"{_short(identity.get('output_id')):12s} "
            f"{str(row.get('state', ''))[:12]:12s} "
            f"{_short(owner.get('run_id')):12s} "
            f"{paths or '-'}"
        )


def _emit_changeset(value: Any) -> None:
    changeset = _jsonable(value)
    click.echo(f"Changeset {_short(changeset.get('output_id'))}")
    click.echo(f"  binding: {changeset.get('binding')}")
    click.echo(f"  state:   {changeset.get('state')}")
    click.echo("  paths:")
    for path in changeset.get("changed_paths") or ():
        click.echo(f"    - {path}")


def _emit_trace(trace: RunTrace, *, show_events: bool, json_output: bool) -> None:
    if json_output:
        _emit_json({"summary": trace.summary(), "events": list(trace.events)} if show_events else trace.summary())
        return
    summary = trace.summary()
    click.echo(f"Trace {summary.get('run_id') or summary.get('execution_id') or '(unknown)'}")
    for key, item in summary.items():
        if key in {"run_id", "execution_id"}:
            continue
        click.echo(f"  {key}: {item}")
    if show_events:
        click.echo("  events:")
        for event in trace.events:
            event_payload = _jsonable(event)
            kind = event_payload.get("kind", "(unknown)") if isinstance(event_payload, dict) else "(unknown)"
            event_id = event_payload.get("event_id", "-") if isinstance(event_payload, dict) else "-"
            click.echo(f"    - {kind} {event_id}")


def _emit_task_list(value: Any) -> None:
    rows = list(_jsonable(value))
    if not rows:
        click.echo("No tasks.")
        return
    click.echo(f"{'TASK':40s} {'VERSION':10s} {'STATUS':10s} IMPORT")
    for row in rows:
        click.echo(
            f"{str(row.get('task_id', ''))[:40]:40s} "
            f"{str(row.get('version', ''))[:10]:10s} "
            f"{str(row.get('status', ''))[:10]:10s} "
            f"{row.get('import_path')}"
        )


def _grant_access_label(grant: Any) -> str:
    """Summarize one captured GitRepo grant descriptor as ``read-only``/``read-write``.

    A grant is read-only only when a clause pins ``mutates`` to ``False`` (``ReadOnly``).
    ``ReadWrite`` leaves ``mutates`` unconstrained (``None``) and path grants pin it to
    ``True``; both mean the binding may write. This reads the recorded descriptor only.
    """
    if isinstance(grant, dict):
        clauses = grant.get("clauses")
        if isinstance(clauses, list | tuple):
            for clause in clauses:
                if isinstance(clause, dict) and clause.get("mutates") is False:
                    return "read-only"
    return "read-write"


def _task_binding_grants(signature: Any) -> list[tuple[str, str]]:
    """Return ``(parameter, access)`` pairs for every per-binding GitRepo grant, in order."""
    grants: list[tuple[str, str]] = []
    if not isinstance(signature, dict):
        return grants
    parameters = signature.get("parameters")
    if not isinstance(parameters, list | tuple):
        return grants
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        grant = parameter.get("gitrepo_grant")
        if grant is None:
            continue
        name = parameter.get("name")
        if not isinstance(name, str) or not name:
            continue
        grants.append((name, _grant_access_label(grant)))
    return grants


def _task_grant_summary_line(task: Any) -> str | None:
    """Compute the leading permission-surface line for ``sp task show``.

    Leads with the per-binding grant summary (``docs read-only / backend read-write``);
    falls back to the task-level may profile (``may: ReadOnly``) when there are no
    per-binding grants; returns ``None`` when neither is recorded. Rendering only.
    """
    grants = _task_binding_grants(task.get("signature_schema") or {}) if isinstance(task, dict) else []
    if grants:
        return " / ".join(f"{name} {access}" for name, access in grants)
    may = task.get("may_default") if isinstance(task, dict) else None
    if may:
        return f"may: {may}"
    return None


def _emit_task_show(value: Any) -> None:
    description = _jsonable(value)
    task = description.get("task") or {}
    artifact = description.get("artifact") or {}
    summary = _task_grant_summary_line(task)
    if summary is not None:
        click.echo(summary)
    click.echo(f"Task {task.get('task_id')}@{task.get('version')}")
    click.echo(f"  status:  {task.get('status')}")
    click.echo(f"  import:  {task.get('import_path')}")
    click.echo(f"  may:     {task.get('may_default')}")
    if task.get("artifact_ref"):
        click.echo("  artifact: present")
    if artifact.get("docstring"):
        click.echo("  docstring:")
        for line in str(artifact["docstring"]).splitlines():
            click.echo(f"    {line}")
    signature = task.get("signature_schema") or {}
    if signature:
        click.echo("  signature:")
        for line in json.dumps(signature, indent=4, sort_keys=True, default=str).splitlines():
            click.echo(f"    {line}")
    if description.get("artifact_error"):
        click.echo(f"  artifact_error: {description['artifact_error']}")


def _emit_mapping_summary(value: Any) -> None:
    payload = _jsonable(value)
    if isinstance(payload, list):
        if not payload:
            click.echo("No rows.")
            return
        for item in payload:
            _emit_mapping_summary(item)
        return
    if not isinstance(payload, dict):
        click.echo(str(payload))
        return
    for key, item in payload.items():
        if isinstance(item, dict | list):
            rendered = json.dumps(item, sort_keys=True, default=str)
        else:
            rendered = str(item)
        click.echo(f"{key}: {rendered}")


def _short(value: object, length: int = 12) -> str:
    if value is None:
        return "-"
    text = str(value)
    return text if len(text) <= length else text[:length]


def _jsonable(value: Any) -> Any:
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        return _jsonable(to_json())
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value


if __name__ == "__main__":
    main()
